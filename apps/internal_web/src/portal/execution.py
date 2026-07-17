"""Direct worker adapter from persisted web jobs to the shared application service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError

from market_research.application import (
    ActorContext,
    ApplicationAuthorizationError,
    RESEARCH_JOB_DISPATCH_SCOPE,
    ResearchApplicationService,
    ResearchPreflightRequest,
    ResearchValidationRequest,
    require_operated_execution_capability,
)
from market_research.application.adapter_contracts import (
    content_hash_payload,
    sha256_prefixed,
)
from market_research.research_composition import builtin_strategy_registry
from market_research.storage_io import write_json_atomic

from .admission import validate_raw_manifest_admission
from .jobs import (
    JobCancellationRequested,
    JobExecutionResult,
    validate_web_job_capability_contract,
    validate_validation_job_gate,
)
from .models import ResearchJob
from .storage import (
    make_artifact_ref,
    read_verified_manifest_bytes,
    resolve_artifact_ref,
)
from .worker import JobProgressReporter, PublicJobError


_SAFE_ERROR_CODES = {
    "application_cancelled": "RESEARCH_JOB_CANCELLED",
    "manifest_invalid": "MANIFEST_INVALID",
    "execution_calibration_invalid": "EXECUTION_CALIBRATION_INVALID",
    "validation_run_failed": "VALIDATION_RUN_FAILED",
    "research_validation_failed": "RESEARCH_VALIDATION_FAILED",
    "research_io_error": "RESEARCH_INPUT_UNAVAILABLE",
    "invalid_research_request": "RESEARCH_REQUEST_INVALID",
    "application_execution_failed": "RESEARCH_ENGINE_ERROR",
    "application_permission_denied": "APPLICATION_PERMISSION_DENIED",
}
_PUBLIC_ERROR_MESSAGES = {
    "RESEARCH_JOB_CANCELLED": "The research job was cancelled.",
    "MANIFEST_INVALID": "The research manifest is invalid.",
    "EXECUTION_CALIBRATION_INVALID": "The execution calibration is invalid.",
    "VALIDATION_RUN_FAILED": "The validation run did not complete.",
    "RESEARCH_VALIDATION_FAILED": "The research validation did not complete.",
    "RESEARCH_INPUT_UNAVAILABLE": "A required research input is unavailable.",
    "RESEARCH_REQUEST_INVALID": "The research request is invalid.",
    "RESEARCH_ENGINE_ERROR": "The research engine could not complete the request.",
    "APPLICATION_PERMISSION_DENIED": "The recorded actor is not authorized for this research action.",
}


@dataclass(frozen=True, slots=True)
class ResearchJobDispatcher:
    """Execute supported jobs without importing the CLI or spawning a process."""

    def execute(
        self,
        job: ResearchJob,
        progress: JobProgressReporter,
    ) -> JobExecutionResult:
        require_operated_execution_capability(
            RESEARCH_JOB_DISPATCH_SCOPE,
            admission_request_id=f"web-job:{job.pk}",
            admission_request_hash=job.request_hash,
        )
        try:
            validate_web_job_capability_contract(job.capability_id)
        except ValidationError as exc:
            raise PublicJobError("CAPABILITY_CONTRACT_INVALID") from exc
        if job.capability_id == ResearchJob.Capability.VALIDATE:
            try:
                validate_validation_job_gate(job)
            except (ValidationError, ResearchJob.DoesNotExist) as exc:
                raise PublicJobError("PREFLIGHT_GATE_INVALID") from exc
        manifest_path = self._verified_manifest_path(job)
        actor = ActorContext(
            actor_id=job.actor_id,
            roles=tuple(str(item) for item in job.actor_roles),
            permissions=frozenset(str(item) for item in job.actor_permissions),
            source="worker",
        )
        service = ResearchApplicationService(
            paths=settings.RESEARCH_PATHS,
            strategy_registry=builtin_strategy_registry(),
        )

        def cancellation_requested() -> bool:
            return ResearchJob.objects.filter(
                pk=job.pk,
                status=ResearchJob.Status.CANCEL_REQUESTED,
            ).exists()

        def report_progress(event: dict[str, Any]) -> None:
            if cancellation_requested():
                raise JobCancellationRequested("research_job_cancellation_requested")
            progress(_safe_progress_event(event))

        if job.capability_id == ResearchJob.Capability.PREFLIGHT:
            request = ResearchPreflightRequest(
                request_id=str(job.pk),
                idempotency_key=job.idempotency_key,
                actor=actor,
                manifest_path=str(manifest_path),
                execution_calibration_path=None,
            )
            try:
                result = service.preflight(
                    request,
                    progress_callback=report_progress,
                    cancellation_check=cancellation_requested,
                )
            except ApplicationAuthorizationError as exc:
                raise PublicJobError("APPLICATION_PERMISSION_DENIED") from exc
            readiness = result.readiness
            workload = result.workload
            if cancellation_requested():
                raise JobCancellationRequested("research_job_cancellation_requested")
            errors = tuple(readiness.errors) + tuple(workload.errors)
            if errors and readiness.report is None and workload.estimate is None:
                raise PublicJobError(_public_error_code(errors[0].code))
            payload: dict[str, Any] = {
                "schema_version": 1,
                "report_kind": "internal_web_preflight",
                "capability_id": "research-preflight",
                "request_hash": job.request_hash,
                "manifest_hash": job.manifest.manifest_hash,
                "manifest_content_hash": job.manifest.content_hash,
                "status": (
                    "PASS"
                    if readiness.report
                    and readiness.report.get("status") == "PASS"
                    and workload.estimate is not None
                    else "FAIL"
                ),
                "readiness": _safe_application_result_projection(
                    readiness.model_dump(mode="json")
                ),
                "workload": _safe_application_result_projection(
                    workload.model_dump(mode="json")
                ),
            }
            return self._publish_web_result(job, payload)

        if job.capability_id == ResearchJob.Capability.VALIDATE:
            output_path = settings.RESEARCH_PATHS.report_path(
                "_internal_web",
                str(job.pk),
                "validation_result.json",
            )
            request = ResearchValidationRequest(
                request_id=str(job.pk),
                idempotency_key=job.idempotency_key,
                actor=actor,
                manifest_path=str(manifest_path),
                out_path=str(output_path),
                mode="strict",
            )
            try:
                result = service.validate(
                    request,
                    progress_callback=report_progress,
                    cancellation_check=cancellation_requested,
                )
            except ApplicationAuthorizationError as exc:
                raise PublicJobError("APPLICATION_PERMISSION_DENIED") from exc
            if cancellation_requested() or result.status.value == "CANCELLED":
                raise JobCancellationRequested("research_job_cancellation_requested")
            if result.report is None:
                code = (
                    result.errors[0].code
                    if result.errors
                    else "application_execution_failed"
                )
                raise PublicJobError(_public_error_code(code))
            # The research engine, not this adapter, writes and hashes this result.
            reference = make_artifact_ref("report", output_path)
            if not result.content_hash:
                raise PublicJobError("RESULT_HASH_MISSING")
            return JobExecutionResult(
                result_ref=reference,
                result_hash=result.content_hash,
                run_id=result.run_id or "",
                research_outcome=result.research_outcome or "",
            )

        raise PublicJobError("CAPABILITY_NOT_AVAILABLE_IN_WEB")

    @staticmethod
    def _verified_manifest_path(job: ResearchJob) -> Path:
        try:
            content = read_verified_manifest_bytes(job.manifest)
        except ValidationError as exc:
            raise PublicJobError("MANIFEST_CONTENT_HASH_MISMATCH") from exc
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicJobError("MANIFEST_INVALID") from exc
        if not isinstance(payload, dict):
            raise PublicJobError("MANIFEST_INVALID")
        try:
            validate_raw_manifest_admission(payload)
        except ValidationError as exc:
            raise PublicJobError("RESEARCH_REQUEST_INVALID") from exc
        return resolve_artifact_ref(job.manifest.storage_ref)

    @staticmethod
    def _publish_web_result(
        job: ResearchJob,
        payload: dict[str, Any],
    ) -> JobExecutionResult:
        target = settings.RESEARCH_PATHS.report_path(
            "_internal_web",
            str(job.pk),
            "preflight_result.json",
        )
        material = dict(payload)
        material["content_hash"] = sha256_prefixed(content_hash_payload(material))
        write_json_atomic(target, material)
        return JobExecutionResult(
            result_ref=make_artifact_ref("report", target),
            result_hash=str(material["content_hash"]),
            research_outcome=str(material.get("status") or ""),
        )


def _public_error_code(application_code: str) -> str:
    return _SAFE_ERROR_CODES.get(application_code, "RESEARCH_ENGINE_ERROR")


def _safe_application_result_projection(value: Any) -> Any:
    """Project application errors to fixed public data before path redaction."""

    if not isinstance(value, dict):
        return _safe_artifact_projection(value)
    projection = dict(value)
    raw_errors = projection.get("errors")
    if isinstance(raw_errors, (list, tuple)):
        projection["errors"] = [
            _public_application_error(error) for error in raw_errors
        ]
    elif raw_errors:
        projection["errors"] = [_public_application_error(raw_errors)]
    return _safe_artifact_projection(projection)


def _public_application_error(value: Any) -> dict[str, str]:
    application_code = str(value.get("code") or "") if isinstance(value, dict) else ""
    public_code = _public_error_code(application_code)
    return {
        "code": public_code,
        "message": _PUBLIC_ERROR_MESSAGES[public_code],
    }


def _safe_progress_event(event: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in event.items():
        normalized = str(key)
        if normalized.lower().endswith("path"):
            continue
        safe[normalized] = _safe_scalar(value)
    return safe


def _safe_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if (
            Path(stripped).is_absolute()
            or PureWindowsPath(stripped).is_absolute()
            or stripped.lower().startswith(("file:", "sqlite:", "duckdb:"))
        ):
            return "<server-managed>"
        return value[:512]
    if isinstance(value, (list, tuple)):
        return [_safe_scalar(item) for item in value[:50]]
    if isinstance(value, dict):
        return {
            str(key): _safe_scalar(item)
            for key, item in list(value.items())[:100]
            if not str(key).lower().endswith("path")
        }
    return str(value)[:512]


def _safe_artifact_projection(value: Any) -> Any:
    """Remove server topology while preserving engine-produced decisions."""

    if isinstance(value, dict):
        return {
            str(key): _safe_artifact_projection(item)
            for key, item in value.items()
            if not str(key).lower().endswith(("path", "uri"))
            and str(key).lower() not in {"db_path", "manifest_path"}
        }
    if isinstance(value, (list, tuple)):
        return [_safe_artifact_projection(item) for item in value]
    return _safe_scalar(value)
