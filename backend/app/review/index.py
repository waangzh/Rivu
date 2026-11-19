"""Run-local inventory and query-driven use of the existing isolated parsers."""

import asyncio
import threading
from pathlib import Path

from app.languages.base import AnalysisLevel
from app.languages.isolation import analyze_isolated
from app.languages.registry import default_language_registry
from app.review.evidence import SourceReadError
from app.review.tool_scope import (
    ReviewPathPolicyError, _validate_repository_relative_path, is_sensitive_repository_path,
)
from app.tools.repo_indexer import _scan_imports
from app.tools.repository_graph import (
    build_repository_graph, resolved_import_targets, reverse_import_hints,
)


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

    def _scan_scope(self, candidates):
        remaining = 8 * 1024 * 1024
        scanned = {}
        for candidate in candidates[:512]:
            if remaining <= 0:
                break
            cached = self.scanned.get(candidate)
            if cached is None:
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
