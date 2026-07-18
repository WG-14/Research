"""Guarded web adapter for authoritative human-review and approval services."""

from __future__ import annotations

from typing import Any, Literal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from market_research.application import (
    ActorContext,
    GovernanceSubjectRef,
    HumanReviewRequest,
    RequestedChange,
    ResearchGovernanceApplicationService,
    StrategyApprovalRequest,
)
from market_research.application.adapter_contracts import (
    GovernanceError,
    GovernanceSubjectType,
    StrategyCandidateLifecycleState,
    content_hash_payload,
    governance_registry_path,
    load_governance_rows,
    sha256_prefixed,
    validate_governance_registry,
)

from .audit import record_web_audit_event
from .models import (
    GovernanceDecision,
    GovernanceDutyClaim,
    GovernanceSubjectState,
    ResearchJob,
)
from .security import actor_snapshot
from .storage import resolve_artifact_ref, verify_result_artifact


def load_review_context(job: ResearchJob) -> dict[str, Any]:
    report = _verified_pass_report(job)
    subject, rows, candidate_state = _approval_ready_subject(
        report,
        reviewed_artifact_hash=job.result_hash,
    )
    prior_reviews = tuple(
        row
        for row in rows
        if row.get("event_type") == "human_review_decision"
        and row.get("subject_type") == subject.subject_type
        and row.get("subject_id") == subject.subject_id
        and row.get("subject_version") == subject.subject_version
        and row.get("reviewed_artifact_hash") == job.result_hash
    )
    authoritative_state = GovernanceSubjectState.objects.filter(
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        subject_version=subject.subject_version,
    ).first()
    if authoritative_state is not None:
        if authoritative_state.reviewed_artifact_hash != job.result_hash:
            raise ValidationError("governance_subject_report_conflict")
        candidate_state = authoritative_state.lifecycle_state
        prior_reviews = tuple(
            {
                "event_type": "human_review_decision",
                "decision": decision.decision,
                "reviewer_id": decision.actor_id,
                "reviewer_role": decision.actor_role,
                "rationale": decision.rationale,
                "reviewed_artifact_hash": decision.reviewed_artifact_hash,
                "row_hash": decision.review_row_hash,
                "subject_type": subject.subject_type,
                "subject_id": subject.subject_id,
                "subject_version": subject.subject_version,
            }
            for decision in authoritative_state.decisions.all()
        )
    return {
        "report": report,
        "subject": subject,
        "prior_reviews": prior_reviews,
        "candidate_state": candidate_state,
        "approval_ready": (
            candidate_state
            == StrategyCandidateLifecycleState.OUT_OF_SAMPLE_PASSED.value
        ),
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
    raw_decision = str(cleaned_data["decision"])
    decision: Literal["CHANGES_REQUESTED", "REJECTED"]
    if raw_decision == "CHANGES_REQUESTED":
        decision = "CHANGES_REQUESTED"
    elif raw_decision == "REJECTED":
        decision = "REJECTED"
    else:
        raise ValidationError("human_review_decision_invalid")
    requested_changes: tuple[RequestedChange, ...] = ()
    if decision == "CHANGES_REQUESTED":
        requested_changes = (
            RequestedChange(
                requirement_id=str(cleaned_data["requirement_id"]),
                description=str(cleaned_data["change_description"]),
                verification_condition=str(cleaned_data["verification_condition"]),
            ),
        )
    rationale = str(cleaned_data["rationale"])
    operation_id = _operation_id(correlation_id)
    operation_payload_hash = _governance_command_hash(
        {
            "action": GovernanceDecision.Action.REVIEW,
            "operation_id": operation_id,
            "actor_id": actor.actor_id,
            "subject": subject.model_dump(mode="json"),
            "decision": decision,
            "rationale": rationale,
            "reviewed_artifact_hash": job.result_hash,
            "requested_changes": tuple(
                change.model_dump(mode="json") for change in requested_changes
            ),
        }
    )
    with transaction.atomic():
        state = _locked_subject_state(job=job, context=context)
        replay = _idempotent_decision(
            operation_id=operation_id,
            operation_payload_hash=operation_payload_hash,
            action=GovernanceDecision.Action.REVIEW,
        )
        if replay is not None:
            return _review_result(replay, subject)
        if (
            state.lifecycle_state
            != GovernanceSubjectState.LifecycleState.OUT_OF_SAMPLE_PASSED
        ):
            raise GovernanceError("human_review_candidate_lifecycle_not_reviewable")
        _claim_originator_duties(state, job)
        _claim_duty(
            state,
            actor_id=actor.actor_id,
            duty=GovernanceDutyClaim.Duty.REVIEWER,
        )
        result = ResearchGovernanceApplicationService(
            settings.RESEARCH_PATHS
        ).record_review(
            HumanReviewRequest(
                request_id=correlation_id,
                idempotency_key=operation_id,
                actor=actor,
                subject=subject,
                decision=decision,
                rationale=rationale,
                reviewed_artifact_hash=job.result_hash,
                requested_changes=requested_changes,
                prohibited_actor_ids=_originator_actor_ids(job),
            )
        )
        try:
            authoritative = GovernanceDecision.objects.create(
                subject=state,
                operation_id=operation_id,
                operation_payload_hash=operation_payload_hash,
                action=GovernanceDecision.Action.REVIEW,
                decision=result.decision,
                actor_id=actor.actor_id,
                actor_role=result.reviewer_role,
                rationale=rationale,
                reviewed_artifact_hash=job.result_hash,
                content_hash=result.row_hash,
                review_row_hash=result.row_hash,
            )
        except IntegrityError as exc:
            raise GovernanceError("governance_operation_id_conflict") from exc
        record_web_audit_event(
            action="research_human_review_recorded",
            actor_id=actor.actor_id,
            object_type="governance_decision",
            object_id=str(authoritative.pk),
            correlation_id=correlation_id,
            details={
                "decision": result.decision,
                "research_job_id": str(job.pk),
                "source_result_hash": job.result_hash,
                "review_row_hash": result.row_hash,
                "subject_id": result.subject.subject_id,
                "subject_version": result.subject.subject_version,
            },
        )
        return _review_result(authoritative, subject)


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
        if str(row.get("reviewer_id") or "") and row.get("decision") != "APPROVED"
    }
    actor = _actor(user)
    source_path = resolve_artifact_ref(job.result_ref)
    target = settings.RESEARCH_PATHS.report_path(
        "_internal_web",
        "governance",
        str(job.pk),
        "strategy_approval.json",
    )
    approval_request_id = _operation_id(
        cleaned_data.get("approval_request_id") or correlation_id
    )
    rationale = str(cleaned_data["rationale"])
    resolved_requirement_ids = tuple(cleaned_data.get("resolved_requirement_ids") or ())
    operation_payload_hash = _governance_command_hash(
        {
            "action": GovernanceDecision.Action.APPROVAL,
            "operation_id": approval_request_id,
            "actor_id": actor.actor_id,
            "subject": subject.model_dump(mode="json"),
            "rationale": rationale,
            "resolved_requirement_ids": resolved_requirement_ids,
            "reviewed_artifact_hash": job.result_hash,
        }
    )
    with transaction.atomic():
        state = _locked_subject_state(job=job, context=context)
        replay = _idempotent_decision(
            operation_id=approval_request_id,
            operation_payload_hash=operation_payload_hash,
            action=GovernanceDecision.Action.APPROVAL,
        )
        if replay is not None:
            return _approval_result(replay, subject)
        if (
            state.lifecycle_state
            != GovernanceSubjectState.LifecycleState.OUT_OF_SAMPLE_PASSED
        ):
            raise GovernanceError("strategy_candidate_already_approved")
        _claim_originator_duties(state, job)
        _claim_duty(
            state,
            actor_id=actor.actor_id,
            duty=GovernanceDutyClaim.Duty.APPROVER,
        )
        result = ResearchGovernanceApplicationService(
            settings.RESEARCH_PATHS
        ).approve_candidate(
            StrategyApprovalRequest(
                request_id=correlation_id,
                idempotency_key=approval_request_id,
                actor=actor,
                source_report_path=str(source_path),
                subject_version=subject.subject_version,
                rationale=rationale,
                resolved_requirement_ids=resolved_requirement_ids,
                output_path=str(target),
                expected_source_report_hash=job.result_hash,
                prohibited_actor_ids=(
                    _originator_actor_ids(job) | frozenset(prior_reviewer_ids)
                ),
            )
        )
        if not result.content_hash:
            raise GovernanceError("strategy_candidate_approval_hash_missing")
        try:
            authoritative = GovernanceDecision.objects.create(
                subject=state,
                operation_id=approval_request_id,
                operation_payload_hash=operation_payload_hash,
                action=GovernanceDecision.Action.APPROVAL,
                decision=GovernanceDecision.Decision.APPROVED,
                actor_id=actor.actor_id,
                actor_role="research_approver",
                rationale=rationale,
                reviewed_artifact_hash=job.result_hash,
                content_hash=result.content_hash,
                review_row_hash=result.review_row_hash,
                transition_row_hash=result.transition_row_hash,
                approval_artifact_ref=str(target),
            )
        except IntegrityError as exc:
            raise GovernanceError("strategy_candidate_approval_conflict") from exc
        updated = GovernanceSubjectState.objects.filter(
            pk=state.pk,
            lifecycle_state=GovernanceSubjectState.LifecycleState.OUT_OF_SAMPLE_PASSED,
            lifecycle_version=state.lifecycle_version,
        ).update(
            lifecycle_state=GovernanceSubjectState.LifecycleState.RESEARCH_APPROVED,
            lifecycle_version=F("lifecycle_version") + 1,
            approved_by=actor.actor_id,
            approved_at=timezone.now(),
            updated_at=timezone.now(),
        )
        if updated != 1:
            raise GovernanceError("strategy_candidate_lifecycle_version_conflict")
        record_web_audit_event(
            action="research_candidate_approved",
            actor_id=actor.actor_id,
            object_type="governance_decision",
            object_id=str(authoritative.pk),
            correlation_id=correlation_id,
            details={
                "research_job_id": str(job.pk),
                "source_result_hash": job.result_hash,
                "approval_hash": result.content_hash,
                "review_row_hash": result.review_row_hash,
                "transition_row_hash": result.transition_row_hash,
                "subject_id": result.subject.subject_id,
                "subject_version": result.subject.subject_version,
                "lifecycle_version": state.lifecycle_version + 1,
            },
        )
        return _approval_result(authoritative, subject)


