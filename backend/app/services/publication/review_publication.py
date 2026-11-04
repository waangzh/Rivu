"""Publisher-only validation for normalized, persisted Run findings."""

from app.review.evidence import EvidenceArchive
from app.review.findings import aggregate, finding_digest, verification_problem, verified_description
from app.services.review.artifact_store import LocalArtifactStore


def _same_refs(left, right):
    return [item.model_dump() for item in left] == [item.model_dump() for item in right]


def _valid_sources(archive, refs):
    if not refs:
        return False
    for source in refs:
        loaded, _ = archive.read_source(source.id)
        if loaded.model_dump() != source.model_dump():
            return False
    return True


def _valid_finding(run, candidate, finding, archive):
    verification = candidate.verification
    if candidate.admission_reason:
        return "candidate_admission_invalid"
    if verification is None or verification.verdict != "confirmed":
        return "verification_not_confirmed"
    if verification_problem(verification, candidate.verification_sources, run.changed_files):
        return "verification_admission_invalid"
    if verification.attribution not in {"introduced", "exposed"}:
        return "finding_not_publishable"
    if candidate.finding is None or candidate.finding.model_dump() != finding.model_dump():
        return "candidate_finding_mismatch"
    description = verified_description(candidate)
    if (finding.candidate_id != candidate.id or finding.claim != description.claim
            or finding.trigger != description.trigger or finding.impact != description.impact
            or finding.severity != verification.severity or finding.attribution != verification.attribution
            or finding.author_comment != verification.author_comment
            or finding.location != candidate.proposal.location
            or not finding.publishable):
        return "finding_fact_mismatch"
    if not _same_refs(finding.sources, candidate.verification_sources):
        return "finding_source_mismatch"
    if finding.admission_digest != finding_digest(candidate, run.snapshot):
        return "finding_digest_mismatch"
    if not (_valid_sources(archive, candidate.sources)
            and _valid_sources(archive, candidate.verification_sources)
            and _valid_sources(archive, finding.sources)):
        return "publication_evidence_invalid"
    return None


def _snapshot_matches(run):
    snapshot = run.snapshot
    if run.source:
        return (snapshot.review_id == run.id
            and snapshot.repository == f"local:{run.source.repository_key}"
            and snapshot.repository_id is None and snapshot.pr_number is None and run.pr is None)
    if run.pr is None:
        return False
    return (snapshot.review_id == run.id and snapshot.repository == f"{run.pr.owner}/{run.pr.repo}"
        and snapshot.repository_id is not None and snapshot.repository_id > 0
        and snapshot.pr_number == run.pr.number
        and snapshot.declared_base_sha == run.pr.base.sha and snapshot.head_sha == run.pr.head.sha)


def validated_questions(run, artifacts=None):
    """Questions need an independent sourced observation, never a raw proposal or failed admission."""
    report = run.report
    if (report is None or run.status not in {"completed", "partial"}
            or report.status not in {"completed", "partial"} or not _snapshot_matches(run)):
        return []
    if len({item.id for item in report.candidates}) != len(report.candidates):
        return []
    archive = EvidenceArchive(snapshot=run.snapshot, artifacts=artifacts or LocalArtifactStore())
    changed_paths = {path for item in run.changed_files for path in (item.file_path, item.old_file_path) if path}
    selected = []
    for candidate in report.candidates:
        output = candidate.verification
        if (candidate.admission_reason or candidate.finding is not None or output is None
                or output.verdict != "insufficient" or output.question is None
                or output.attribution == "pre_existing"
                or candidate.question_digest != finding_digest(candidate, run.snapshot)):
            continue
        refs = candidate.verification_sources
        location = candidate.proposal.location
        if (not any(ref.path in changed_paths for ref in refs)
                or (location and not all(any(
                    ref.path == location.path and ref.side == location.side
                    and ref.start_line <= location.line <= ref.end_line
                    for ref in sources) for sources in (candidate.sources, refs)))):
            continue
        try:
            if _valid_sources(archive, candidate.sources) and _valid_sources(archive, refs):
                selected.append(candidate)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(selected, key=lambda item: item.id)


def validated_refutations(run, artifacts=None):
    """A refutation can resolve a candidate only while its counterevidence remains valid."""
    if run.report is None or not _snapshot_matches(run):
        return set()
    archive = EvidenceArchive(snapshot=run.snapshot, artifacts=artifacts or LocalArtifactStore())
    valid = set()
    for candidate in run.report.candidates:
        output = candidate.verification
        if (output is None or output.verdict != "refuted" or candidate.admission_reason
                or candidate.finding is not None
                or verification_problem(output, candidate.verification_sources, run.changed_files)):
            continue
        try:
            if (_valid_sources(archive, candidate.sources)
                    and _valid_sources(archive, candidate.verification_sources)):
                valid.add(candidate.id)
        except (OSError, ValueError, TypeError):
            continue
    return valid


def validated_findings(run, artifacts=None):
    """Return only fully reconstructed publication facts; never replay old graphs."""
    report = run.report
    if report is None:
        return [], []
    snapshot = run.snapshot
    if not _snapshot_matches(run):
        return [], [(finding.candidate_id, "snapshot_mismatch") for finding in report.findings]
    archive = EvidenceArchive(snapshot=snapshot, artifacts=artifacts or LocalArtifactStore())
    candidates = {candidate.id: candidate for candidate in report.candidates}
    if len(candidates) != len(report.candidates):
        return [], [(candidate.id, "duplicate_candidate_conflict") for candidate in report.candidates]
    selected, withheld, seen = [], [], set()
    for finding in report.findings:
        if finding.id in seen or finding.candidate_id not in candidates:
            withheld.append((finding.candidate_id, "finding_identity_conflict"))
            continue
        seen.add(finding.id)
        try:
            reason = _valid_finding(run, candidates[finding.candidate_id], finding, archive)
        except (OSError, ValueError, TypeError):
            reason = "publication_evidence_invalid"
        if reason:
            withheld.append((finding.candidate_id, reason))
        else:
            selected.append(finding)
    valid_members = []
    accepted_ids = {finding.id for finding in selected}
    for candidate in report.candidates:
        if candidate.finding is None:
            continue
        if candidate.finding.id in seen:
            if candidate.finding.id in accepted_ids:
                valid_members.append(candidate)
            continue
        try:
            reason = _valid_finding(run, candidate, candidate.finding, archive)
        except (OSError, ValueError, TypeError):
            reason = "publication_evidence_invalid"
        if reason:
            withheld.append((candidate.id, reason))
        else:
            valid_members.append(candidate)
    canonical_ids = {finding.id for finding in aggregate(valid_members, report.finding_deduplication)}
    for candidate in report.candidates:
        if candidate.finding is not None and candidate.finding.id in canonical_ids and candidate.finding.id not in seen:
            withheld.append((candidate.id, "finding_missing_from_report"))
    return selected, sorted(set(withheld))
