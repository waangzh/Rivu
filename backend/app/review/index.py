"""Run-local inventory and query-driven use of the existing isolated parsers."""

import asyncio
import json
import subprocess
import threading
from pathlib import Path

from app.languages.base import AnalysisLevel
from app.languages.isolation import analyze_isolated
from app.languages.registry import default_language_registry
from app.review.evidence import SourceReadError
from app.review.tool_scope import (
    ReviewPathPolicyError, _validate_repository_relative_path, is_sensitive_repository_path,
)
from app.tools.git_tool import call_with_control
from app.tools.repo_indexer import _scan_imports
from app.tools.repository_graph import (
    build_repository_graph, resolved_import_targets, reverse_import_hints,
)


_MAX_SYMBOL_FIELD_CHARS = 256
_MAX_SYMBOL_PAGE_CHARS = 16_000


class OnDemandIndex:
    def __init__(self, evidence):
        self.evidence = evidence
        self.inventory = tuple(sorted(evidence.allowed_paths))
        self.entries = {path: {"path": path,
            "language": default_language_registry.detect_language(path), "imports": [],
            "import_refs": [], "parser_id": "unparsed"} for path in self.inventory}
        self.analyses, self.scanned = {}, {}
        self.cancel_event = threading.Event()
        self._lock = asyncio.Lock()

    def _parse(self, path, side):
        if path not in self.entries:
            raise ValueError("path_not_authorized")
        key = (side, path)
        if key not in self.analyses:
            _, content = self.evidence._source(path, side)
            adapter = default_language_registry.for_path(path)
            if adapter is None:
                raise SourceReadError("parser_unavailable", next_action="Read fixed lines with read_file.")
            self.analyses[key] = analyze_isolated(adapter, content.encode("utf-8"), path,
                Path(path).suffix.casefold(), cancel_event=self.cancel_event)
        return self.analyses[key]

    def _head_entries(self, parsed_paths):
        # Keep the inventory for module resolution, overlay only this query's analyses.
        entries = dict(self.entries)
        for path in parsed_paths:
            analysis = self.analyses["head", path]
            entries[path] = {
                **entries[path], "imports": sorted({ref.module for ref in analysis.imports}),
                "import_refs": [ref.model_dump() for ref in analysis.imports],
                "python_namespace": analysis.python_namespace, "parser_id": analysis.parser_id,
            }
        return entries

    def _symbol(self, path, name, side):
        analysis = self._parse(path, side)
        matches = [symbol for symbol in analysis.symbols if name in {symbol.name, symbol.symbol_id}]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise SourceReadError("symbol_ambiguous", matches=[
                {"symbol": item.name, "symbol_id": item.symbol_id,
                 "range": [item.start_line, item.end_line]} for item in matches[:8]
            ], truncated=len(matches) > 8, next_action=(
                "Use a unique symbol_id where available, or read the intended range with read_file."
            ))
        code = "symbol_not_found" if analysis.level == AnalysisLevel.semantic else "parser_unavailable"
        raise SourceReadError(code, next_action="Use search_code and read_file at the requested revision.")

    async def symbol(self, path, name, side="head"):
        async with self._lock:
            symbol = await asyncio.to_thread(self._symbol, path, name, side)
            return symbol.to_index_entry()

    def _list_symbols(self, path, side="head", offset=0, limit=80):
        if side not in {"head", "base"}:
            raise SourceReadError("invalid_source_side")
        if type(offset) is not int or offset < 0:
            raise SourceReadError("invalid_source_range", next_action="Use a non-negative integer offset.")
        if type(limit) is not int or not 1 <= limit <= 120:
            raise SourceReadError("invalid_page_size", minimum=1, maximum=120,
                                  next_action="Use an integer limit between 1 and 120.")
        analysis = self._parse(path, side)
        if analysis.level != AnalysisLevel.semantic:
            raise SourceReadError("parser_unavailable", next_action="Read fixed lines with read_file.")
        symbols = analysis.symbols
        selected = []
        index = offset
        while index < len(symbols) and len(selected) < limit:
            item = symbols[index]
            name = item.name
            name_truncated = False
            if len(name) > _MAX_SYMBOL_FIELD_CHARS:
                name = name[:_MAX_SYMBOL_FIELD_CHARS] + "..."
                name_truncated = True
            entry = {
                "symbol": name,
                "kind": item.kind,
                "range": [item.start_line, item.end_line],
            }
            if item.symbol_id is not None:
                if len(item.symbol_id) <= _MAX_SYMBOL_FIELD_CHARS and not name_truncated:
                    entry["symbol_id"] = item.symbol_id
                else:
                    name_truncated = True
            if item.container:
                if len(item.container) > _MAX_SYMBOL_FIELD_CHARS:
                    entry["container"] = item.container[:_MAX_SYMBOL_FIELD_CHARS] + "..."
                    name_truncated = True
                else:
                    entry["container"] = item.container
            if name_truncated:
                entry["name_truncated"] = True
            if len(json.dumps(selected + [entry], ensure_ascii=False)) > _MAX_SYMBOL_PAGE_CHARS:
                if not selected:
                    entry = {
                        "symbol": entry["symbol"][:64] + "...",
                        "kind": str(item.kind)[:64],
                        "range": [item.start_line, item.end_line],
                        "name_truncated": True,
                    }
                    if len(json.dumps([entry], ensure_ascii=False)) > _MAX_SYMBOL_PAGE_CHARS:
                        raise SourceReadError("source_output_too_large")
                else:
                    break
            selected.append(entry)
            index += 1
        next_offset = index if index < len(symbols) else None
        return {
            "ok": True,
            "source": "symbol_outline_hint",
            "path": path,
            "revision": side,
            "parser_id": analysis.parser_id,
            "symbols": selected,
            "total_symbols": len(symbols),
            "offset": offset,
            "next_offset": next_offset,
            "has_more": next_offset is not None,
            "truncated": any(item.get("name_truncated") for item in selected),
        }

    async def list_symbols(self, path, side="head", offset=0, limit=80):
        async with self._lock:
            return await asyncio.to_thread(self._list_symbols, path, side, offset, limit)

    def _prefetch_scan_window(self, window, remaining):
        """按候选顺序在同份 8 MiB 预算内预取未缓存 HEAD 源码，不扩大本次扫描窗口。"""
        git = getattr(self.evidence, "git", None)
        batch_reader = getattr(git, "get_files_content_at_revision", None)
        if batch_reader is None or not window:
            return {}, {}
        sha = self.evidence.snapshot.head_sha
        need_meta = [
            path for path in window
            if path not in self.scanned
            and path in self.evidence.allowed_paths
            and not is_sensitive_repository_path(path)
            and self.evidence._cache_get((sha, path)) is None
        ]
        entries = {}
        if need_meta:
            try:
                entries = git.get_tree_entries(self.evidence.repo_path, sha, need_meta)
            except (ValueError, OSError):
                return {}, {}
        budget = remaining
        to_fetch, valid_entries = [], {}
        for path in window:
            if budget <= 0:
                break
            cached_scan = self.scanned.get(path)
            if cached_scan is not None:
                size = cached_scan[0]
                if size > budget:
                    break
                budget -= size
                continue
            if path not in self.evidence.allowed_paths or is_sensitive_repository_path(path):
                continue
            cached_source = self.evidence._cache_get((sha, path))
            if cached_source is not None:
                entry, content = cached_source
                try:
                    self.evidence._check_entry(entry)
                    self.evidence._check_worktree(path)
                except (ValueError, OSError):
                    continue
                size = len(content.encode("utf-8"))
                if size > budget:
                    break
                budget -= size
                continue
            entry = entries.get(path)
            try:
                self.evidence._check_entry(entry)
                self.evidence._check_worktree(path)
            except (ValueError, OSError):
                continue
            size = entry["size"] or 0
            if size > budget:
                break
            to_fetch.append(path)
            valid_entries[path] = entry
            budget -= size
        if not to_fetch:
            return {}, entries
        try:
            fetched = batch_reader(
                self.evidence.repo_path, sha, to_fetch, max_file_bytes=self.evidence.max_file_bytes,
            )
        except (ValueError, OSError):
            return {}, entries
        for path, content in fetched.items():
            self.evidence._cache_put((sha, path), valid_entries[path], content)
        return fetched, entries

    def _scan_scope(self, candidates):
        remaining = 8 * 1024 * 1024
        window = candidates[:512]
        sha = self.evidence.snapshot.head_sha
        git = getattr(self.evidence, "git", None)
        prefetched, entries = self._prefetch_scan_window(window, remaining)
        scanned = {}
        for candidate in window:
            if remaining <= 0:
                break
            cached = self.scanned.get(candidate)
            if cached is None:
                content = prefetched.get(candidate)
                if content is None:
                    entry = entries.get(candidate)
                    if entry is not None:
                        try:
                            self.evidence._check_entry(entry)
                            self.evidence._check_worktree(candidate)
                        except (ValueError, OSError):
                            continue
                        if (entry.get("size") or 0) > remaining:
                            # 先区分不可读 blob（如二进制/非 UTF-8）与真正耗尽预算的文本文件。
                            cached_source = self.evidence._cache_get((sha, candidate))
                            if cached_source is not None:
                                if len(cached_source[1].encode("utf-8")) > remaining:
                                    break
                                content = cached_source[1]
                            elif git is not None:
                                try:
                                    probe = call_with_control(
                                        git.get_file_content_at_revision,
                                        self.evidence.repo_path, sha, candidate,
                                    )
                                except (ValueError, OSError, subprocess.SubprocessError):
                                    continue
                                if probe is None:
                                    continue
                                if len(probe.encode("utf-8")) > remaining:
                                    break
                                content = probe
                    if content is None:
                        try:
                            _, content = self.evidence._source(candidate, "head")
                        except (ValueError, OSError):
                            continue
                encoded = content.encode("utf-8")
                size = len(encoded)
                if size > remaining:
                    break
                imports = _scan_imports(Path(candidate), self.entries[candidate]["language"],
                                        source=encoded, cancel_event=self.cancel_event)
                self.scanned[candidate] = size, imports
            else:
                size, imports = cached
                if size > remaining:
                    break
            remaining -= size
            scanned[candidate] = imports
        return scanned

    def _relations(self, path, name, direction, scope):
        if scope:
            try:
                scope = _validate_repository_relative_path(scope.rstrip("/"))
            except ReviewPathPolicyError as exc:
                raise SourceReadError("invalid_path_filter") from exc
            if is_sensitive_repository_path(scope):
                raise ValueError("path_not_authorized")
        selected = self._symbol(path, name, "head")
        entries = list(self._head_entries([path]).values())
        scoped_paths = {candidate for candidate in self.inventory
                        if not scope or candidate == scope or candidate.startswith(scope + "/")}
        candidates = scoped_paths - {path}
        targets = resolved_import_targets(entries, [path]) & candidates if direction != "in" else set()
        scanned = {}
        if direction != "out":
            scanned = self._scan_scope(sorted(candidate for candidate in candidates
                                             if self.entries[candidate]["language"] != "unknown"))
            targets.update(reverse_import_hints(entries, scanned, [path]))
        parsed_paths = [path]
        for target in sorted(targets)[:32]:
            try:
                self._parse(target, "head")
            except (ValueError, OSError):
                continue
            parsed_paths.append(target)
        symbols = [symbol.to_index_entry() for parsed in parsed_paths
                   for symbol in self.analyses["head", parsed].symbols]
        graph = build_repository_graph(list(self._head_entries(parsed_paths).values()), symbols)
        symbol_id = selected.symbol_id or f"{path}::{selected.name}"
        edges = [edge for edge in graph["edges"] if edge["type"] == "calls" and (
            (direction != "out" and edge["target"] == symbol_id
             and edge["source_file"] in scoped_paths)
            or (direction != "in" and edge["source"] == symbol_id
                and edge["target_file"] in scoped_paths))]
        return {"ok": True, "source": "static_relation_hint", "revision": "head",
            "scope": scope, "edges": edges[:40],
            "truncated": len(edges) > 40, "scope_complete": False,
            "parsed_files": len(parsed_paths), "scanned_files": len(scanned),
            "limitation": "Bounded HEAD call hints; empty edges do not establish absence. "
                          "For deleted or renamed code and old consumers, use search_code and read_file on BASE."}

    async def relations(self, path, name, direction, scope=""):
        async with self._lock:
            return await asyncio.to_thread(self._relations, path, name, direction, scope)