def _locked_subject_state(
    *,
    job: ResearchJob,
    context: dict[str, Any],
) -> GovernanceSubjectState:
    subject: GovernanceSubjectRef = context["subject"]
    observed_state = str(
        context.get("candidate_state")
        or StrategyCandidateLifecycleState.OUT_OF_SAMPLE_PASSED.value
    )
    if observed_state not in {
        StrategyCandidateLifecycleState.OUT_OF_SAMPLE_PASSED.value,
        StrategyCandidateLifecycleState.RESEARCH_APPROVED.value,
    }:
        raise GovernanceError("governance_candidate_not_approval_ready")
    identity = {
        "subject_type": subject.subject_type,
        "subject_id": subject.subject_id,
        "subject_version": subject.subject_version,
    }
    existing = GovernanceSubjectState.objects.filter(**identity).first()
    if existing is None:
        try:
            state, _created = GovernanceSubjectState.objects.get_or_create(
                **identity,
                defaults={
                    "source_job": job,
                    "reviewed_artifact_hash": job.result_hash,
                    "lifecycle_state": (
                        GovernanceSubjectState.LifecycleState.OUT_OF_SAMPLE_PASSED
                    ),
                },
            )
        except IntegrityError:
            state = GovernanceSubjectState.objects.get(**identity)
    else:
        state = existing
    state = GovernanceSubjectState.objects.select_for_update().get(pk=state.pk)
    if state.reviewed_artifact_hash != job.result_hash:
        raise GovernanceError("governance_subject_report_conflict")
    return state


