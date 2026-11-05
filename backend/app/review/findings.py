"""Admission checks provenance and necessary fields, never semantic truth."""

from uuid import uuid4

from app.review.domain import Candidate, Finding, FindingGroup
from app.review.planner import identity


def admit_candidate(proposal, context, tool_call_id):
    refs = context.resolve(proposal.source_refs)
    snapshot = context.provider.run.snapshot
    if any(ref.review_id != snapshot.review_id or ref.commit_sha != (
            snapshot.head_sha if ref.side == "head" else snapshot.base_sha) for ref in refs):
        raise ValueError("candidate_snapshot_mismatch")
    if proposal.location and not location_sourced(proposal.location, refs):
        raise ValueError("candidate_location_not_sourced")
    if proposal.root_cause and not location_sourced(proposal.root_cause.location, refs):
        raise ValueError("root_cause_not_sourced")
    return Candidate(id=uuid4().hex, unit_id=context.work.unit_id,
        attempt_id=context.work.attempt_id, tool_call_id=tool_call_id,
        proposal=proposal, sources=refs,
        dependency_sources=context.resolve_dependencies(proposal.dependency_refs))


def finding_digest(candidate, snapshot):
    proposal_excluded = {"root_cause"} if candidate.proposal.root_cause is None else set()
    if not candidate.proposal.dependency_refs:
        proposal_excluded.add("dependency_refs")
    return identity({"snapshot": snapshot.model_dump(), "candidate_id": candidate.id,
        "proposal": candidate.proposal.model_dump(exclude=proposal_excluded),
        "verification": candidate.verification.model_dump(exclude_none=True,
            exclude={"dependency_refs"} if not candidate.verification.dependency_refs else set()),
        "sources": [ref.model_dump() for ref in candidate.verification_sources],
        **({"dependency_sources": [ref.model_dump() for ref in candidate.verification_dependency_sources]}
           if candidate.verification_dependency_sources else {}),
        **({"proposal_dependency_sources": [ref.model_dump() for ref in candidate.dependency_sources]}
           if candidate.dependency_sources else {})})


def verified_description(candidate):
    return candidate.verification.verified_claim or candidate.proposal


def location_sourced(location, refs):
    return any(ref.path == location.path and ref.side == location.side
               and ref.start_line <= location.line <= ref.end_line for ref in refs)


def candidate_summary(candidate):
    output = candidate.verification
    root = output.root_cause if output and output.verdict == "confirmed" else candidate.proposal.root_cause
    return {"id": candidate.id, "claim": candidate.proposal.claim[:300],
            "trigger": candidate.proposal.trigger[:300], "impact": candidate.proposal.impact[:300],
            "location": candidate.proposal.location.model_dump() if candidate.proposal.location else None,
            "root_cause": root.model_dump() if root else None,
            "status": output.verdict if output else "unverified"}


def verification_problem(output, refs, changed_files):
    """Return the admission failure code for a verifier output, if any."""
    if not refs and (output.verdict in {"confirmed", "refuted"}
                     or (output.verdict == "insufficient" and output.question is not None)):
        return "missing_decision_sources"
    if output.verdict == "confirmed":
        if output.root_cause and not location_sourced(output.root_cause.location, refs):
            return "root_cause_not_sourced"
        if (output.severity is None or not output.reason.strip()
                or not output.author_comment or not output.author_comment.strip()):
            return "missing_confirmed_fields"
        if output.attribution in {"introduced", "exposed"}:
            changes = {item.file_path: item for item in changed_files}
            causal = [ref for ref in refs if ref.path in changes
                      or any(item.old_file_path == ref.path for item in changed_files)]
            added = any(changes.get(ref.path) and changes[ref.path].change_type == "added"
                        and ref.side == "head" for ref in causal)
            if not added and not {"base", "head"} <= {ref.side for ref in causal}:
                return "missing_change_attribution_sources"
    return None


