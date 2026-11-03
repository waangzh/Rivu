"""Attempt-local references over shared immutable source bytes."""

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import Field

from app.review.domain import Model, Snapshot, SourceRef
from app.review.evidence import digest, source_lines
from app.review.planner import identity, members, tokens, unit_capacity
from app.review.store import BudgetExceeded


TOOL_LOOP_WARNING = 3
TOOL_LOOP_LIMIT = 5


class RepeatedToolLoop(RuntimeError):
    """An identical tool loop exhausted its bounded finalization attempts."""


class InvestigationState(Model):
    question_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=500)
    status: Literal["open", "resolved", "blocked", "not_applicable"]
    source_refs: list[str] = Field(default_factory=list, max_length=32)
    counterevidence_refs: list[str] = Field(default_factory=list, max_length=32)
    retrieval_refs: list[str] = Field(default_factory=list, max_length=32)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    candidate_ids: list[str] = Field(default_factory=list, max_length=16)
    scope: str = Field(default="", max_length=500)
    outcome: Literal["supported", "refuted", "undetermined"] = "undetermined"
    conclusion: str = Field(min_length=1, max_length=1500, description=(
        "Compact investigation notes, including counterevidence and the scope of the conclusion. "
        "For an open question, record what is known so far. Not a verified finding."
    ))
    missing_premise: str | None = Field(default=None, max_length=500)