def _claim_originator_duties(
    state: GovernanceSubjectState,
    job: ResearchJob,
) -> None:
    for actor_id in sorted(_originator_actor_ids(job)):
        _claim_duty(
            state,
            actor_id=actor_id,
            duty=GovernanceDutyClaim.Duty.ORIGINATOR,
        )


def _claim_duty(
    state: GovernanceSubjectState,
    *,
    actor_id: str,
    duty: str,
) -> GovernanceDutyClaim:
    normalized_actor_id = str(actor_id).strip()
    if not normalized_actor_id:
        raise GovernanceError("governance_duty_actor_required")
    existing = GovernanceDutyClaim.objects.filter(
        subject=state,
        actor_id=normalized_actor_id,
    ).first()
    if existing is not None:
        if existing.duty != duty:
            raise GovernanceError("governance_separation_of_duties_violation")
        return existing
    try:
        return GovernanceDutyClaim.objects.create(
            subject=state,
            actor_id=normalized_actor_id,
            duty=duty,
        )
    except IntegrityError as exc:
        raise GovernanceError("governance_separation_of_duties_violation") from exc


def _idempotent_decision(
    *,
    operation_id: str,
    operation_payload_hash: str,
    action: str,
) -> GovernanceDecision | None:
    existing = (
        GovernanceDecision.objects.select_for_update()
        .filter(operation_id=operation_id)
        .first()
    )
    if existing is None:
        return None
    if (
        existing.operation_payload_hash != operation_payload_hash
        or existing.action != action
    ):
        raise GovernanceError("governance_idempotency_payload_conflict")
    return existing


