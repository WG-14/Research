from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, QuerySet
from django.utils import timezone

from market_research.application.capabilities import (
    CapabilityExecutionMode,
    GuiPolicy,
    get_capability,
)
from market_research.application.contracts import (
    ResearchPreflightRequest,
    ResearchPreflightResult,
    ResearchValidationRequest,
    ResearchValidationResult,
)
from market_research.application.adapter_contracts import sha256_prefixed

from .audit import record_web_audit_event
from .models import ManifestUpload, ResearchJob
from .security import (
    actor_snapshot,
    can_view_all_jobs,
    reject_paths_in_job_payload,
    validate_sha256,
)
from .storage import SafeArtifactRef, verify_result_artifact


ACTIVE_STATUSES = (
    ResearchJob.Status.QUEUED,
    ResearchJob.Status.RUNNING,
    ResearchJob.Status.CANCEL_REQUESTED,
)
WEB_JOB_CAPABILITY_CONTRACTS = {
    ResearchJob.Capability.PREFLIGHT: (
        "ResearchApplicationService.preflight",
        ResearchPreflightRequest,
        ResearchPreflightResult,
    ),
    ResearchJob.Capability.VALIDATE: (
        "ResearchApplicationService.validate",
        ResearchValidationRequest,
        ResearchValidationResult,
    ),
}


class IdempotencyConflict(ValueError):
    pass


class ActiveJobConflict(RuntimeError):
    """A different active job already owns this user's execution slot."""

    code = "research_job_owner_active_conflict"

    def __init__(self, existing_job: ResearchJob) -> None:
        self.existing_job = existing_job
        self.job = existing_job
        super().__init__(self.code)


class JobLeaseLost(RuntimeError):
    pass


class JobCancellationRequested(RuntimeError):
    pass


def validate_web_job_capability_contract(capability_id: str) -> None:
    """Fail closed unless the core catalog exactly matches the web adapter."""

    expected = WEB_JOB_CAPABILITY_CONTRACTS.get(capability_id)
    if expected is None:
        raise ValidationError("research_job_capability_not_supported_by_web")
    try:
        specification = get_capability(capability_id)
    except KeyError as exc:
        raise ValidationError(
            "research_job_capability_missing_from_core_catalog"
        ) from exc
    service_id, request_model, result_model = expected
    if (
        specification.gui_policy is not GuiPolicy.REQUIRED
        or specification.execution_mode is not CapabilityExecutionMode.QUEUED
        or specification.permission != "research.execute"
        or specification.service_id != service_id
        or specification.request_model is not request_model
        or specification.result_model is not result_model
    ):
        raise ValidationError("research_job_capability_contract_mismatch")


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job: ResearchJob
    created: bool


@dataclass(frozen=True, slots=True)
class JobExecutionResult:
    result_ref: SafeArtifactRef
    result_hash: str
    run_id: str = ""
    research_outcome: str = ""


def canonical_job_request(
    *,
    capability_id: str,
    manifest: ManifestUpload,
    options: dict[str, Any] | None = None,
    source_preflight_job: ResearchJob | None = None,
) -> dict[str, Any]:
    validate_web_job_capability_contract(capability_id)
    normalized_options = dict(options or {})
    reject_paths_in_job_payload(normalized_options)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "capability_id": capability_id,
        "manifest_upload_id": str(manifest.pk),
        "manifest_content_hash": manifest.content_hash,
        "manifest_hash": manifest.manifest_hash,
        "options": normalized_options,
    }
    if capability_id == ResearchJob.Capability.VALIDATE:
        if source_preflight_job is None:
            raise ValidationError("validation_source_preflight_required")
        payload.update(
            {
                "source_preflight_job_id": str(source_preflight_job.pk),
                "source_preflight_request_hash": source_preflight_job.request_hash,
                "source_preflight_result_hash": source_preflight_job.result_hash,
            }
        )
    elif source_preflight_job is not None:
        raise ValidationError("source_preflight_only_valid_for_validation")
    return payload


