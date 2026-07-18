"""Machine-readable internal-web API and persisted-schema contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Literal, cast

from django.apps import apps
from django.db import models
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field

from .models import ResearchJob
from .presenters import safe_error_action, safe_error_message


API_VERSION = "1.0.0"
API_SCHEMA_VERSION: Literal["1.0"] = "1.0"
JobStatus = Literal[
    "QUEUED",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "CANCEL_REQUESTED",
    "CANCELLED",
]
CapabilityId = Literal["research-preflight", "research-validate"]
ResearchOutcome = Literal["PASS", "FAIL"]


class ApiModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")


class JobSubmissionRequest(ApiModel):
    capability_id: CapabilityId
    source_preflight_job_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-fA-F-]{36}$",
    )


class ApiError(ApiModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1)
    action: str = Field(min_length=1)
    retryable: bool
    correlation_id: str = Field(min_length=1, max_length=128)


class ApiErrorEnvelope(ApiModel):
    schema_version: Literal["1.0"] = "1.0"
    error: ApiError


class JobProgress(ApiModel):
    percent: int = Field(ge=0, le=100)
    stage_code: str
    stage_label: str
    message: str


class JobLinks(ApiModel):
    self: str
    status: str
    cancel: str | None = None
    retry: str | None = None


class JobResource(ApiModel):
    schema_version: Literal["1.0"] = "1.0"
    id: str
    manifest_id: str
    capability_id: CapabilityId
    status: JobStatus
    status_label: str
    terminal: bool
    version: int = Field(ge=0)
    progress: JobProgress
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    run_id: str | None = None
    research_outcome: ResearchOutcome | None = None
    result_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    error: ApiError | None = None
    cancel_allowed: bool
    retry_allowed: bool
    links: JobLinks


class PageMetadata(ApiModel):
    count: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    next: str | None = None
    previous: str | None = None
    sort: Literal["created_at", "-created_at", "updated_at", "-updated_at"]
    filters: dict[str, str]


class JobListResponse(ApiModel):
    schema_version: Literal["1.0"] = "1.0"
    page: PageMetadata
    items: tuple[JobResource, ...]


STATUS_LABELS: dict[str, str] = {
    ResearchJob.Status.QUEUED: "대기 중",
    ResearchJob.Status.RUNNING: "실행 중",
    ResearchJob.Status.SUCCEEDED: "완료",
    ResearchJob.Status.FAILED: "실패",
    ResearchJob.Status.CANCEL_REQUESTED: "취소 요청됨",
    ResearchJob.Status.CANCELLED: "취소됨",
}
STAGE_LABELS = {
    "starting": "연구 환경 준비",
    "readiness_scan": "데이터 준비 상태 확인",
    "workload_estimate": "예상 작업량 계산",
    "validation": "연구 검증 실행",
    "complete": "결과 무결성 확인",
    "failed": "안전하게 중단됨",
    "cancelled": "취소 완료",
}
STAGE_PROGRESS = {
    "": 10,
    "starting": 20,
    "readiness_scan": 40,
    "workload_estimate": 60,
    "validation": 75,
    "complete": 100,
    "failed": 100,
    "cancelled": 100,
}


def _progress_message(job: ResearchJob) -> str:
    if job.status == ResearchJob.Status.QUEUED:
        return "대기열에 저장되었습니다. 브라우저를 닫아도 작업은 유지됩니다."
    if job.status == ResearchJob.Status.RUNNING:
        return "현재 단계를 실행 중이며 완료 시 다음 상태가 저장됩니다."
    if job.status == ResearchJob.Status.CANCEL_REQUESTED:
        return "안전한 단계 경계에서 취소를 적용하고 있습니다."
    if job.status == ResearchJob.Status.FAILED:
        return safe_error_message(job)
    return "최종 상태와 결과 무결성이 저장되었습니다."


def project_job(
    job: ResearchJob,
    *,
    cancel_allowed: bool,
    retry_allowed: bool,
    correlation_id: str,
) -> JobResource:
    """Return a path-free, timezone-aware projection of persisted job state."""

    job_id = str(job.pk)
    manifest_id = str(job.manifest_id)
    stage_code = job.progress_stage or (
        "queued" if not job.is_terminal else job.status.lower()
    )
    percent = 100 if job.is_terminal else STAGE_PROGRESS.get(job.progress_stage, 25)
    error = None
    if job.status == ResearchJob.Status.FAILED:
        error = ApiError(
            code=job.error_code or "RESEARCH_JOB_FAILED",
            message=safe_error_message(job),
            action=(
                f"{safe_error_action(job)} 사전 점검은 새 요청으로 다시 실행할 수 있습니다."
                if retry_allowed
                else safe_error_action(job)
            ),
            retryable=retry_allowed,
            correlation_id=correlation_id,
        )
    base = f"/api/v1/jobs/{job_id}/"
    retry_link = f"/api/v1/manifests/{manifest_id}/jobs/" if retry_allowed else None
    return JobResource(
        id=job_id,
        manifest_id=manifest_id,
        capability_id=cast(CapabilityId, job.capability_id),
        status=cast(JobStatus, job.status),
        status_label=STATUS_LABELS.get(job.status, job.status),
        terminal=job.is_terminal,
        version=job.version,
        progress=JobProgress(
            percent=percent,
            stage_code=stage_code,
            stage_label=STAGE_LABELS.get(
                job.progress_stage,
                STATUS_LABELS.get(job.status, job.status),
            ),
            message=_progress_message(job),
        ),
        created_at=timezone.localtime(job.created_at),
        updated_at=timezone.localtime(job.updated_at),
        finished_at=(
            timezone.localtime(job.finished_at) if job.finished_at is not None else None
        ),
        run_id=job.run_id or None,
        research_outcome=cast(ResearchOutcome | None, job.research_outcome or None),
        result_hash=job.result_hash or None,
        error=error,
        cancel_allowed=cancel_allowed,
        retry_allowed=retry_allowed,
        links=JobLinks(
            self=base,
            status=base,
            cancel=f"{base}cancel/" if cancel_allowed else None,
            retry=retry_link,
        ),
    )


def _openapi_schema(model: type[BaseModel]) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = model.model_json_schema(ref_template="#/components/schemas/{model}")
    definitions = dict(raw.pop("$defs", {}))
    return raw, definitions


def build_openapi_document() -> dict[str, Any]:
    """Generate OpenAPI directly from the Pydantic models used by views."""

    components: dict[str, Any] = {}
    for model in (
        ApiErrorEnvelope,
        JobSubmissionRequest,
        JobResource,
        JobListResponse,
    ):
        schema, definitions = _openapi_schema(model)
        components[model.__name__] = schema
        for name, definition in definitions.items():
            existing = components.get(name)
            if existing is not None and existing != definition:
                raise RuntimeError(f"openapi_component_collision:{name}")
            components[name] = definition

    error_responses = {
        str(status): {
            "description": description,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ApiErrorEnvelope"}
                }
            },
        }
        for status, description in (
            (400, "Malformed or contract-invalid request"),
            (401, "Authentication required"),
            (403, "Role or object authorization denied"),
            (404, "Visible resource not found"),
            (405, "HTTP method not allowed"),
            (409, "Idempotency or active-job conflict"),
            (415, "Content type must be application/json"),
        )
    }
    job_response = {
        "description": "Current asynchronous job resource",
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/JobResource"}}
        },
    }
    id_parameter = {
        "name": "job_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Market Research Internal Web API",
            "version": API_VERSION,
            "description": (
                "Authenticated, CSRF-protected API for offline research jobs. "
                "All state changes use the same application services as the GUI."
            ),
        },
        "servers": [{"url": "/", "description": "Current internal-web origin"}],
        "security": [{"sessionCookie": []}],
        "paths": {
            "/api/v1/openapi.json": {
                "get": {
                    "operationId": "getInternalWebOpenApi",
                    "summary": "Get this generated API contract",
                    "responses": {
                        "200": {"description": "OpenAPI 3.1 document"},
                        **error_responses,
                    },
                }
            },
            "/api/v1/jobs/": {
                "get": {
                    "operationId": "listResearchJobs",
                    "summary": "List object-authorized research jobs",
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "schema": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "default": 25,
                            },
                        },
                        {
                            "name": "offset",
                            "in": "query",
                            "schema": {"type": "integer", "minimum": 0, "default": 0},
                        },
                        {
                            "name": "status",
                            "in": "query",
                            "schema": {
                                "type": "string",
                                "enum": list(ResearchJob.Status.values),
                            },
                        },
                        {
                            "name": "capability",
                            "in": "query",
                            "schema": {
                                "type": "string",
                                "enum": list(ResearchJob.Capability.values),
                            },
                        },
                        {
                            "name": "sort",
                            "in": "query",
                            "schema": {
                                "type": "string",
                                "enum": [
                                    "created_at",
                                    "-created_at",
                                    "updated_at",
                                    "-updated_at",
                                ],
                                "default": "-created_at",
                            },
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "Stable limit/offset page",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/JobListResponse"
                                    }
                                }
                            },
                        },
                        **error_responses,
                    },
                }
            },
            "/api/v1/manifests/{manifest_id}/jobs/": {
                "post": {
                    "operationId": "submitResearchJob",
                    "summary": "Submit an asynchronous research job",
                    "parameters": [
                        {
                            "name": "manifest_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "uuid"},
                        },
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string", "format": "uuid"},
                        },
                        {
                            "name": "X-CSRFToken",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/JobSubmissionRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": job_response,
                        "200": job_response,
                        **error_responses,
                    },
                }
            },
            "/api/v1/jobs/{job_id}/": {
                "get": {
                    "operationId": "getResearchJob",
                    "summary": "Get durable asynchronous status",
                    "parameters": [id_parameter],
                    "responses": {"200": job_response, **error_responses},
                }
            },
            "/api/v1/jobs/{job_id}/cancel/": {
                "post": {
                    "operationId": "cancelResearchJob",
                    "summary": "Idempotently request cancellation",
                    "parameters": [
                        id_parameter,
                        {
                            "name": "X-CSRFToken",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {"200": job_response, **error_responses},
                }
            },
        },
        "components": {
            "securitySchemes": {
                "sessionCookie": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "sessionid",
                    "description": "Django session; mutating calls also require CSRF.",
                }
            },
            "schemas": dict(sorted(components.items())),
        },
    }


def build_persisted_schema_document() -> dict[str, Any]:
    """Describe the actual Django model metadata used by migrations/runtime."""

    model_documents: dict[str, Any] = {}
    for model in sorted(
        apps.get_app_config("portal").get_models(),
        key=lambda item: item._meta.label_lower,
    ):
        fields: list[dict[str, Any]] = []
        for field in model._meta.fields:
            related_model = getattr(field.remote_field, "model", None)
            fields.append(
                {
                    "name": field.name,
                    "column": field.column,
                    "type": field.get_internal_type(),
                    "null": field.null,
                    "blank": field.blank,
                    "primary_key": field.primary_key,
                    "unique": field.unique,
                    "editable": field.editable,
                    "max_length": field.max_length,
                    "related_model": (
                        related_model._meta.label_lower
                        if isinstance(related_model, type)
                        and issubclass(related_model, models.Model)
                        else None
                    ),
                }
            )
        model_documents[model._meta.label_lower] = {
            "database_table": model._meta.db_table,
            "ordering": list(model._meta.ordering or ()),
            "fields": fields,
            "constraints": [
                {
                    "name": constraint.name,
                    "type": type(constraint).__name__,
                }
                for constraint in model._meta.constraints
            ],
            "indexes": [
                {
                    "name": index.name,
                    "fields": list(index.fields),
                }
                for index in model._meta.indexes
            ],
        }
    return {
        "schema_version": API_SCHEMA_VERSION,
        "status": "current",
        "generated_from": "django.apps[portal].model._meta",
        "models": model_documents,
    }