def _operation_id(value: object) -> str:
    operation_id = str(value).strip()
    if not operation_id or len(operation_id) > 128:
        raise GovernanceError("governance_operation_id_invalid")
    return operation_id


def _governance_command_hash(payload: dict[str, Any]) -> str:
    return sha256_prefixed(
        content_hash_payload(payload),
        label="internal_web_governance_command",
    )


def _review_result(
    decision: GovernanceDecision,
    subject: GovernanceSubjectRef,
) -> dict[str, Any]:
    return {
        "decision": decision.decision,
        "row_hash": decision.review_row_hash,
        "subject": subject,
    }


def _approval_result(
    decision: GovernanceDecision,
    subject: GovernanceSubjectRef,
) -> dict[str, Any]:
    return {
        "approval_hash": decision.content_hash,
        "review_row_hash": decision.review_row_hash,
        "transition_row_hash": decision.transition_row_hash,
        "subject": subject,
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
    *,
    reviewed_artifact_hash: str,
) -> tuple[GovernanceSubjectRef, list[dict[str, Any]], str]:
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
            and row.get("subject_type")
            == GovernanceSubjectType.STRATEGY_CANDIDATE.value
            and row.get("subject_id") == candidate_id
        ):
            states[str(row.get("subject_version") or "")] = str(
                row.get("to_state") or ""
            )
    approved_for_report = sorted(
        version
        for version, state in states.items()
        if version
        and state == StrategyCandidateLifecycleState.RESEARCH_APPROVED.value
        and any(
            row.get("event_type") == "human_review_decision"
            and row.get("decision") == "APPROVED"
            and row.get("subject_type")
            == GovernanceSubjectType.STRATEGY_CANDIDATE.value
            and row.get("subject_id") == candidate_id
            and str(row.get("subject_version") or "") == version
            and row.get("reviewed_artifact_hash") == reviewed_artifact_hash
            for row in rows
        )
    )
    approval_ready = sorted(
        version
        for version, state in states.items()
        if version
        and state == StrategyCandidateLifecycleState.OUT_OF_SAMPLE_PASSED.value
    )
    if len(approved_for_report) == 1 and not approval_ready:
        selected_version = approved_for_report[0]
    elif not approved_for_report and len(approval_ready) == 1:
        selected_version = approval_ready[0]
    else:
        raise ValidationError("governance_candidate_not_uniquely_approval_ready")
    return (
        GovernanceSubjectRef(
            subject_type="strategy_candidate",
            subject_id=candidate_id,
            subject_version=selected_version,
        ),
        rows,
        states[selected_version],
    )


def _originator_actor_ids(job: ResearchJob) -> frozenset[str]:
    return frozenset(
        value for value in (str(job.owner_id), str(job.actor_id or "").strip()) if value
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