@dataclass
class AgentContext:
    provider: Any
    work: Any
    budget: Any
    visible: dict[str, SourceRef] = field(default_factory=dict)
    pending: dict[str, SourceRef] = field(default_factory=dict)
    pending_hunks: dict[str, str] = field(default_factory=dict)
    material_failures: dict[str, str] = field(default_factory=dict)
    cursors: dict[str, dict] = field(default_factory=dict)
    tool_calls: int = 0
    model_calls: int = 0
    structured_output_retry: bool = False
    verification_correction_limit: int | None = None
    source_counter: int = 0
    tool_repetitions: dict[str, tuple[str, int]] = field(default_factory=dict)
    round_calls: list[str] = field(default_factory=list)
    previous_tool_round: str | None = None
    repeated_tool_rounds: int = 0
    loop_closing_calls: int = 0
    investigation_closed: bool = False
    investigation_tools: set[str] = field(default_factory=set)
    source_paths: set[str] = field(default_factory=set)
    related_paths: set[str] = field(default_factory=set)
    delivered_hunks: set[str] = field(default_factory=set)
    coverage_checkpoint_sent: bool = False
    investigations: dict[str, InvestigationState] = field(default_factory=dict)
    memory_revision: int = 0
    memory_only_round: bool = False
    receipts: dict[str, dict] = field(default_factory=dict)
    visible_receipts: set[str] = field(default_factory=set)
    submitted: dict[str, dict] = field(default_factory=dict)
    initial_packet: dict = field(default_factory=dict)
    initial_archive: dict | None = None
    window: Any = None

    @property
    def evidence(self):
        return self.provider.evidence

    @property
    def denied_paths(self):
        return self.provider.denied_paths

    @property
    def changed_files(self):
        return [item.model_dump() for item in self.provider.run.changed_files]

    async def check_active(self):
        await self.budget.active()

    def note_tool_result(self, name, arguments, result) -> int:
        if name == "record_investigation" and result.get("ok") is True:
            self.memory_only_round = True
            return 0
        normalized = dict(arguments)
        cursor = normalized.get("cursor")
        if cursor in self.cursors:
            saved = self.cursors[cursor]
            normalized["cursor"] = {key: saved.get(key) for key in ("scope", "position", "column")}
        key = identity([name, normalized])
        # Opaque handles do not change a deterministic result.
        def semantic(value):
            if isinstance(value, dict):
                return {key: semantic(item) for key, item in value.items()
                        if key not in {"cursor", "next_cursor", "source_ref"}}
            if isinstance(value, list):
                return [semantic(item) for item in value]
            return value
        digest = identity(semantic(result))
        self.round_calls.append(identity([key, digest]))
        if result.get("ok") is True:
            self.investigation_tools.add(name)
            if name == "search_code":
                self.related_paths.update(match["path"] for match in result.get("matches", []))
            elif name == "find_relations":
                self.related_paths.update(
                    edge[key].partition("::")[0] for edge in result.get("edges", [])
                    for key in ("source", "target") if isinstance(edge.get(key), str)
                )
        sources = ([result["source"]] if isinstance(result.get("source"), dict) else [])
        sources.extend(item["source"] for item in result.get("sources", [])
                       if isinstance(item, dict) and isinstance(item.get("source"), dict))
        for source in sources:
            self.source_paths.add(source.get("path", ""))
        previous, count = self.tool_repetitions.get(key, (None, 0))
        count = count + 1 if previous == digest else 1
        self.tool_repetitions[key] = (digest, count)
        return count

    @staticmethod
    def is_test_path(path: str) -> bool:
        parts = PurePosixPath(path.casefold()).parts
        name = parts[-1] if parts else ""
        return (
            any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts[:-1])
            or name.startswith("test_") or name.endswith(("_test.py", "_test.go"))
            or ".test." in name or ".spec." in name
        )

    def small_unit_ready_for_checkpoint(self) -> bool:
        if self.work.kind != "review":
            return False
        unit = next(unit for unit in self.provider.run.plan.units if unit.id == self.work.unit_id)
        changed_lines = sum(
            item.get("additions", 0) + item.get("deletions", 0)
            for item in self.changed_files if item["file_path"] in unit.paths
        )
        if len(unit.paths) > 3 or changed_lines >= 200:
            return False
        relation_checked = bool(self.investigation_tools & {
            "search_code", "find_relations",
        })
        materials_received = set(unit.required_hunk_ids) <= self.delivered_hunks
        tests_received = any(self.is_test_path(path) for path in self.source_paths)
        consumer_received = any(
            path in self.related_paths and path not in unit.paths and not self.is_test_path(path)
            for path in self.source_paths
        )
        return (
            materials_received
            and tests_received
            and relation_checked
            and consumer_received
        )

    @staticmethod
    def _source_items(value):
        if isinstance(value, dict):
            source = value.get("source")
            if (isinstance(source, dict) and isinstance(value.get("content"), str)
                    and {"commit_sha", "side", "path", "start_line", "end_line"} <= source.keys()):
                # Projection also consumes compact metadata; evidence admission is separate.
                yield value, source
            for item in value.values():
                yield from AgentContext._source_items(item)
        elif isinstance(value, list):
            for item in value:
                yield from AgentContext._source_items(item)

    @staticmethod
    def _source_range(source):
        return (
            source.get("commit_sha"), source.get("side"), source.get("path"),
            source.get("start_line"), source.get("end_line"),
        )

    @classmethod
    def compact_messages(cls, messages):
        """Deduplicate only source bodies present in this request projection."""
        ranges = {}
        tool_results = {}
        compacted = []
        for message in messages:
            if message.type not in {"human", "tool"} or not isinstance(message.content, str):
                compacted.append(message)
                continue
            try:
                payload = json.loads(message.content)
            except (TypeError, ValueError):
                compacted.append(message)
                continue
            changed = False
            for item, source in cls._source_items(payload):
                key = cls._source_range(source)[:3]
                start, end = source.get("start_line"), source.get("end_line")
                known = ranges.setdefault(key, [])
                duplicate = any(first <= start and end <= last for first, last in known)
                if duplicate:
                    item["content"] = (
                        f"[source body omitted; use evidence {item.get('source_ref', source.get('id'))} "
                        f"for {source.get('path')}:{start}-{end}]"
                    )
                    item["content_omitted"] = True
                    changed = True
                merged = []
                for first, last in sorted([*known, (start, end)]):
                    if merged and first <= merged[-1][1] + 1:
                        merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
                    else:
                        merged.append((first, last))
                ranges[key] = merged
                if message.type == "tool":
                    item["source"] = {key: source[key] for key in (
                        "commit_sha", "side", "path", "start_line", "end_line",
                    )}
                    changed = True
            if message.type == "tool" and isinstance(payload, dict):
                fingerprint = identity([message.name, payload])
                previous = tool_results.get(fingerprint)
                if previous and any(key in payload for key in ("matches", "edges", "paths", "member")):
                    payload = {key: value for key, value in payload.items()
                               if key not in {"matches", "edges", "paths", "member"}}
                    payload["same_result_as_tool_call"] = previous
                    payload["content_omitted"] = True
                    changed = True
                else:
                    tool_results[fingerprint] = message.tool_call_id
            compacted.append(message.model_copy(update={
                "content": json.dumps(payload, ensure_ascii=False),
            }) if changed else message)
        return compacted

    async def archive_tool(self, name, arguments, message):
        """Archive the original result once; handles never grant source authority."""
        payload = {
            "review_id": self.provider.run.id, "attempt_id": self.work.attempt_id,
            "snapshot": self.provider.run.snapshot.model_dump(),
            "tool": name, "arguments": arguments, "tool_call_id": message.tool_call_id,
            "result": json.loads(message.content),
        }
        artifact = await asyncio.to_thread(self.evidence.artifacts.put_text,
            task_id=self.provider.run.id, kind="context-receipt",
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle = f"R{len(self.receipts) + 1}"
        result = payload["result"]
        summary = {key: value for key, value in result.items() if key not in {
            "content", "source", "sources", "matches", "edges", "paths", "member",
            "investigation", "original",
        }}
        summary.update({f"{key}_count": len(result[key])
                        for key in ("matches", "edges", "paths") if key in result})
        self.receipts[handle] = {
            "uri": artifact.uri, "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "tool": name, "arguments": arguments, "summary": summary,
        }
        return message.model_copy(update={
            "content": json.dumps({**result, "receipt_ref": handle}, ensure_ascii=False)})

    async def recall(self, handle):
        if handle not in self.visible_receipts:
            raise ValueError("receipt_not_visible_in_this_attempt")
        receipt = self.receipts[handle]
        encoded = await asyncio.to_thread(self.evidence.artifacts.read_text, receipt["uri"])
        if digest(encoded) != receipt["sha256"]:
            raise ValueError("context_receipt_corrupted")
        payload = json.loads(encoded)
        return {"ok": True, "original": payload}

    async def persist(self):
        if self.initial_archive is None:
            initial = await asyncio.to_thread(self.evidence.artifacts.put_text,
                task_id=self.provider.run.id, kind="context-initial",
                content=json.dumps(self.initial_packet, ensure_ascii=False, sort_keys=True))
            self.initial_archive = {"uri": initial.uri, "sha256": initial.sha256}
        state = {
            "review_id": self.provider.run.id, "unit_id": self.work.unit_id,
            "attempt_id": self.work.attempt_id,
            "initial_context": self.initial_archive,
            "investigations": {key: value.model_dump() for key, value in self.investigations.items()},
            "memory_revision": self.memory_revision,
            "sources": {key: value.model_dump() for key, value in self.visible.items()},
            "receipts": self.receipts, "visible_receipts": sorted(self.visible_receipts),
            "candidates": self.submitted, "cursors": self.cursors,
            "context_metrics": self.window.metrics if self.window else {},
        }
        artifact = await asyncio.to_thread(self.evidence.artifacts.put_text,
            task_id=self.provider.run.id, kind="context-state",
            content=json.dumps(state, ensure_ascii=False, sort_keys=True))
        await asyncio.to_thread(self.provider.store.save_context, self.provider.run.id,
            self.work, artifact.uri, self.provider.lease)

    def begin_round(self):
        # Parallel tool completion order must not change the round identity.
        fingerprint = identity(sorted(self.round_calls)) if self.round_calls else None
        if fingerprint is None and self.memory_only_round:
            self.memory_only_round = False
            return self.repeated_tool_rounds
        self.memory_only_round = False
        if fingerprint is None:
            self.repeated_tool_rounds = 0
        elif fingerprint == self.previous_tool_round:
            self.repeated_tool_rounds += 1
        else:
            self.repeated_tool_rounds = 1
        self.previous_tool_round = fingerprint
        self.round_calls.clear()
        return self.repeated_tool_rounds

    def stage(self, ref: SourceRef):
        for handle, issued in {**self.visible, **self.pending}.items():
            if issued.id == ref.id:
                return handle
        self.source_counter += 1
        handle = f"S{self.source_counter}"
        self.pending[handle] = ref
        return handle

    def resolve(self, handles):
        if len(set(handles)) != len(handles) or any(handle not in self.visible for handle in handles):
            raise ValueError("source_not_visible_in_this_attempt")
        return [self.visible[handle] for handle in handles]

    async def acknowledge(self, pending, hunks):
        await asyncio.to_thread(self.provider.store.deliver, self.provider.run.id, self.work,
            list(hunks), self.material_failures, self.provider.lease,
            source_ids=[ref.id for ref in pending.values()], digests=hunks)
        self.visible.update(pending)
        self.delivered_hunks.update(hunks)
        self.visible_receipts.update(self.receipts)
        for ref in pending.values():
            self.source_paths.add(ref.path)
        for handle in pending:
            self.pending.pop(handle, None)
        for key in hunks:
            self.pending_hunks.pop(key, None)
            self.material_failures.pop(key, None)

    async def read(self, path, start, end, revision):
        result = await self.evidence.read(path, start, end, revision)
        ref = SourceRef.model_validate(result.pop("evidence"))
        handle = self.stage(ref)
        return {**result, "source_ref": handle, "source": ref.model_dump()}


@dataclass
class ContextProvider:
    run: Any
    evidence: Any
    store: Any
    lease: dict
    denied_paths: frozenset[str] = frozenset()
    discussions: list[dict] = field(default_factory=list)
    index: Any = None
    _member_index: dict | None = field(default=None, init=False, repr=False, compare=False)
    _member_snapshot: Snapshot | None = field(default=None, init=False, repr=False, compare=False)
    _member_changes: list | None = field(default=None, init=False, repr=False, compare=False)

    def material_capacity(self) -> int:
        return unit_capacity(self.run.configuration.options.limits)

    @property
    def _member_map(self):
        changes = self.run.changed_files
        if self._member_index is None:
            self._member_snapshot = self.run.snapshot.model_copy(deep=True)
            self._member_index = members(changes)
        elif (self.run.snapshot != self._member_snapshot
                or (changes is not self._member_changes and changes != self._member_changes)):
            raise ValueError("fixed_snapshot_inputs_changed")
        # Frozen diff inputs are immutable; compare content only when the list is replaced.
        self._member_changes = changes
        return self._member_index

    def _member(self, key):
        return deepcopy(self._member_map[key])

    async def material(self, context, key):
        member = self._member(key)
        references = []
        for side, number_key, path in (
            ("base", "old_line_no", member["old_path"]),
            ("head", "new_line_no", member["path"]),
        ):
            lines = [line for line in member["lines"] if line[number_key] is not None]
            if not lines:
                continue
            first, last = lines[0][number_key], lines[-1][number_key]
            result = await self.evidence.read(path, first, last, side)
            actual = source_lines(result["content"])
            for line in lines:
                if actual[line[number_key] - first].rstrip("\n") != line["content"]:
                    raise ValueError("diff_blob_mismatch")
            ref = SourceRef.model_validate(result["evidence"])
            references.append((ref, result["content"]))
        if not member["lines"]:
            git, repo, snap = self.evidence.git, self.evidence.repo_path, self.run.snapshot
            before = await asyncio.to_thread(git.get_tree_entries, repo, snap.base_sha, [member["old_path"]])
            after = await asyncio.to_thread(git.get_tree_entries, repo, snap.head_sha, [member["path"]])
            if not before and not after:
                raise ValueError("structural_member_unavailable")
            member = {**member, "base_entry": before, "head_entry": after}
        packet = {"member": member, "sources": [
            {"source": ref.model_dump(), "content": content} for ref, content in references]}
        if tokens(packet) > self.material_capacity():
            raise ValueError("material_capacity")
        for item, (ref, _) in zip(packet["sources"], references):
            item["source_ref"] = context.stage(ref)
        context.pending_hunks[key] = identity(packet)
        return packet

    async def initial(self, context, candidate=None, *, request_budget=None, estimate_request=None):
        unit = next(unit for unit in self.run.plan.units if unit.id == context.work.unit_id)
        saved = await asyncio.to_thread(self.store.candidates, self.run.id)
        context.submitted = {
            item.id: {"id": item.id, "claim": item.proposal.claim}
            for item in saved if item.unit_id == unit.id}
        material, candidate_materials = [], []
        missing_candidate_materials = [
            {"path": ref.path, "side": ref.side, "start_line": ref.start_line,
             "end_line": ref.end_line, "reason": "material_capacity"}
            for ref in candidate.sources] if candidate else []
        omissions = list(missing_candidate_materials)
        context.material_failures.update({key: "material_capacity" for key in unit.required_hunk_ids})
        base = {
            "snapshot": self.run.snapshot.model_dump(), "depth": self.run.configuration.options.depth,
            "focus": unit.focus,
            "change_map": [{"paths": item.paths, "risk": item.risk} for item in self.run.plan.units][:100],
            "history_untrusted": [{"claim": item["claim"], "head_sha": item["head_sha"],
                "status": "historical_not_current_evidence"} for item in self.run.plan.history[:20]],
            "author_statement_untrusted": {
                "title": self.run.pr.title if self.run.pr else self.run.source.title,
                "body": (self.run.pr.body or "")[:4000] if self.run.pr else ""},
            "unit": unit.model_dump(), "materials": material, "missing_material": context.material_failures,
            "already_submitted": [{"id": item.id, "claim": item.proposal.claim}
                                  for item in saved if item.unit_id == unit.id][:100],
            "candidate": candidate.proposal.model_dump(exclude={"source_refs"}) if candidate else None,
            "candidate_materials": candidate_materials,
            "missing_candidate_materials": missing_candidate_materials,
        }
        def fits_request():
            return request_budget is None or estimate_request(base) <= request_budget

        def trim_background():
            # Frozen source takes precedence over author claims and broad orientation.
            if request_budget is None:
                return
            for key in ("already_submitted", "history_untrusted", "change_map"):
                while base[key] and not fits_request():
                    base[key] = base[key][:len(base[key]) // 2]
            for key in ("body", "title"):
                if not fits_request():
                    base["author_statement_untrusted"][key] = ""

        trim_background()
        if not fits_request():
            raise BudgetExceeded("input_token_limit")
        spent = 0
        if candidate:
            for ref, omission in zip(candidate.sources, omissions):
                before_pending = dict(context.pending)
                try:
                    item = await context.read(ref.path, ref.start_line, ref.end_line, ref.side)
                except (ValueError, OSError):
                    omission["reason"] = "material_unavailable"
                    continue
                cost = tokens(item)
                candidate_materials.append(item)
                missing_candidate_materials.remove(omission)
                trim_background()
                if ((request_budget is None and spent + cost > self.material_capacity())
                        or not fits_request()):
                    candidate_materials.pop()
                    context.pending = before_pending
                    missing_candidate_materials.append(omission)
                    continue
                spent += cost
        by_path = {}
        member_map = self._member_map
        for key in unit.required_hunk_ids:
            by_path.setdefault(member_map[key]["path"], []).append(key)
        # Give every related file a chance before consuming the remaining space.
        ordered_keys = [keys[index] for index in range(max(map(len, by_path.values()), default=0))
                        for keys in by_path.values() if index < len(keys)]
        for key in ordered_keys:
            before_pending = dict(context.pending)
            try:
                packet = await self.material(context, key)
            except (ValueError, OSError):
                context.material_failures[key] = "material_unavailable"
                continue
            cost = tokens(packet)
            material.append(packet)
            context.material_failures.pop(key, None)
            trim_background()
            if ((request_budget is None and spent + cost > self.material_capacity())
                    or not fits_request()):
                material.pop()
                context.pending_hunks.pop(key, None)
                context.pending = before_pending
                context.material_failures[key] = "material_capacity"
                continue
            spent += cost
        if not fits_request():
            raise BudgetExceeded("input_token_limit")
        return base