def validate_source_preflight_evidence(
    *,
    manifest: ManifestUpload,
    source_preflight_job: ResearchJob | None,
    for_update: bool = False,
) -> ResearchJob:
    """Reload and verify the complete evidence chain used to unlock validation."""

    source_id = getattr(source_preflight_job, "pk", None)
    if source_id is None:
        raise ValidationError("validation_source_preflight_required")
    queryset = ResearchJob.objects.select_related("manifest")
    if for_update:
        queryset = queryset.select_for_update()
    try:
        source = queryset.get(pk=source_id)
    except ResearchJob.DoesNotExist as exc:
        raise ValidationError("validation_source_preflight_missing") from exc
    if source.capability_id != ResearchJob.Capability.PREFLIGHT:
        raise ValidationError("validation_source_preflight_capability_invalid")
    if source.status != ResearchJob.Status.SUCCEEDED:
        raise ValidationError("validation_source_preflight_not_succeeded")
    if source.research_outcome != ResearchJob.ResearchOutcome.PASS:
        raise ValidationError("validation_source_preflight_not_pass")
    if source.manifest_id != manifest.pk:
        raise ValidationError("validation_source_preflight_manifest_mismatch")
    if not source.result_ref or not source.result_hash:
        raise ValidationError("validation_source_preflight_result_missing")

    request_payload = source.request_payload
    if not isinstance(request_payload, dict) or not isinstance(
        request_payload.get("options", {}), dict
    ):
        raise ValidationError("validation_source_preflight_request_invalid")
    expected_source_request = canonical_job_request(
        capability_id=ResearchJob.Capability.PREFLIGHT,
        manifest=manifest,
        options=request_payload.get("options", {}),
    )
    expected_source_hash = sha256_prefixed(
        expected_source_request,
        label="internal_web_job_request",
    )
    if (
        request_payload != expected_source_request
        or source.request_hash != expected_source_hash
    ):
        raise ValidationError("validation_source_preflight_request_binding_invalid")

    result_payload = verify_result_artifact(
        source.result_ref,
        expected_hash=source.result_hash,
    )
    expected_result_bindings = {
        "report_kind": "internal_web_preflight",
        "capability_id": "research-preflight",
        "request_hash": source.request_hash,
        "manifest_hash": manifest.manifest_hash,
        "manifest_content_hash": manifest.content_hash,
        "status": ResearchJob.ResearchOutcome.PASS,
    }
    if any(
        result_payload.get(key) != value
        for key, value in expected_result_bindings.items()
    ):
        raise ValidationError("validation_source_preflight_result_binding_invalid")
    return source


def validate_validation_job_gate(job: ResearchJob) -> ResearchJob:
    """Revalidate persisted validation intent immediately before execution."""

    if job.capability_id != ResearchJob.Capability.VALIDATE:
        raise ValidationError("validation_job_capability_invalid")
    source = validate_source_preflight_evidence(
        manifest=job.manifest,
        source_preflight_job=job.source_preflight_job,
    )
    expected_request = canonical_job_request(
        capability_id=job.capability_id,
        manifest=job.manifest,
        options=(
            job.request_payload.get("options", {})
            if isinstance(job.request_payload, dict)
            else {}
        ),
        source_preflight_job=source,
    )
    expected_hash = sha256_prefixed(
        expected_request,
        label="internal_web_job_request",
    )
    if job.request_payload != expected_request or job.request_hash != expected_hash:
        raise ValidationError("validation_job_request_binding_invalid")
    return source


