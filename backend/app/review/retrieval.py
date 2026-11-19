"""Bounded pagination over authorized immutable sources."""

import asyncio
import fnmatch
import json
import re
import subprocess
from functools import lru_cache
from uuid import uuid4

from app.review.evidence import SourceReadError, digest, source_lines
from app.review.tool_scope import (
    _validate_repository_relative_path, is_sensitive_repository_path, validate_repository_file,
)

PAGE_CHARS = 16_000
SCAN_FILES = 4096


def _size(value):
    return len(json.dumps(value))


def _limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise SourceReadError("invalid_page_size", minimum=1, maximum=maximum,
                              next_action=f"Use an integer limit between 1 and {maximum}.")


def _scope(context, operation, parameters):
    snapshot = context.evidence.snapshot.model_dump()
    return digest(json.dumps([snapshot, operation, parameters,
        sorted(context.evidence.allowed_paths - context.denied_paths)], sort_keys=True))


def _position(context, cursor, scope, size):
    if cursor == "":
        return 0, 0, 0
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", cursor):
            raise ValueError("invalid_cursor")
        value = context.cursors[cursor]
        position, column, unreadable = value["position"], value["column"], value["unreadable_files"]
        if (value["scope"] != scope or type(position) is not int or type(column) is not int
                or not 0 <= position < size or column < 0
                or type(unreadable) is not int or unreadable < 0):
            raise ValueError("invalid_cursor")
        if (position, column) == (0, 0):
            raise ValueError("invalid_cursor")
        return position, column, unreadable
    except (ValueError, KeyError, TypeError, UnicodeError, OSError) as exc:
        raise SourceReadError(
            "invalid_cursor", retry_without_cursor=True,
            next_action="A cursor belongs to the original query, path, glob and revision. "
            "Keep all original filters to continue that search, or omit cursor to start "
            "a new search with the current filters. Never invent or edit cursor tokens.",
        ) from exc


def _page(context, scope, cursor, position, has_more, *, unreadable_total=0, search_window=None, **body):
    next_cursor = None
    if has_more:
        next_cursor = digest(uuid4().hex)
        context.cursors[next_cursor] = {"scope": scope, "position": position[0], "column": position[1],
                                       "unreadable_files": unreadable_total, "search_window": search_window}
    return {"ok": True, **body,
            **({"scope_empty": body["scope_files"] == 0} if "scope_files" in body else {}),
            "pagination_id": scope, "cursor": cursor,
            "next_cursor": next_cursor, "has_more": has_more, "truncated": False}


def _validate_glob(pattern):
    if pattern.startswith("!"):
        raise SourceReadError("unsupported_glob",
            next_action="Use a positive glob; put exclusions in exclude_globs without a leading !.")
    if (len(pattern) > 200 or "\\" in pattern or "\x00" in pattern
            or ".." in pattern.split("/")):
        raise SourceReadError("invalid_path_filter")


def _exclusions(patterns):
    if patterns is None:
        return []
    if (not isinstance(patterns, list) or len(patterns) > 16
            or any(not isinstance(pattern, str) or not pattern for pattern in patterns)):
        raise SourceReadError("invalid_exclude_globs",
                              next_action="Supply at most 16 nonempty positive glob strings.")
    for pattern in patterns:
        _validate_glob(pattern)
    return sorted(set(patterns))


def _matches_glob(path, pattern):
    path_parts = path.split("/")
    pattern_parts = pattern.split("/") if "/" in pattern else ["**", pattern]

    @lru_cache(maxsize=None)
    def matches(path_index, pattern_index):
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        component = pattern_parts[pattern_index]
        if component == "**":
            return (matches(path_index, pattern_index + 1)
                    or path_index < len(path_parts) and matches(path_index + 1, pattern_index))
        return (path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], component)
                and matches(path_index + 1, pattern_index + 1))

    return matches(0, 0)


def _paths(context, path="", glob="", exclude_globs=None):
    if path:
        _validate_repository_relative_path(path.rstrip("/"))
        if path.rstrip("/") in context.denied_paths or is_sensitive_repository_path(path.rstrip("/")):
            raise ValueError("path_not_authorized")
    _validate_glob(glob)
    exclusions = _exclusions(exclude_globs)
    prefix = path.rstrip("/")
    return sorted(item for item in context.evidence.allowed_paths
        if item not in context.denied_paths and not is_sensitive_repository_path(item)
        and (not prefix or item == prefix or item.startswith(prefix + "/"))
        and (not glob or _matches_glob(item, glob))
        and not any(_matches_glob(item, pattern) for pattern in exclusions))