def admit_verification(candidate, output, context):
    run = context.provider.run
    try:
        refs = context.resolve(output.source_refs)
        dependencies = context.resolve_dependencies(output.dependency_refs)
    except ValueError:
        # A model-supplied handle is untrusted; keep the verification as an
        # admission failure instead of letting it abort the whole work item.
        refs, dependencies, problem = [], [], "invalid_source_refs"
    else:
        problem = verification_problem(output, refs, run.changed_files)
    candidate.verification = output.model_copy(update={
        "verdict": "insufficient", "severity": None, "author_comment": None,
        "reason": f"{problem}: {output.reason}"} if problem else {})
    candidate.verification_sources = refs
    candidate.verification_dependency_sources = dependencies
    candidate.admission_reason = problem
    candidate.decision_digest = finding_digest(candidate, run.snapshot) if not problem else None
    candidate.question_digest = (finding_digest(candidate, run.snapshot)
        if not problem and output.verdict == "insufficient" and output.question else None)
    candidate.finding = None
    if candidate.verification.verdict == "confirmed":
        description = verified_description(candidate)
        candidate.finding = Finding(id=f"finding-{candidate.id}", candidate_id=candidate.id,
            claim=description.claim, trigger=description.trigger,
            impact=description.impact, severity=output.severity,
            attribution=output.attribution, author_comment=output.author_comment,
            location=candidate.proposal.location, sources=refs,
            publishable=output.attribution in {"introduced", "exposed"},
            admission_digest=finding_digest(candidate, run.snapshot))
    return candidate


def confirmed_candidate(candidate):
    return (candidate.finding is not None and not candidate.admission_reason
            and candidate.verification is not None
            and candidate.verification.verdict == "confirmed")


def exact_group_key(candidate):
    finding = candidate.finding
    root = candidate.verification.root_cause
    if (root and location_sourced(root.location, candidate.verification_sources)
            and (finding.location is None
                 or location_sourced(finding.location, candidate.verification_sources))):
        origins = sorted({(ref.review_id, ref.commit_sha) for ref in candidate.verification_sources
                          if location_sourced(root.location, [ref])})
        return identity(["root_cause", root.model_dump(), origins, finding.severity,
                         finding.attribution, finding.publishable])
    return identity(["exact", finding.claim, finding.trigger, finding.impact, finding.severity,
                     finding.attribution, finding.author_comment,
                     finding.location.model_dump() if finding.location else None,
                     sorted(ref.id for ref in finding.sources)])


def comparison_valid(comparison, candidates):
    if len(set(comparison.candidate_ids)) != 2:
        return False
    members = [candidates.get(key) for key in comparison.candidate_ids]
    if any(item is None or not confirmed_candidate(item) for item in members):
        return False
    if tuple(item.finding.admission_digest for item in members) != comparison.admission_digests:
        return False
    if len({(item.finding.attribution, item.finding.publishable) for item in members}) != 1:
        return False
    refs = [ref for item in members for ref in item.verification_sources]
    if (len({ref.review_id for ref in refs}) != 1
            or any(len({ref.commit_sha for ref in refs if ref.side == side}) > 1
                   for side in ("base", "head"))):
        return False
    cited = set(comparison.source_ids)
    if not cited or not cited <= {ref.id for ref in refs}:
        return False
    for item in members:
        sources = [ref for ref in item.verification_sources if ref.id in cited]
        locations = [item.finding.location]
        if item.verification.root_cause:
            locations.append(item.verification.root_cause.location)
        if not sources or any(location and not location_sourced(location, sources)
                              for location in locations):
            return False
    return True


def group_findings(candidates, deduplication=None):
    eligible = [item for item in candidates if confirmed_candidate(item)]
    by_id = {item.id: item for item in eligible}
    decisions, conflicts = {}, set()
    if deduplication and deduplication.status == "ready":
        for comparison in deduplication.comparisons:
            if comparison_valid(comparison, by_id):
                pair = frozenset(comparison.candidate_ids)
                if pair in decisions:
                    conflicts.add(pair)
                decisions[pair] = comparison.verdict
    keys = {item.id: exact_group_key(item) for item in eligible}

    def equivalent(left, right):
        pair = frozenset((left.id, right.id))
        if pair in conflicts:
            return False
        if pair in decisions:
            return decisions[pair] == "same"
        return keys[left.id] == keys[right.id]

    groups = []
    for candidate in eligible:
        group = next((items for items in groups
                      if all(equivalent(candidate, member) for member in items)), None)
        if group is None:
            groups.append([candidate])
        else:
            group.append(candidate)
    return [FindingGroup(
        representative_id=next((item.finding.id for item in items
                                if item.finding.location and item.finding.location.side == "head"),
                               items[0].finding.id),
        member_candidate_ids=[item.id for item in items]) for items in groups]


def aggregate(candidates, deduplication=None):
    by_id = {item.finding.id: item.finding for item in candidates if item.finding}
    return [by_id[group.representative_id] for group in group_findings(candidates, deduplication)]