def enqueue_research_job(
    *,
    owner: Any,
    manifest: ManifestUpload,
    capability_id: str,
    idempotency_key: str,
    options: dict[str, Any] | None = None,
    correlation_id: str | uuid.UUID | None = None,
    source_preflight_job: ResearchJob | None = None,
) -> EnqueueResult:
    if not owner.has_perm("portal.submit_research_job"):
        raise PermissionDenied("research_job_submit_permission_required")
    if manifest.owner_id != owner.pk and not owner.has_perm(
        "portal.view_all_research_manifests"
    ):
        raise PermissionDenied("research_manifest_access_denied")
    key = str(idempotency_key or "").strip()
    if not key or len(key) > 64:
        raise ValidationError("idempotency_key_invalid")
    actor_id, roles, permissions = actor_snapshot(owner)
    cid = uuid.UUID(str(correlation_id)) if correlation_id else uuid.uuid4()

    try:
        with transaction.atomic():
            verified_source = source_preflight_job
            if capability_id == ResearchJob.Capability.VALIDATE:
                verified_source = validate_source_preflight_evidence(
                    manifest=manifest,
                    source_preflight_job=source_preflight_job,
                    for_update=True,
                )
            elif source_preflight_job is not None:
                raise ValidationError("source_preflight_only_valid_for_validation")
            payload = canonical_job_request(
                capability_id=capability_id,
                manifest=manifest,
                options=options,
                source_preflight_job=verified_source,
            )
            request_hash = sha256_prefixed(
                payload,
                label="internal_web_job_request",
            )
            existing = ResearchJob.objects.filter(
                owner=owner,
                idempotency_key=key,
            ).first()
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "idempotency_key_reused_for_different_request"
                    )
                return EnqueueResult(existing, False)
            job = ResearchJob.objects.create(
                owner=owner,
                manifest=manifest,
                source_preflight_job=verified_source,
                capability_id=capability_id,
                request_payload=payload,
                request_hash=request_hash,
                idempotency_key=key,
                actor_id=actor_id,
                actor_roles=roles,
                actor_permissions=permissions,
                correlation_id=cid,
            )
            record_web_audit_event(
                action="research_job_queued",
                actor_id=actor_id,
                object_type="research_job",
                object_id=str(job.pk),
                correlation_id=str(cid),
                details={
                    "capability_id": capability_id,
                    "request_hash": request_hash,
                    "manifest_hash": manifest.manifest_hash,
                },
            )
    except IntegrityError as exc:
        existing_for_key = ResearchJob.objects.filter(
            owner=owner,
            idempotency_key=key,
        ).first()
        if existing_for_key is not None:
            if existing_for_key.request_hash != request_hash:
                raise IdempotencyConflict(
                    "idempotency_key_reused_for_different_request"
                ) from exc
            return EnqueueResult(existing_for_key, False)

        existing_for_request = ResearchJob.objects.filter(
            owner=owner,
            request_hash=request_hash,
            status__in=ACTIVE_STATUSES,
        ).first()
        if existing_for_request is not None:
            return EnqueueResult(existing_for_request, False)

        active_job = ResearchJob.objects.filter(
            owner=owner,
            status__in=ACTIVE_STATUSES,
        ).first()
        if active_job is not None:
            raise ActiveJobConflict(active_job) from exc

        raise

    return EnqueueResult(job, True)


def claim_next_job(*, worker_id: str, now: Any | None = None) -> ResearchJob | None:
    from market_research.application import (
        LEGACY_WEB_CLAIM_SCOPE,
        require_operated_execution_capability,
    )

    require_operated_execution_capability(LEGACY_WEB_CLAIM_SCOPE)
    claimed_at = now or timezone.now()
    lease_seconds = int(settings.INTERNAL_WEB_JOB_LEASE_SECONDS)
    candidate_ids = list(
        ResearchJob.objects.filter(status=ResearchJob.Status.QUEUED)
        .order_by("created_at")
        .values_list("pk", flat=True)[:32]
    )
    for job_id in candidate_ids:
        with transaction.atomic():
            token = uuid.uuid4()
            updated = ResearchJob.objects.filter(
                pk=job_id,
                status=ResearchJob.Status.QUEUED,
            ).update(
                status=ResearchJob.Status.RUNNING,
                started_at=claimed_at,
                heartbeat_at=claimed_at,
                lease_token=token,
                lease_expires_at=claimed_at + timedelta(seconds=lease_seconds),
                attempt_count=F("attempt_count") + 1,
                version=F("version") + 1,
                progress_stage="starting",
                updated_at=claimed_at,
            )
            if updated != 1:
                continue
            job = ResearchJob.objects.select_related(
                "owner",
                "manifest",
                "source_preflight_job",
            ).get(pk=job_id)
            record_web_audit_event(
                action="research_job_claimed",
                actor_id=str(worker_id)[:255],
                object_type="research_job",
                object_id=str(job.pk),
                correlation_id=str(job.correlation_id),
                details={"attempt_count": job.attempt_count},
            )
        return job
    return None