def _matching_paths(service, paths, side, query):
    sha = service.snapshot.head_sha if side == "head" else service.snapshot.base_sha
    entries = service.git.get_tree_entries(service.repo_path, sha, paths)
    readable, unreadable, excluded, remaining_bytes = [], set(), set(), 8 * 1024 * 1024
    processed = 0
    for path in paths:
        entry = entries.get(path)
        if entry is None:
            processed += 1
            continue  # A BASE deletion or old rename path need not exist in HEAD.
        try:
            if entry.get("mode") not in {"100644", "100755"}:
                excluded.add(path)
                processed += 1
                continue
            if entry.get("size") is None or entry["size"] > service.max_file_bytes:
                raise ValueError("source_too_large")
            if entry["size"] > remaining_bytes:
                break
            local = service.repo_path / path
            if local.exists() or local.is_symlink():
                validate_repository_file(service.repo_path, path, require_tracked_if_git=False)
            readable.append(path)
            remaining_bytes -= entry["size"]
        except (ValueError, OSError):
            unreadable.add(path)
        processed += 1
    return service.git.find_matching_paths(service.repo_path, sha, query, readable), unreadable, excluded, processed


async def search(context, query, path="", glob="", side="head", cursor="", limit=12,
                 *, exclude_globs=None):
    _limit(limit, 40)
    if not query or len(query) > 200 or "\x00" in query or "\n" in query or side not in {"head", "base"}:
        raise ValueError("invalid_search")
    exclusions = _exclusions(exclude_globs)
    paths = _paths(context, path, glob, exclusions)
    filters = {"query": query, "path": path, "glob": glob,
               "exclude_globs": exclusions, "revision": side}
    scope = _scope(context, "search", filters)
    first, line_start, prior_unreadable = await asyncio.to_thread(
        _position, context, cursor, scope, len(paths))
    window = None
    if cursor:
        saved = context.cursors[cursor]
        window = saved.get("search_window")
    stop = min(len(paths), first + SCAN_FILES)
    matches, matched, unreadable, scanned = [], 0, 0, 0
    # git grep only locates matching files; source reads still enforce blob and path policy.
    try:
        if window:
            stop = window["stop"]
            hits, rejected, excluded = set(window["hits"]), set(), set()
            if not first < stop <= len(paths) or not hits <= set(paths[:stop]):
                raise ValueError("invalid_cursor")
        else:
            hits, rejected, excluded, processed = await asyncio.to_thread(
                _matching_paths, context.evidence, paths[first:stop], side, query)
            scanned = processed
            stop = first + processed
            window = {"stop": stop, "hits": sorted(hits)}
    except subprocess.TimeoutExpired as exc:
        raise ValueError("snapshot_search_timeout") from exc
    # Locate matches across this bounded scan before paging source hits, not unrelated paths.
    unreadable = len(rejected)
    positions = {selected: index for index, selected in enumerate(paths)}
    for selected in sorted(hits, key=positions.__getitem__):
        index = positions[selected]
        if index < first:
            continue
        if context.check_active:
            await context.check_active()
        matched += 1
        try:
            _, content = await asyncio.to_thread(context.evidence._source, selected, side)
        except (ValueError, OSError):
            unreadable += 1
            continue
        lines = source_lines(content)
        begin = line_start if index == first else 0
        if begin > len(lines):
            raise ValueError("invalid_cursor")
        for line in range(begin, len(lines)):
            if query not in lines[line]:
                continue
            match = {"path": selected, "line": line + 1, "text": lines[line][:300], "side": side}
            if len(matches) >= limit or _size(matches + [match]) > PAGE_CHARS:
                return await asyncio.to_thread(
                    _page, context, scope, cursor, (index, line), True, source="search_hint",
                    matches=matches, scanned_files=scanned, matched_files=matched, unreadable_files=unreadable,
                    policy_exclusions=len(excluded),
                    search_window=window,
                    unreadable_total=prior_unreadable + unreadable,
                    scope_files=len(paths), scope_complete=False, filters=filters)
            matches.append(match)
    return await asyncio.to_thread(
        _page, context, scope, cursor, (stop, 0), stop < len(paths), source="search_hint",
        matches=matches, scanned_files=scanned, matched_files=matched, unreadable_files=unreadable,
        policy_exclusions=len(excluded),
        unreadable_total=prior_unreadable + unreadable,
        scope_files=len(paths), scope_complete=stop == len(paths) and not (prior_unreadable + unreadable),
        filters=filters)


def find_paths(context, query="", path="", glob="", cursor="", limit=80, *, exclude_globs=None):
    _limit(limit, 80)
    if len(query) > 200 or "\x00" in query:
        raise ValueError("invalid_path_query")
    exclusions = _exclusions(exclude_globs)
    paths = [item for item in _paths(context, path, glob, exclusions) if query in item]
    filters = {"query": query, "path": path, "glob": glob, "exclude_globs": exclusions}
    scope = _scope(context, "paths", filters)
    index, column, _ = _position(context, cursor, scope, len(paths))
    if column:
        raise ValueError("invalid_cursor")
    selected = []
    while index < len(paths) and len(selected) < limit:
        if _size(selected + [paths[index]]) > PAGE_CHARS:
            if not selected:
                raise ValueError("path_too_large")
            break
        selected.append(paths[index])
        index += 1
    return _page(context, scope, cursor, (index, 0), index < len(paths),
                 source="path_hint", paths=selected, scope_files=len(paths), filters=filters)
