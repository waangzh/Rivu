from app.review.findings import finding_digest
from app.services.publication.publication_planner import PublicationPlanner
from app.services.publication.review_publication import validated_findings


def test_normalized_confirmed_finding_is_rebuilt_from_candidate_and_evidence(normalized_run):
    run, artifacts = normalized_run
    accepted, withheld = validated_findings(run, artifacts)
    assert [item.id for item in accepted] == ["finding-candidate"]
    assert withheld == []
    plan = PublicationPlanner(artifacts).build(run)
    assert plan.report_version
    assert [item.finding_key for item in plan.inline_drafts] == ["finding-candidate"]
    assert plan.outcome.publishable_count == 1


def test_tampered_finding_is_withheld_instead_of_published(normalized_run):
    run, artifacts = normalized_run
    run.report.findings[0].impact = "forged"
    accepted, withheld = validated_findings(run, artifacts)
    assert accepted == []
    assert withheld == [("candidate", "finding_fact_mismatch")]


def test_report_regeneration_changes_plan_version_and_hash(normalized_run):
    run, artifacts = normalized_run
    first = PublicationPlanner(artifacts).build(run)
    run.report.reasons.append("regenerated")
    from app.review.planner import identity
    run.report.version = identity(run.report.model_dump())
    second = PublicationPlanner(artifacts).build(run)
    assert first.report_version != second.report_version
    assert first.model_dump(mode="json") != second.model_dump(mode="json")


def test_publisher_rejects_confirmed_finding_with_failed_admission(normalized_run):
    run, artifacts = normalized_run
    candidate = run.report.candidates[0]
    candidate.admission_reason = "missing_confirmed_fields"

    accepted, withheld = validated_findings(run, artifacts)

    assert accepted == []
    assert withheld == [("candidate", "candidate_admission_invalid")]


def test_publisher_rechecks_confirmed_fields_and_attribution_sources(normalized_run):
    run, artifacts = normalized_run
    candidate = run.report.candidates[0]
    finding = run.report.findings[0]

    candidate.verification = candidate.verification.model_copy(update={"reason": ""})
    accepted, withheld = validated_findings(run, artifacts)
    assert accepted == []
    assert withheld == [("candidate", "verification_admission_invalid")]

    candidate.verification = candidate.verification.model_copy(update={
        "reason": "Head-only evidence on a modified file",
        "source_refs": [candidate.verification_sources[1].id],
    })
    candidate.verification_sources = [candidate.verification_sources[1]]
    finding.sources = list(candidate.verification_sources)
    finding.admission_digest = finding_digest(candidate, run.snapshot)
    accepted, withheld = validated_findings(run, artifacts)

    assert accepted == []
    assert withheld == [("candidate", "verification_admission_invalid")]


def test_publisher_uses_server_resolved_sources_not_model_context_handles(normalized_run):
    run, artifacts = normalized_run
    candidate = run.report.candidates[0]
    finding = run.report.findings[0]
    candidate.proposal = candidate.proposal.model_copy(update={
        "source_refs": ["reviewer-head-handle"],
    })
    candidate.verification = candidate.verification.model_copy(update={
        "source_refs": ["verifier-base-handle", "verifier-head-handle"],
    })
    finding.admission_digest = finding_digest(candidate, run.snapshot)

    accepted, withheld = validated_findings(run, artifacts)

    assert [item.id for item in accepted] == [finding.id]
    assert withheld == []


def test_refined_local_failure_is_revalidated_without_overwriting_original_candidate(normalized_run):
    from app.review.domain import FailureDescription

    run, artifacts = normalized_run
    candidate = run.report.candidates[0]
    candidate.verification.verified_claim = FailureDescription(
        claim="Local return value is wrong", trigger="Call function directly", impact="Wrong return value")
    finding = candidate.finding
    finding.claim, finding.trigger, finding.impact = (
        candidate.verification.verified_claim.claim,
        candidate.verification.verified_claim.trigger,
        candidate.verification.verified_claim.impact,
    )
    finding.admission_digest = finding_digest(candidate, run.snapshot)
    accepted, withheld = validated_findings(run, artifacts)
    assert accepted == [finding] and withheld == []
    assert candidate.proposal.impact == "Incorrect result"
    finding.impact = candidate.proposal.impact
    accepted, withheld = validated_findings(run, artifacts)
    assert accepted == [] and withheld == [("candidate", "finding_fact_mismatch")]


def test_absent_optional_verifier_details_do_not_invalidate_existing_admission_digest(normalized_run):
    from app.review.planner import identity

    run, _ = normalized_run
    candidate = run.report.candidates[0]
    original_verification = candidate.verification.model_dump(exclude={"verified_claim", "question"})
    original_digest = identity({
        "snapshot": run.snapshot.model_dump(), "candidate_id": candidate.id,
        "proposal": candidate.proposal.model_dump(), "verification": original_verification,
        "sources": [ref.model_dump() for ref in candidate.verification_sources],
    })
    assert finding_digest(candidate, run.snapshot) == original_digest