def update_job_progress(
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    stage: str,
    details: dict[str, Any] | None = None,
    now: Any | None = None,
) -> ResearchJob:
    observed_at = now or timezone.now()
    normalized_stage = str(stage or "").strip()
    if not normalized_stage or len(normalized_stage) > 128:
        raise ValidationError("job_progress_stage_invalid")
    safe_details = dict(details or {})
    reject_paths_in_job_payload(safe_details)
    updated = ResearchJob.objects.filter(
        pk=job_id,
        lease_token=lease_token,
        lease_expires_at__gt=observed_at,
        status__in=(
            ResearchJob.Status.RUNNING,
            ResearchJob.Status.CANCEL_REQUESTED,
        ),
    ).update(
        progress_stage=normalized_stage,
        progress_details=safe_details,
        heartbeat_at=observed_at,
        lease_expires_at=observed_at
        + timedelta(seconds=int(settings.INTERNAL_WEB_JOB_LEASE_SECONDS)),
        version=F("version") + 1,
        updated_at=observed_at,
    )
    if updated != 1:
        raise JobLeaseLost("research_job_lease_lost")
    job = ResearchJob.objects.get(pk=job_id)
    if job.status == ResearchJob.Status.CANCEL_REQUESTED:
        raise JobCancellationRequested("research_job_cancellation_requested")
    return job


def request_job_cancellation(
    *,
    actor: Any,
    job_id: uuid.UUID,
    correlation_id: str | uuid.UUID | None = None,
) -> ResearchJob:
    with transaction.atomic():
        job = ResearchJob.objects.select_for_update().get(pk=job_id)
        owns_job = job.owner_id == actor.pk
        if not (
            (owns_job and actor.has_perm("portal.cancel_own_research_job"))
            or actor.has_perm("portal.manage_research_web")
        ):
            raise PermissionDenied("research_job_cancel_permission_required")
        now = timezone.now()
        cid = str(correlation_id or job.correlation_id)
        if job.status == ResearchJob.Status.QUEUED:
            updated = ResearchJob.objects.filter(pk=job.pk, status=job.status).update(
                status=ResearchJob.Status.CANCELLED,
                cancel_requested_at=now,
                finished_at=now,
                error_code="CANCELLED_BEFORE_START",
                version=F("version") + 1,
                updated_at=now,
            )
            action = (
                "research_job_cancelled"
                if updated == 1
                else "research_job_cancel_raced"
            )
        elif job.status == ResearchJob.Status.RUNNING:
            updated = ResearchJob.objects.filter(pk=job.pk, status=job.status).update(
                status=ResearchJob.Status.CANCEL_REQUESTED,
                cancel_requested_at=now,
                version=F("version") + 1,
                updated_at=now,
            )
            action = (
                "research_job_cancel_requested"
                if updated == 1
                else "research_job_cancel_raced"
            )
        elif job.status == ResearchJob.Status.CANCEL_REQUESTED:
            action = "research_job_cancel_request_repeated"
        else:
            action = "research_job_cancel_ignored_terminal"
        job.refresh_from_db()
        actor_id, _roles, _permissions = actor_snapshot(actor)
        record_web_audit_event(
            action=action,
            actor_id=actor_id,
            object_type="research_job",
            object_id=str(job.pk),
            correlation_id=cid,
            details={"status": job.status},
        )
    return job


