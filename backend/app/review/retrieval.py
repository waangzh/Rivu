"""Bounded pagination over authorized immutable sources."""

import asyncio
import fnmatch
import json
import re
import subprocess
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from uuid import uuid4

from app.review.domain import utcnow
from app.review.evidence import SourceReadError, digest, source_lines
from app.review.tool_scope import (
    _validate_repository_relative_path, is_sensitive_repository_path, validate_repository_file,
)
from app.tools.git_tool import GitToolCancelled, GitToolTimeout, call_with_control

PAGE_CHARS = 16_000
SCAN_FILES = 4096
SEARCH_REGEX_TIMEOUT_SECONDS = 15.0
MAX_SEARCH_WINDOWS = 16
MAX_SEARCH_WINDOW_CACHE_BYTES = 512 * 1024


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


def _search_deadline(context, *, regex=False):
    timeout = SEARCH_REGEX_TIMEOUT_SECONDS if regex else None
    provider = getattr(context, "provider", None)
    run = getattr(provider, "run", None) if provider is not None else None
    work = getattr(context, "work", None)
    if run is not None and work is not None and hasattr(run, "work_deadline"):
        remaining = (run.work_deadline(work) - utcnow()).total_seconds()
        if remaining <= 0:
            raise ValueError("snapshot_search_timeout")
        timeout = remaining if timeout is None else min(timeout, remaining)
    return time.monotonic() + timeout if timeout is not None else None


