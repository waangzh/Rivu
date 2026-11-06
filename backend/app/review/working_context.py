"""Cache-stable context epochs over immutable evidence and an investigation ledger."""

import json
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage

from app.review.context import TOOL_LOOP_WARNING
from app.review.planner import tokens


PERFORMANCE_INPUT_TOKENS = 128_000
MIN_RECLAIM_TOKENS = 8_192


def packet_message(packet, name):
    return HumanMessage(content=json.dumps(packet, ensure_ascii=False), id=name)


@dataclass
class WorkingContext:
    prefix: list = field(default_factory=list)
    cutoff: int = 0
    epoch: int = 0
    retained: set[str] = field(default_factory=set)
    checkpoint_epoch: int = -1
    checkpoint_revision: int | None = None
    metrics: dict = field(default_factory=dict)
    previous_projection: list = field(default_factory=list)
    slow_responses: int = 0

    @property
    def checkpoint_pending(self):
        return self.checkpoint_revision is not None

    @staticmethod
    def initial_ceiling(limits):
        return max(1, limits.context_tokens - limits.output_tokens - 1024)

    @classmethod
    def initial_budget(cls, limits):
        return cls.initial_ceiling(limits)

    @staticmethod
    def active_refs(context):
        questions = context.investigations
        pending = [key for key, note in questions.items() if note.status in {"open", "blocked"}]
        visited, refs = set(), set(context.pending)
        while pending:
            key = pending.pop()
            if key in visited or key not in questions:
                continue
            visited.add(key)
            note = questions[key]
            refs.update(note.source_refs)
            refs.update(note.counterevidence_refs)
            pending.extend(note.depends_on)
        return refs

    def observe_response(self, input_tokens, output_tokens, elapsed_seconds, timeout_seconds):
        short_and_slow = (
            input_tokens is not None and input_tokens >= PERFORMANCE_INPUT_TOKENS
            and output_tokens is not None and output_tokens <= 512
            and elapsed_seconds >= timeout_seconds / 2
        )
        self.slow_responses = self.slow_responses + 1 if short_and_slow else 0

    @classmethod
    def required_refs(cls, context, catalog):
        active = cls.active_refs(context)
        if context.work.kind == "verification":
            active.update(item["source_ref"] for item in context.initial_packet.get("candidate_materials", []))
        active_sources = [catalog[handle][1]["source"] for handle in active if handle in catalog]
        active.update(handle for handle, (_, item) in catalog.items() if any(
            item["source"]["path"] == source["path"]
            and item["source"]["side"] != source["side"]
            and item["source"]["start_line"] <= source["end_line"]
            and source["start_line"] <= item["source"]["end_line"]
            for source in active_sources))
        return active

    @staticmethod
    def source_catalog(context, messages):
        catalog = {}
        for position, message in enumerate(messages):
            if message.type not in {"human", "tool"}:
                continue
            try:
                payload = json.loads(message.content)
            except (ValueError, TypeError):
                continue
            for item, source in context._source_items(payload):
                handle = item.get("source_ref")
                if handle and not item.get("content_omitted"):
                    catalog[handle] = (position, {
                        "source_ref": handle, "source": source, "content": item["content"],
                    })
        return catalog

    def project(self, context, messages, estimate, input_limit, *, force=False):
        """Keep the existing prefix until enough space is reclaimed or capacity requires it."""
        base = [*self.prefix, *messages[self.cutoff:]] if self.epoch else messages
        projected = context.compact_messages(base)
        present_catalog = self.source_catalog(context, projected)
        restored = []
        if self.epoch:
            catalog = self.source_catalog(context, messages)
            live_sources = [item["source"] for _, item in present_catalog.values()]
            for handle in sorted(self.required_refs(context, catalog)):
                if handle not in catalog:
                    continue
                item = catalog[handle][1]
                source = item["source"]
                key = context._source_range(source)[:3]
                covered = any(
                    context._source_range(current)[:3] == key
                    and current["start_line"] <= source["start_line"]
                    and source["end_line"] <= current["end_line"]
                    for current in live_sources)
                if not covered:
                    restored.append(item)
                    live_sources.append(source)
            if restored:
                projected = context.compact_messages([
                    *projected, packet_message({"restored_sources": restored}, f"restore-{len(messages)}")])
                self.prefix, self.cutoff = projected, len(messages)
                present_catalog = self.source_catalog(context, projected)
                self.retained = set(present_catalog)
        before = estimate(projected)
        soft = input_limit * 3 // 4
        target = input_limit // 2
        reclaim = max(min(MIN_RECLAIM_TOKENS, input_limit // 4), before // 4)
        active = self.active_refs(context)
        closed = {handle for note in context.investigations.values()
                  if note.status in {"resolved", "not_applicable"}
                  for handle in [*note.source_refs, *note.counterevidence_refs]} - active
        capacity_pressure = before > soft
        latency_pressure = before >= PERFORMANCE_INPUT_TOKENS and self.slow_responses >= 2
        investigation_pressure = (
            before >= PERFORMANCE_INPUT_TOKENS and bool(closed)
            and context.repeated_tool_rounds >= TOOL_LOOP_WARNING
        )
        reason = ("capacity" if capacity_pressure else "latency" if latency_pressure else
                  "investigation" if investigation_pressure else None)
        if force and reason is None:
            reason = "admission"
        if reason is not None:
            target = min(target, before * 2 // 3)
        should_compact = force or reason is not None
        if self.checkpoint_pending and context.memory_revision > self.checkpoint_revision:
            self.checkpoint_revision = None
        checkpoint = (before > input_limit * 3 // 5 or reason is not None) \
            and self.checkpoint_epoch != self.epoch
        if checkpoint:
            self.checkpoint_epoch = self.epoch
            if not force and before < input_limit * 9 // 10:
                self.checkpoint_revision = context.memory_revision
        if self.checkpoint_pending and not force and before < input_limit:
            should_compact = False
        changed = False
        if should_compact:
            rebuilt, retained, cutoff = self.rebuild(context, messages, estimate, target, input_limit)
            after = estimate(rebuilt)
            if (force and after < before) or before - after >= reclaim:
                self.prefix, self.cutoff = rebuilt, cutoff
                self.retained = retained
                self.epoch += 1
                self.checkpoint_epoch = self.epoch
                projected = rebuilt
                changed = True
        after = estimate(projected) if changed else before
        shared = []
        for previous, current in zip(self.previous_projection, projected):
            if previous != current:
                break
            shared.append(current.content)
        original = estimate(messages)
        self.metrics = {
            "epoch": self.epoch, "compacted": changed,
            "original_input_tokens": original, "projected_input_tokens": after,
            "saved_input_tokens": original - after,
            "soft_input_tokens": soft, "target_input_tokens": target,
            "pressure": reason is not None, "pressure_reason": reason,
            "minimum_reclaim_tokens": reclaim, "slow_responses": self.slow_responses,
            "restored_sources": len(restored),
            "retained_sources": len(self.source_catalog(context, projected)) if changed else len(present_catalog),
            "checkpoint": checkpoint,
            "checkpoint_pending": self.checkpoint_pending,
            "shared_message_prefix_tokens": tokens(shared) if shared else 0,
        }
        self.previous_projection = projected
        return projected

    def rebuild(self, context, messages, estimate, target, input_limit):
        initial = context.initial_packet
        envelope = {key: value for key, value in initial.items()
                    if key not in {"materials", "candidate_materials", "already_submitted",
                                   "missing_material", "missing_candidate_materials"}}
        catalog = self.source_catalog(context, messages)
        active = self.required_refs(context, catalog)
        closed = {handle for note in context.investigations.values()
                  if note.status in {"resolved", "not_applicable"}
                  for handle in [*note.source_refs, *note.counterevidence_refs]} - active

        # Retain complete recent rounds, never an orphaned tool result.
        rounds = [index for index, message in enumerate(messages)
                  if isinstance(message, AIMessage) and message.tool_calls]
        cutoff = rounds[-2] if len(rounds) >= 2 else (rounds[0] if rounds else len(messages))
        recent = messages[cutoff:]
        recent_handles = {handle for handle, (position, _) in catalog.items() if position >= cutoff}
        inventory = [
            {"source_ref": handle, **{key: item["source"][key] for key in (
                "commit_sha", "side", "path", "start_line", "end_line")}}
            for handle, (_, item) in catalog.items()
        ]
        changes = []
        for material in initial.get("materials", []):
            member = material["member"]
            changes.append({key: member[key] for key in ("id", "path", "old_path")})
        receipts = [
            {"receipt_ref": handle, "tool": item["tool"], "arguments": item["arguments"],
             **item["summary"]}
            for handle, item in context.receipts.items()
            if item["tool"] in {"search_code", "find_relations", "find_files"}
        ]
        read_history = {}
        for handle, receipt in context.receipts.items():
            if (receipt["tool"] not in {"read_file", "read_symbol", "read_diff"}
                    or receipt["summary"].get("ok") is not True):
                continue
            key = json.dumps([receipt["tool"], receipt["arguments"]], sort_keys=True)
            entry = read_history.setdefault(key, {
                "tool": receipt["tool"], "arguments": receipt["arguments"],
                "received_count": 0, "last_receipt_ref": handle,
            })
            entry["received_count"] += 1
            entry["last_receipt_ref"] = handle
        memory = {
            "investigations_unverified": [
                note.model_dump(exclude_defaults=True) for note in context.investigations.values()],
            "submitted_candidates": list(context.submitted.values()),
            "source_inventory": inventory, "change_inventory": changes,
            "retrieval_receipts": receipts,
            "read_history": list(read_history.values()),
            "missing_material": context.material_failures,
            "notice": (
                "Only working_set and recent messages contain source bodies. Inventory and "
                "investigation notes are not evidence of a claim or completed review. "
                "read_file restores any listed fixed source; read_diff restores a change member; "
                "recall_context restores a receipt. Read history records received data, not "
                "answered questions; avoid rereading it without a specific missing premise. "
                "Search scope/partial flags still apply."
            ),
        }
        working = []
        prefix = [
            packet_message(envelope, "review-envelope"),
            packet_message(memory, f"investigation-memory-{self.epoch + 1}"),
            packet_message({"working_set": working}, f"working-set-{self.epoch + 1}"),
        ]

        def assemble():
            prefix[2] = packet_message({"working_set": working}, f"working-set-{self.epoch + 1}")
            return context.compact_messages([*prefix, *recent])

        retained = set(recent_handles)
        for handle in sorted(active - recent_handles):
            if handle in catalog:
                working.append(catalog[handle][1])
                retained.add(handle)
        # Critical premises can expand above the soft target, but not the request gate.
        required_size = estimate(assemble())
        optional_limit = max(target, required_size)
        size = required_size
        changed_handles = {item["source_ref"] for packet in initial.get("materials", [])
                           for item in packet["sources"]}
        order = sorted(set(catalog) - retained - closed, key=lambda handle: (
            handle not in changed_handles, -catalog[handle][0], handle))
        for handle in order:
            cost = tokens(catalog[handle][1])
            if size + cost <= min(optional_limit, input_limit):
                working.append(catalog[handle][1])
                retained.add(handle)
                size += cost
        return assemble(), retained, len(messages)
