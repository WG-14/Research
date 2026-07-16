"""Guarded web adapter for authoritative human-review and approval services."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError

from market_research.application import (
    ActorContext,
    GovernanceSubjectRef,
    HumanReviewRequest,
    RequestedChange,
    ResearchGovernanceApplicationService,
    StrategyApprovalRequest,
)
from market_research.research.governance import (
    GovernanceSubjectType,
    StrategyCandidateLifecycleState,
    governance_registry_path,
    load_governance_rows,
    validate_governance_registry,
)

from .audit import append_web_audit_event
from .models import ResearchJob
from .security import actor_snapshot
from .storage import resolve_artifact_ref, verify_result_artifact


def load_review_context(job: ResearchJob) -> dict[str, Any]:
    report = _verified_pass_report(job)
    subject, rows = _approval_ready_subject(report)
    prior_reviews = tuple(
        row
        for row in rows
        if row.get("event_type") == "human_review_decision"
        and row.get("subject_type") == subject.subject_type
        and row.get("subject_id") == subject.subject_id
        and row.get("subject_version") == subject.subject_version
        and row.get("reviewed_artifact_hash") == job.result_hash
    )
    return {
        "report": report,
        "subject": subject,
        "prior_reviews": prior_reviews,
    }


def record_job_review(
    *,
    user: Any,
    job: ResearchJob,
    cleaned_data: dict[str, Any],
    correlation_id: str,
) -> dict[str, Any]:
    context = load_review_context(job)
    subject: GovernanceSubjectRef = context["subject"]
    actor = _actor(user)
    decision = str(cleaned_data["decision"])
    requested_changes: tuple[RequestedChange, ...] = ()
    if decision == "CHANGES_REQUESTED":
        requested_changes = (
            RequestedChange(
                requirement_id=str(cleaned_data["requirement_id"]),
                description=str(cleaned_data["change_description"]),
                verification_condition=str(cleaned_data["verification_condition"]),
            ),
        )
    result = ResearchGovernanceApplicationService(settings.RESEARCH_PATHS).record_review(
        HumanReviewRequest(
            request_id=correlation_id,
            actor=actor,
            subject=subject,
            decision=decision,
            rationale=str(cleaned_data["rationale"]),
            reviewed_artifact_hash=job.result_hash,
            requested_changes=requested_changes,
            prohibited_actor_ids=_originator_actor_ids(job),
        )
    )
    append_web_audit_event(
        action="research_human_review_recorded",
        actor_id=actor.actor_id,
        object_type="research_job",
        object_id=str(job.pk),
        correlation_id=correlation_id,
        details={
            "decision": result.decision,
            "source_result_hash": job.result_hash,
            "review_row_hash": result.row_hash,
            "subject_id": result.subject.subject_id,
            "subject_version": result.subject.subject_version,
        },
    )
    return {
        "decision": result.decision,
        "row_hash": result.row_hash,
        "subject": result.subject,
    }


def approve_job_candidate(
    *,
    user: Any,
    job: ResearchJob,
    cleaned_data: dict[str, Any],
    correlation_id: str,
) -> dict[str, Any]:
    context = load_review_context(job)
    subject: GovernanceSubjectRef = context["subject"]
    prior_reviewer_ids = {
        str(row.get("reviewer_id") or "")
        for row in context["prior_reviews"]
        if str(row.get("reviewer_id") or "")
    }
    actor = _actor(user)
    source_path = resolve_artifact_ref(job.result_ref)
    target = settings.RESEARCH_PATHS.report_path(
        "_internal_web",
        "governance",
        str(job.pk),
        "strategy_approval.json",
    )
    if target.exists():
        raise ValidationError("strategy_approval_output_already_exists")
    result = ResearchGovernanceApplicationService(settings.RESEARCH_PATHS).approve_candidate(
        StrategyApprovalRequest(
            request_id=correlation_id,
            actor=actor,
            source_report_path=str(source_path),
            subject_version=subject.subject_version,
            rationale=str(cleaned_data["rationale"]),
            resolved_requirement_ids=tuple(
                cleaned_data.get("resolved_requirement_ids") or ()
            ),
            output_path=str(target),
            expected_source_report_hash=job.result_hash,
            prohibited_actor_ids=(
                _originator_actor_ids(job) | frozenset(prior_reviewer_ids)
            ),
        )
    )
    append_web_audit_event(
        action="research_candidate_approved",
        actor_id=actor.actor_id,
        object_type="research_job",
        object_id=str(job.pk),
        correlation_id=correlation_id,
        details={
            "source_result_hash": job.result_hash,
            "approval_hash": result.content_hash,
            "review_row_hash": result.review_row_hash,
            "transition_row_hash": result.transition_row_hash,
            "subject_id": result.subject.subject_id,
            "subject_version": result.subject.subject_version,
        },
    )
    return {
        "approval_hash": result.content_hash,
        "review_row_hash": result.review_row_hash,
        "transition_row_hash": result.transition_row_hash,
        "subject": result.subject,
    }


def _verified_pass_report(job: ResearchJob) -> dict[str, Any]:
    if (
        job.capability_id != ResearchJob.Capability.VALIDATE
        or job.status != ResearchJob.Status.SUCCEEDED
        or job.research_outcome != ResearchJob.ResearchOutcome.PASS
        or not job.result_ref
        or not job.result_hash
    ):
        raise ValidationError("governance_source_job_not_passed_validation")
    report = verify_result_artifact(job.result_ref, expected_hash=job.result_hash)
    if (
        report.get("schema_version") != 3
        or report.get("artifact_type") != "validated_research_result"
        or report.get("content_hash") != job.result_hash
        or report.get("manifest_hash") != job.manifest.manifest_hash
        or report.get("experiment_id") != job.manifest.experiment_id
        or report.get("run_id") != job.run_id
        or report.get("end_to_end_validation_result") != "PASS"
    ):
        raise ValidationError("governance_source_report_binding_invalid")
    return report


def _approval_ready_subject(
    report: dict[str, Any],
) -> tuple[GovernanceSubjectRef, list[dict[str, Any]]]:
    candidate_id = str(report.get("selected_candidate_id") or "").strip()
    if not candidate_id:
        raise ValidationError("governance_selected_candidate_missing")
    validation = validate_governance_registry(settings.RESEARCH_PATHS)
    if validation.get("status") != "PASS":
        raise ValidationError("governance_registry_invalid")
    rows = load_governance_rows(governance_registry_path(settings.RESEARCH_PATHS))
    states: dict[str, str] = {}
    for row in rows:
        if (
            row.get("event_type") == "lifecycle_transition"
            and row.get("subject_type") == GovernanceSubjectType.STRATEGY_CANDIDATE.value
            and row.get("subject_id") == candidate_id
        ):
            states[str(row.get("subject_version") or "")] = str(
                row.get("to_state") or ""
            )
    eligible = sorted(
        version
        for version, state in states.items()
        if version
        and state == StrategyCandidateLifecycleState.OUT_OF_SAMPLE_PASSED.value
    )
    if len(eligible) != 1:
        raise ValidationError("governance_candidate_not_uniquely_approval_ready")
    return (
        GovernanceSubjectRef(
            subject_type="strategy_candidate",
            subject_id=candidate_id,
            subject_version=eligible[0],
        ),
        rows,
    )


def _originator_actor_ids(job: ResearchJob) -> frozenset[str]:
    return frozenset(
        value
        for value in (str(job.owner_id), str(job.actor_id or "").strip())
        if value
    )


def _actor(user: Any) -> ActorContext:
    actor_id, roles, permissions = actor_snapshot(user)
    return ActorContext(
        actor_id=actor_id,
        roles=tuple(roles),
        permissions=frozenset(permissions),
        source="web",
    )


__all__ = [
    "approve_job_candidate",
    "load_review_context",
    "record_job_review",
]