def _check_control(deadline, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise GitToolCancelled("Git 操作已取消")
    if deadline is not None and time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired("git", 0)


def _window_cache(context) -> OrderedDict:
    cache = getattr(context, "search_windows", None)
    if not isinstance(cache, OrderedDict):
        cache = OrderedDict()
        try:
            setattr(context, "search_windows", cache)
        except AttributeError:
            pass
    return cache


def _cache_window(context, window_id, payload):
    hits = payload["hits"]
    entry_bytes = _size(hits)
    if entry_bytes > MAX_SEARCH_WINDOW_CACHE_BYTES:
        return
    cache = _window_cache(context)
    if window_id in cache:
        cache.pop(window_id)
    total_bytes = sum(item.get("bytes", 0) for item in cache.values())
    while cache and (len(cache) >= MAX_SEARCH_WINDOWS
                     or total_bytes + entry_bytes > MAX_SEARCH_WINDOW_CACHE_BYTES):
        _, evicted = cache.popitem(last=False)
        total_bytes -= evicted.get("bytes", 0)
    cache[window_id] = {"hits": hits, "bytes": entry_bytes}


def _get_cached_window(context, window_id):
    cache = _window_cache(context)
    entry = cache.get(window_id)
    if entry is not None:
        cache.move_to_end(window_id)
    return entry


def _matching_hits(service, paths, side, query, *, ignore_case=False, word_boundary=False,
                   regex=False, deadline=None, cancel_event=None):
    _check_control(deadline, cancel_event)
    sha = service.snapshot.head_sha if side == "head" else service.snapshot.base_sha
    entries = call_with_control(
        service.git.get_tree_entries, service.repo_path, sha, paths,
        deadline=deadline, cancel_event=cancel_event,
    )
    readable, unreadable, excluded, remaining_bytes = [], set(), set(), 8 * 1024 * 1024
    processed = 0
    for path in paths:
        _check_control(deadline, cancel_event)
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
    _check_control(deadline, cancel_event)
    if ignore_case or word_boundary or regex:
        hits = call_with_control(
            service.git.find_matching_paths,
            service.repo_path, sha, query, readable,
            ignore_case=ignore_case, word_boundary=word_boundary, regex=regex,
            deadline=deadline, cancel_event=cancel_event,
        )
    else:
        hits = call_with_control(
            service.git.find_matching_paths,
            service.repo_path, sha, query, readable,
            deadline=deadline, cancel_event=cancel_event,
        )
    return hits, unreadable, excluded, processed


def _summarize_batch(service, sha, query, batch, *, ignore_case=False, word_boundary=False,
                     regex=False, deadline=None, cancel_event=None):
    _check_control(deadline, cancel_event)
    summarize = getattr(service.git, "summarize_matching_lines", None)
    if summarize is not None:
        return call_with_control(
            summarize, service.repo_path, sha, query, batch,
            ignore_case=ignore_case, word_boundary=word_boundary, regex=regex,
            deadline=deadline, cancel_event=cancel_event,
        )
    by_file = call_with_control(
        service.git.find_matching_lines, service.repo_path, sha, query, batch,
        ignore_case=ignore_case, word_boundary=word_boundary, regex=regex,
        deadline=deadline, cancel_event=cancel_event,
    )
    return {
        path: (min(lines), len(lines))
        for path, lines in by_file.items() if lines
    }


def _fit_single_match(match):
    """确保首条匹配项也绝不超过 PAGE_CHARS 预算。"""
    if _size([match]) <= PAGE_CHARS:
        return match
    shrunk = dict(match)
    shrunk.pop("before", None)
    shrunk.pop("after", None)
    for key in ("text", "sample"):
        if key in shrunk and len(shrunk[key]) > 120:
            shrunk[key] = shrunk[key][:120]
    if _size([shrunk]) <= PAGE_CHARS:
        return shrunk
    raise ValueError("path_too_large")


def _extract_file_matches(service, selected, side, query, begin, *,
                          sha="", enhanced=False, ignore_case=False,
                          word_boundary=False, regex=False,
                          file_summary=None, hit_lines=None,
                          context_lines=0, files_only=False,
                          max_items=41, existing_chars=2, has_prior_matches=False,
                          deadline=None, cancel_event=None):
    _check_control(deadline, cancel_event)
    _, content = call_with_control(
        service._source, selected, side, deadline=deadline, cancel_event=cancel_event,
    )
    _check_control(deadline, cancel_event)
    lines = source_lines(content)
    if not files_only and begin > len(lines):
        raise ValueError("invalid_cursor")
    if files_only:
        if file_summary is not None:
            first_line, count = file_summary
            if count <= 0 or not 1 <= first_line <= len(lines):
                return []
            first_hit = first_line - 1
        elif enhanced:
            if hit_lines is None:
                return []
            valid = [line_no for line_no in hit_lines if 1 <= line_no <= len(lines)]
            if not valid:
                return []
            first_hit = valid[0] - 1
            count = len(valid)
        else:
            count, first_hit = 0, None
            for line_no, text in enumerate(lines):
                if (line_no & 1023) == 0:
                    _check_control(deadline, cancel_event)
                if query in text:
                    count += 1
                    if first_hit is None:
                        first_hit = line_no
            if count == 0 or first_hit is None:
                return []
        return [(0, {
            "path": selected,
            "match_count": count,
            "first_line": first_hit + 1,
            "sample": lines[first_hit][:300],
            "side": side,
        })]

    if enhanced and hit_lines is None:
        if not sha:
            sha = service.snapshot.head_sha if side == "head" else service.snapshot.base_sha
        hit_lines = call_with_control(
            service.git.find_matching_lines,
            service.repo_path, sha, query, [selected],
            ignore_case=ignore_case, word_boundary=word_boundary, regex=regex,
            min_line=begin + 1, max_matches=max_items,
            deadline=deadline, cancel_event=cancel_event,
        ).get(selected, [])
        _check_control(deadline, cancel_event)

    items = []
    running_chars = existing_chars

    def build_match(line_idx):
        match = {"path": selected, "line": line_idx + 1, "text": lines[line_idx][:300], "side": side}
        if context_lines:
            match["before"] = [
                lines[idx][:300] for idx in range(max(0, line_idx - context_lines), line_idx)
            ]
            match["after"] = [
                lines[idx][:300]
                for idx in range(line_idx + 1, min(len(lines), line_idx + 1 + context_lines))
            ]
        return match

    if hit_lines is not None:
        candidate_indices = (
            line_no - 1 for line_no in hit_lines
            if 1 <= line_no <= len(lines) and line_no - 1 >= begin
        )
    else:
        candidate_indices = (
            line_idx for line_idx in range(begin, len(lines))
            if query in lines[line_idx]
        )

    for step, line_idx in enumerate(candidate_indices):
        if (step & 255) == 0:
            _check_control(deadline, cancel_event)
        match = build_match(line_idx)
        if not has_prior_matches and not items:
            match = _fit_single_match(match)
        items.append((line_idx, match))
        running_chars += _size(match) + 2
        # 多取至多 1 条命中即可判定是否需要续页及续页行号，避免无谓构造全文件命中。
        if len(items) >= max_items or ((has_prior_matches or len(items) > 1) and running_chars > PAGE_CHARS):
            break
    return items


async def search(context, query, path="", glob="", side="head", cursor="", limit=12,
                 *, exclude_globs=None, ignore_case=False, word_boundary=False,
                 regex=False, context_lines=0, files_only=False):
    _limit(limit, 40)
    if type(context_lines) is not int or not 0 <= context_lines <= 3:
        raise SourceReadError(
            "invalid_context_lines", minimum=0, maximum=3,
            next_action="Use an integer context_lines between 0 and 3.",
        )
    if (not query or len(query) > 200 or "\x00" in query or "\n" in query
            or side not in {"head", "base"}):
        raise ValueError("invalid_search")
    exclusions = _exclusions(exclude_globs)
    paths = _paths(context, path, glob, exclusions)
    filters = {"query": query, "path": path, "glob": glob,
               "exclude_globs": exclusions, "revision": side}
    if ignore_case:
        filters["ignore_case"] = True
    if word_boundary:
        filters["word_boundary"] = True
    if regex:
        filters["regex"] = True
    if context_lines:
        filters["context_lines"] = context_lines
    if files_only:
        filters["files_only"] = True
    scope = _scope(context, "search", {
        **filters,
        "ignore_case": bool(ignore_case),
        "word_boundary": bool(word_boundary),
        "regex": bool(regex),
        "context_lines": context_lines,
        "files_only": bool(files_only),
    })
    first, line_start, prior_unreadable = await asyncio.to_thread(
        _position, context, cursor, scope, len(paths))
    window_ref = None
    if cursor:
        saved = context.cursors[cursor]
        window_ref = saved.get("search_window")
    stop = min(len(paths), first + SCAN_FILES)
    matches, matched, unreadable, scanned = [], 0, 0, 0
    window_id = None
    enhanced = bool(ignore_case or word_boundary or regex)
    sha = context.evidence.snapshot.head_sha if side == "head" else context.evidence.snapshot.base_sha
    # 整次搜索共用同一个绝对截止时间与取消信号，贯穿元数据查询、grep 与源码读取。
    deadline = _search_deadline(context, regex=regex)
    cancel_event = threading.Event()
    try:
        if context.check_active:
            await context.check_active()
        _check_control(deadline, cancel_event)
        if window_ref:
            window_start = window_ref.get("start")
            stop = window_ref.get("stop")
            if (type(window_start) is not int or type(stop) is not int
                    or not 0 <= window_start <= first < stop <= len(paths)):
                raise ValueError("invalid_cursor")
            window_id = window_ref.get("window_id")
            cached = _get_cached_window(context, window_id) if window_id else None
            if cached is not None:
                hits = set(cached["hits"])
                rejected, excluded = set(), set()
                if not hits <= set(paths[window_start:stop]):
                    raise ValueError("invalid_cursor")
            elif "hits" in window_ref:
                hits = set(window_ref["hits"])
                rejected, excluded = set(), set()
                if not hits <= set(paths[window_start:stop]):
                    raise ValueError("invalid_cursor")
            else:
                hits, _, excluded, _ = await asyncio.to_thread(
                    _matching_hits, context.evidence, paths[window_start:stop], side, query,
                    ignore_case=ignore_case, word_boundary=word_boundary, regex=regex,
                    deadline=deadline, cancel_event=cancel_event)
                rejected = set()
        else:
            window_start = first
            hits, rejected, excluded, processed = await asyncio.to_thread(
                _matching_hits, context.evidence, paths[first:stop], side, query,
                ignore_case=ignore_case, word_boundary=word_boundary, regex=regex,
                deadline=deadline, cancel_event=cancel_event)
            scanned = processed
            stop = first + processed
            window_id = digest(uuid4().hex)
            window_ref = {"start": window_start, "stop": stop, "window_id": window_id}
        unreadable = len(rejected)
        positions = {selected: index for index, selected in enumerate(paths)}
        sorted_hits = [
            selected for selected in sorted(hits, key=positions.__getitem__)
            if positions[selected] >= first
        ]
        summaries: dict[str, tuple[int, int]] = {}
        summary_cursor = 0
        for pos, selected in enumerate(sorted_hits):
            index = positions[selected]
            if context.check_active:
                await context.check_active()
            _check_control(deadline, cancel_event)
            if files_only and enhanced and pos >= summary_cursor:
                batch_size = max(1, (limit - len(matches)) + 1)
                batch = sorted_hits[pos:pos + batch_size]
                summary_cursor = pos + len(batch)
                summaries.update(await asyncio.to_thread(
                    _summarize_batch, context.evidence, sha, query, batch,
                    ignore_case=ignore_case, word_boundary=word_boundary, regex=regex,
                    deadline=deadline, cancel_event=cancel_event,
                ))
            matched += 1
            begin = line_start if index == first else 0
            try:
                file_matches = await asyncio.to_thread(
                    _extract_file_matches, context.evidence, selected, side, query, begin,
                    sha=sha, enhanced=enhanced, ignore_case=ignore_case,
                    word_boundary=word_boundary, regex=regex,
                    file_summary=summaries.get(selected) if (files_only and enhanced) else None,
                    context_lines=context_lines, files_only=files_only,
                    max_items=(limit - len(matches)) + 1,
                    existing_chars=_size(matches),
                    has_prior_matches=bool(matches),
                    deadline=deadline, cancel_event=cancel_event,
                )
            except ValueError as exc:
                if str(exc) in {"invalid_cursor", "invalid_regex", "snapshot_search_timeout"}:
                    raise
                unreadable += 1
                continue
            except OSError:
                unreadable += 1
                continue
            for line, match in file_matches:
                if not matches:
                    match = _fit_single_match(match)
                if len(matches) >= limit or _size(matches + [match]) > PAGE_CHARS:
                    if context.check_active:
                        await context.check_active()
                    _check_control(deadline, cancel_event)
                    if window_id:
                        _cache_window(context, window_id, {"hits": sorted(hits)})
                    return await asyncio.to_thread(
                        _page, context, scope, cursor, (index, 0 if files_only else line), True,
                        source="search_hint", matches=matches, scanned_files=scanned,
                        matched_files=matched - 1 if files_only else matched,
                        unreadable_files=unreadable, policy_exclusions=len(excluded),
                        search_window=window_ref, unreadable_total=prior_unreadable + unreadable,
                        scope_files=len(paths), scope_complete=False, filters=filters)
                matches.append(match)
        if context.check_active:
            await context.check_active()
        _check_control(deadline, cancel_event)
        return await asyncio.to_thread(
            _page, context, scope, cursor, (stop, 0), stop < len(paths), source="search_hint",
            matches=matches, scanned_files=scanned, matched_files=matched, unreadable_files=unreadable,
            policy_exclusions=len(excluded),
            unreadable_total=prior_unreadable + unreadable,
            scope_files=len(paths), scope_complete=stop == len(paths) and not (prior_unreadable + unreadable),
            filters=filters)
    except asyncio.CancelledError:
        cancel_event.set()
        raise
    except GitToolCancelled as exc:
        raise asyncio.CancelledError() from exc
    except (subprocess.TimeoutExpired, GitToolTimeout) as exc:
        raise ValueError("snapshot_search_timeout") from exc
    except ValueError as exc:
        if str(exc) == "invalid_regex":
            raise SourceReadError(
                "invalid_regex",
                next_action="Fix the POSIX regular expression syntax or set regex=false for literal search.",
            ) from exc
        raise


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


def list_directory(context, path="", depth=1, cursor="", limit=80, *, exclude_globs=None):
    _limit(limit, 80)
    if type(depth) is not int or not 1 <= depth <= 3:
        raise SourceReadError(
            "invalid_depth", minimum=1, maximum=3,
            next_action="Use an integer depth between 1 and 3.",
        )
    exclusions = _exclusions(exclude_globs)
    files = _paths(context, path, "", exclusions)
    prefix = path.rstrip("/")
    dir_counts: dict[str, int] = {}
    file_entries: set[str] = set()
    for item in files:
        if prefix and item == prefix:
            file_entries.add(item)
            continue
        relative = item[len(prefix) + 1:] if prefix else item
        parts = relative.split("/")
        if len(parts) <= depth:
            file_entries.add(item)
            max_dir_depth = len(parts) - 1
        else:
            max_dir_depth = depth
        for level in range(1, max_dir_depth + 1):
            sub = "/".join(parts[:level])
            directory = f"{prefix}/{sub}" if prefix else sub
            dir_counts[directory] = dir_counts.get(directory, 0) + 1
    entries = sorted(
        [
            *({"path": directory, "kind": "dir", "file_count": count}
              for directory, count in dir_counts.items()),
            *({"path": file_path, "kind": "file"} for file_path in file_entries),
        ],
        key=lambda entry: entry["path"],
    )
    filters = {"path": path, "depth": depth, "exclude_globs": exclusions}
    scope = _scope(context, "list_dir", filters)
    index, column, _ = _position(context, cursor, scope, len(entries))
    if column:
        raise ValueError("invalid_cursor")
    selected = []
    while index < len(entries) and len(selected) < limit:
        if _size(selected + [entries[index]]) > PAGE_CHARS:
            if not selected:
                raise ValueError("path_too_large")
            break
        selected.append(entries[index])
        index += 1
    return _page(context, scope, cursor, (index, 0), index < len(entries),
                 source="dir_hint", entries=selected, total_entries=len(entries),
                 scope_files=len(files), filters=filters)