def complete_job_success(
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    result: JobExecutionResult,
    authoritative_result_committed: bool = False,
) -> ResearchJob:
    validate_sha256(result.result_hash, field="result_hash")
    verify_result_artifact(result.result_ref, expected_hash=result.result_hash)
    research_outcome = str(result.research_outcome or "").strip().upper()
    if (
        job_capability := ResearchJob.objects.filter(pk=job_id)
        .values_list("capability_id", flat=True)
        .first()
    ):
        if (
            job_capability
            in {
                ResearchJob.Capability.PREFLIGHT,
                ResearchJob.Capability.VALIDATE,
            }
            and research_outcome not in ResearchJob.ResearchOutcome.values
        ):
            raise ValidationError("job_result_research_outcome_required")
    if research_outcome and research_outcome not in ResearchJob.ResearchOutcome.values:
        raise ValidationError("job_result_research_outcome_invalid")
    with transaction.atomic():
        now = timezone.now()
        allowed_statuses = (
            (ResearchJob.Status.RUNNING, ResearchJob.Status.CANCEL_REQUESTED)
            if authoritative_result_committed
            else (ResearchJob.Status.RUNNING,)
        )
        updated = ResearchJob.objects.filter(
            pk=job_id,
            lease_token=lease_token,
            lease_expires_at__gt=now,
            status__in=allowed_statuses,
        ).update(
            status=ResearchJob.Status.SUCCEEDED,
            result_ref=str(result.result_ref),
            result_hash=result.result_hash,
            research_outcome=research_outcome,
            run_id=str(result.run_id or "")[:128],
            progress_stage="complete",
            finished_at=now,
            heartbeat_at=now,
            lease_token=None,
            lease_expires_at=None,
            error_code="",
            version=F("version") + 1,
            updated_at=now,
        )
        if updated != 1:
            current = ResearchJob.objects.get(pk=job_id)
            if current.status == ResearchJob.Status.CANCEL_REQUESTED:
                raise JobCancellationRequested("research_job_cancellation_requested")
            raise JobLeaseLost("research_job_lease_lost")
        job = ResearchJob.objects.get(pk=job_id)
        record_web_audit_event(
            action="research_job_succeeded",
            actor_id="internal-web-worker",
            object_type="research_job",
            object_id=str(job.pk),
            correlation_id=str(job.correlation_id),
            details={
                "result_ref": str(result.result_ref),
                "result_hash": result.result_hash,
                "research_outcome": research_outcome,
                "authoritative_result_committed": authoritative_result_committed,
            },
        )
    return job


def fail_job(
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    error_code: str,
) -> ResearchJob:
    code = str(error_code or "").strip().upper()
    if not code or len(code) > 128 or not code.replace("_", "").isalnum():
        code = "UNEXPECTED_WORKER_ERROR"
    cancellation_requested = False
    with transaction.atomic():
        now = timezone.now()
        updated = ResearchJob.objects.filter(
            pk=job_id,
            lease_token=lease_token,
            lease_expires_at__gt=now,
            status=ResearchJob.Status.RUNNING,
        ).update(
            status=ResearchJob.Status.FAILED,
            error_code=code,
            progress_stage="failed",
            finished_at=now,
            heartbeat_at=now,
            lease_token=None,
            lease_expires_at=None,
            version=F("version") + 1,
            updated_at=now,
        )
        if updated != 1:
            current = ResearchJob.objects.get(pk=job_id)
            if current.status == ResearchJob.Status.CANCEL_REQUESTED:
                cancellation_requested = True
            else:
                raise JobLeaseLost("research_job_lease_lost")
        else:
            job = ResearchJob.objects.get(pk=job_id)
            record_web_audit_event(
                action="research_job_failed",
                actor_id="internal-web-worker",
                object_type="research_job",
                object_id=str(job.pk),
                correlation_id=str(job.correlation_id),
                details={"error_code": code},
            )
    if cancellation_requested:
        return finalize_cancelled(job_id=job_id, lease_token=lease_token)
    return job


def finalize_cancelled(
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
) -> ResearchJob:
    with transaction.atomic():
        now = timezone.now()
        updated = ResearchJob.objects.filter(
            pk=job_id,
            lease_token=lease_token,
            lease_expires_at__gt=now,
            status__in=(
                ResearchJob.Status.RUNNING,
                ResearchJob.Status.CANCEL_REQUESTED,
            ),
        ).update(
            status=ResearchJob.Status.CANCELLED,
            error_code="CANCELLED_BY_REQUEST",
            progress_stage="cancelled",
            finished_at=now,
            heartbeat_at=now,
            lease_token=None,
            lease_expires_at=None,
            version=F("version") + 1,
            updated_at=now,
        )
        if updated != 1:
            raise JobLeaseLost("research_job_lease_lost")
        job = ResearchJob.objects.get(pk=job_id)
        record_web_audit_event(
            action="research_job_cancelled",
            actor_id="internal-web-worker",
            object_type="research_job",
            object_id=str(job.pk),
            correlation_id=str(job.correlation_id),
            details={"error_code": job.error_code},
        )
    return job


def jobs_visible_to(user: Any) -> QuerySet[ResearchJob]:
    if not getattr(user, "is_authenticated", False):
        return ResearchJob.objects.none()
    queryset = ResearchJob.objects.select_related("owner", "manifest")
    return queryset if can_view_all_jobs(user) else queryset.filter(owner=user)
