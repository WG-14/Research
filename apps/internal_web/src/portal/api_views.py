"""Versioned JSON adapter for the internal research application services."""

from __future__ import annotations

import uuid
import re
from typing import Any, Literal, cast
from urllib.parse import urlencode

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpRequest, JsonResponse
from django.views.csrf import csrf_failure as django_csrf_failure
from pydantic import ValidationError as PydanticValidationError

from .api_contract import (
    API_SCHEMA_VERSION,
    ApiError,
    ApiErrorEnvelope,
    JobListResponse,
    JobSubmissionRequest,
    PageMetadata,
    ResearchListResponse,
    ResearchPageMetadata,
    ResearchProjectionResponse,
    ResearchResource,
    build_openapi_document,
    project_job,
)
from .authorization import can_access_manifest, jobs_visible_to, manifests_visible_to
from .jobs import (
    ActiveJobConflict,
    IdempotencyConflict,
    enqueue_research_job,
    request_job_cancellation,
)
from .models import ResearchJob, ResourceAccessGrant
from .research_explorer import (
    RESEARCH_EXPLORATION_PERMISSION,
    ResearchExplorerService,
    audit_research_exploration_read,
)

SORT_FIELDS = frozenset({"created_at", "-created_at", "updated_at", "-updated_at"})
SortOrder = Literal["created_at", "-created_at", "updated_at", "-updated_at"]
_STABLE_RESEARCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


def _correlation_id(request: HttpRequest) -> str:
    return str(getattr(request, "correlation_id", uuid.uuid4()))


def _json_response(model: Any, *, status: int = 200) -> JsonResponse:
    if hasattr(model, "model_dump"):
        payload = model.model_dump(mode="json")
    else:
        payload = model
    return JsonResponse(
        payload,
        status=status,
        json_dumps_params={"ensure_ascii": False, "sort_keys": True},
    )


def _error(
    request: HttpRequest,
    *,
    status: int,
    code: str,
    message: str,
    action: str,
    retryable: bool = False,
) -> JsonResponse:
    return _json_response(
        ApiErrorEnvelope(
            error=ApiError(
                code=code,
                message=message,
                action=action,
                retryable=retryable,
                correlation_id=_correlation_id(request),
            )
        ),
        status=status,
    )


def _require_method(request: HttpRequest, method: str) -> JsonResponse | None:
    if request.method == method:
        return None
    response = _error(
        request,
        status=405,
        code="METHOD_NOT_ALLOWED",
        message=f"이 API는 {method} 요청만 지원합니다.",
        action="API 명세의 method와 Content-Type을 확인해 주세요.",
    )
    response["Allow"] = method
    return response


def _require_permission(
    request: HttpRequest,
    permission: str,
) -> JsonResponse | None:
    user = request.user
    if not getattr(user, "is_authenticated", False) or not getattr(
        user, "is_active", False
    ):
        return _error(
            request,
            status=401,
            code="AUTHENTICATION_REQUIRED",
            message="로그인 세션이 필요하거나 만료되었습니다.",
            action="다시 로그인한 뒤 동일한 요청 식별값으로 재시도해 주세요.",
            retryable=True,
        )
    if not user.has_perm(permission):
        return _error(
            request,
            status=403,
            code="PERMISSION_DENIED",
            message="현재 역할에는 이 작업을 수행할 권한이 없습니다.",
            action="관리자에게 필요한 역할과 문의 ID를 전달해 주세요.",
        )
    return None


def _can_cancel(user: Any, job: ResearchJob) -> bool:
    return bool(
        (job.owner_id == user.pk and user.has_perm("portal.cancel_own_research_job"))
        or user.has_perm("portal.manage_research_web")
    )


def _can_retry(user: Any, job: ResearchJob) -> bool:
    return bool(
        job.status in {ResearchJob.Status.FAILED, ResearchJob.Status.CANCELLED}
        and job.capability_id == ResearchJob.Capability.PREFLIGHT
        and user.has_perm("portal.submit_research_job")
        and can_access_manifest(
            user,
            job.manifest,
            access=ResourceAccessGrant.Access.SUBMIT,
        )
    )


def _project(request: HttpRequest, job: ResearchJob) -> Any:
    return project_job(
        job,
        cancel_allowed=_can_cancel(request.user, job)
        and job.status in {ResearchJob.Status.QUEUED, ResearchJob.Status.RUNNING},
        retry_allowed=_can_retry(request.user, job),
        correlation_id=_correlation_id(request),
    )


def openapi_document(request: HttpRequest) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, "portal.view_researchjob"):
        return denied
    return _json_response(build_openapi_document())


def job_list(request: HttpRequest) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, "portal.view_researchjob"):
        return denied
    try:
        limit = int(request.GET.get("limit", "25"))
        offset = int(request.GET.get("offset", "0"))
    except ValueError:
        return _error(
            request,
            status=400,
            code="PAGINATION_INVALID",
            message="limit과 offset은 정수여야 합니다.",
            action="limit은 1~100, offset은 0 이상으로 지정해 주세요.",
        )
    if not 1 <= limit <= 100 or offset < 0:
        return _error(
            request,
            status=400,
            code="PAGINATION_INVALID",
            message="페이지 범위가 허용된 한도를 벗어났습니다.",
            action="limit은 1~100, offset은 0 이상으로 지정해 주세요.",
        )
    status_filter = str(request.GET.get("status") or "")
    capability_filter = str(request.GET.get("capability") or "")
    sort = str(request.GET.get("sort") or "-created_at")
    if status_filter and status_filter not in ResearchJob.Status.values:
        return _error(
            request,
            status=400,
            code="STATUS_FILTER_INVALID",
            message="알 수 없는 작업 상태입니다.",
            action="OpenAPI 명세의 status enum 중 하나를 사용해 주세요.",
        )
    if capability_filter and capability_filter not in ResearchJob.Capability.values:
        return _error(
            request,
            status=400,
            code="CAPABILITY_FILTER_INVALID",
            message="알 수 없는 연구 기능입니다.",
            action="OpenAPI 명세의 capability enum 중 하나를 사용해 주세요.",
        )
    if sort not in SORT_FIELDS:
        return _error(
            request,
            status=400,
            code="SORT_INVALID",
            message="지원하지 않는 정렬 기준입니다.",
            action="created_at 또는 updated_at에 선택적으로 '-'를 붙여 주세요.",
        )

    queryset = jobs_visible_to(request.user)
    filters: dict[str, str] = {}
    if status_filter:
        queryset = queryset.filter(status=status_filter)
        filters["status"] = status_filter
    if capability_filter:
        queryset = queryset.filter(capability_id=capability_filter)
        filters["capability"] = capability_filter
    count = queryset.count()
    tie_breaker = "-pk" if sort.startswith("-") else "pk"
    jobs = list(queryset.order_by(sort, tie_breaker)[offset : offset + limit])

    def page_url(new_offset: int) -> str:
        values = {
            "limit": str(limit),
            "offset": str(new_offset),
            "sort": sort,
            **filters,
        }
        return f"{request.path}?{urlencode(values)}"

    next_url = page_url(offset + limit) if offset + limit < count else None
    previous_url = page_url(max(0, offset - limit)) if offset > 0 else None
    return _json_response(
        JobListResponse(
            schema_version=API_SCHEMA_VERSION,
            page=PageMetadata(
                count=count,
                limit=limit,
                offset=offset,
                next=next_url,
                previous=previous_url,
                sort=cast(SortOrder, sort),
                filters=filters,
            ),
            items=tuple(_project(request, job) for job in jobs),
        )
    )


def job_detail(request: HttpRequest, job_id: uuid.UUID) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, "portal.view_researchjob"):
        return denied
    job = jobs_visible_to(request.user).filter(pk=job_id).first()
    if job is None:
        return _error(
            request,
            status=404,
            code="JOB_NOT_FOUND",
            message="요청한 작업을 찾을 수 없습니다.",
            action="작업 ID와 현재 계정의 자원 접근 범위를 확인해 주세요.",
        )
    return _json_response(_project(request, job))


def job_submit(request: HttpRequest, manifest_id: uuid.UUID) -> JsonResponse:
    if invalid := _require_method(request, "POST"):
        return invalid
    if denied := _require_permission(request, "portal.submit_research_job"):
        return denied
    if request.content_type != "application/json":
        return _error(
            request,
            status=415,
            code="CONTENT_TYPE_INVALID",
            message="JSON 요청만 지원합니다.",
            action="Content-Type을 application/json으로 설정해 주세요.",
        )
    raw_key = str(request.headers.get("Idempotency-Key") or "")
    try:
        idempotency_key = str(uuid.UUID(raw_key))
    except ValueError:
        return _error(
            request,
            status=400,
            code="IDEMPOTENCY_KEY_INVALID",
            message="요청 식별값이 없거나 UUID 형식이 아닙니다.",
            action="새 UUID를 Idempotency-Key 헤더에 넣고 재시도해 주세요.",
        )
    try:
        submission = JobSubmissionRequest.model_validate_json(request.body)
    except PydanticValidationError:
        return _error(
            request,
            status=400,
            code="REQUEST_SCHEMA_INVALID",
            message="요청 본문이 연구 작업 계약과 일치하지 않습니다.",
            action="OpenAPI의 JobSubmissionRequest 필드와 enum을 확인해 주세요.",
        )
    manifest = (
        manifests_visible_to(
            request.user,
            access=ResourceAccessGrant.Access.SUBMIT,
        )
        .filter(pk=manifest_id)
        .first()
    )
    if manifest is None:
        return _error(
            request,
            status=404,
            code="MANIFEST_NOT_FOUND",
            message="사용 가능한 연구 정의를 찾을 수 없습니다.",
            action="manifest ID와 현재 계정의 자원 접근 grant를 확인해 주세요.",
        )
    source = None
    if submission.source_preflight_job_id is not None:
        try:
            source_id = uuid.UUID(submission.source_preflight_job_id)
        except ValueError:
            return _error(
                request,
                status=400,
                code="SOURCE_PREFLIGHT_ID_INVALID",
                message="사전 점검 작업 ID가 UUID 형식이 아닙니다.",
                action="완료된 사전 점검의 job ID를 사용해 주세요.",
            )
        source = jobs_visible_to(request.user).filter(pk=source_id).first()
        if source is None:
            return _error(
                request,
                status=404,
                code="SOURCE_PREFLIGHT_NOT_FOUND",
                message="사용 가능한 사전 점검 작업을 찾을 수 없습니다.",
                action="job ID와 현재 계정의 자원 접근 범위를 확인해 주세요.",
            )
    try:
        result = enqueue_research_job(
            owner=request.user,
            manifest=manifest,
            capability_id=submission.capability_id,
            idempotency_key=idempotency_key,
            correlation_id=_correlation_id(request),
            source_preflight_job=source,
        )
    except IdempotencyConflict:
        return _error(
            request,
            status=409,
            code="IDEMPOTENCY_CONFLICT",
            message="같은 요청 식별값이 다른 입력에 이미 사용되었습니다.",
            action="원래 요청과 같은 본문을 사용하거나 새 UUID로 새 의도를 제출해 주세요.",
        )
    except ActiveJobConflict as exc:
        return _error(
            request,
            status=409,
            code="ACTIVE_JOB_CONFLICT",
            message="현재 계정에 이미 진행 중인 작업이 있습니다.",
            action=f"작업 {exc.existing_job.pk}의 완료 또는 취소를 확인해 주세요.",
            retryable=True,
        )
    except PermissionDenied:
        return _error(
            request,
            status=403,
            code="OBJECT_PERMISSION_DENIED",
            message="이 연구 정의에 작업을 제출할 권한이 없습니다.",
            action="자원 단위 submit grant를 확인해 주세요.",
        )
    except ValidationError as exc:
        return _error(
            request,
            status=400,
            code="JOB_SUBMISSION_INVALID",
            message="사전 점검과 검증 작업의 상태 또는 증거 연결이 올바르지 않습니다.",
            action=f"완료된 PASS 사전 점검과 동일 manifest를 사용해 주세요. ({exc.code or 'invalid'})",
        )
    return _json_response(
        _project(request, result.job),
        status=201 if result.created else 200,
    )


def job_cancel(request: HttpRequest, job_id: uuid.UUID) -> JsonResponse:
    if invalid := _require_method(request, "POST"):
        return invalid
    if denied := _require_permission(request, "portal.view_researchjob"):
        return denied
    job = jobs_visible_to(request.user).filter(pk=job_id).first()
    if job is None:
        return _error(
            request,
            status=404,
            code="JOB_NOT_FOUND",
            message="요청한 작업을 찾을 수 없습니다.",
            action="작업 ID와 현재 계정의 자원 접근 범위를 확인해 주세요.",
        )
    if not _can_cancel(request.user, job):
        return _error(
            request,
            status=403,
            code="OBJECT_PERMISSION_DENIED",
            message="이 작업을 취소할 권한이 없습니다.",
            action="소유자 취소 권한 또는 관리자 역할을 확인해 주세요.",
        )
    try:
        cancelled = request_job_cancellation(
            actor=request.user,
            job_id=job_id,
            correlation_id=_correlation_id(request),
        )
    except PermissionDenied:
        return _error(
            request,
            status=403,
            code="OBJECT_PERMISSION_DENIED",
            message="취소 권한 검증에 실패했습니다.",
            action="소유자와 역할 권한을 다시 확인해 주세요.",
        )
    return _json_response(_project(request, cancelled))


def _research_service() -> ResearchExplorerService:
    from django.conf import settings

    return ResearchExplorerService(settings.RESEARCH_PATHS)


def _research_detail_level(request: HttpRequest) -> str | JsonResponse:
    value = str(request.GET.get("detail") or "summary")
    if value not in {"summary", "technical"}:
        return _error(
            request,
            status=400,
            code="DETAIL_LEVEL_INVALID",
            message="detail 값이 지원되는 범위를 벗어났습니다.",
            action="summary 또는 technical 중 하나를 사용해 주세요.",
        )
    return value


def _research_pagination(
    request: HttpRequest,
) -> tuple[int, int] | JsonResponse:
    try:
        limit = int(request.GET.get("limit", "25"))
        offset = int(request.GET.get("offset", "0"))
    except ValueError:
        limit, offset = 0, -1
    if not 1 <= limit <= 100 or offset < 0:
        return _error(
            request,
            status=400,
            code="PAGINATION_INVALID",
            message="페이지 범위가 허용된 한도를 벗어났습니다.",
            action="limit은 1~100, offset은 0 이상으로 지정해 주세요.",
        )
    return limit, offset


def _stable_research_identity(
    request: HttpRequest, *values: str
) -> JsonResponse | None:
    if all(_STABLE_RESEARCH_ID.fullmatch(str(value)) for value in values):
        return None
    return _error(
        request,
        status=400,
        code="RESEARCH_ID_INVALID",
        message="연구 객체 식별자가 올바르지 않습니다.",
        action="API가 반환한 logical_id와 version을 그대로 사용해 주세요.",
    )


def _research_query_error(request: HttpRequest, exc: BaseException) -> JsonResponse:
    reason = str(exc)
    if reason == "research_resource_not_found":
        return _error(
            request,
            status=404,
            code="RESEARCH_RESOURCE_NOT_FOUND",
            message="요청한 연구 증거를 찾을 수 없습니다.",
            action="stable ID, version 및 현재 registry 상태를 확인해 주세요.",
        )
    if any(
        marker in reason
        for marker in (
            "filter_invalid",
            "query_invalid",
            "detail_level_invalid",
            "record_type_invalid",
            "section_invalid",
            "identity_invalid",
        )
    ):
        return _error(
            request,
            status=400,
            code="RESEARCH_QUERY_INVALID",
            message="연구 탐색 필터 또는 식별자가 올바르지 않습니다.",
            action="OpenAPI의 허용 필터와 stable ID를 확인해 주세요.",
        )
    return _error(
        request,
        status=503,
        code="RESEARCH_REGISTRY_UNAVAILABLE",
        message="검증된 연구 registry를 현재 조회할 수 없습니다.",
        action="registry 무결성 점검 결과와 문의 ID를 관리자에게 전달해 주세요.",
        retryable=True,
    )


def _research_audit_error(request: HttpRequest) -> JsonResponse:
    return _error(
        request,
        status=503,
        code="AUDIT_UNAVAILABLE",
        message="조회 감사 기록을 저장할 수 없습니다.",
        action="감사 저장소 상태를 확인한 뒤 같은 조회를 다시 요청해 주세요.",
        retryable=True,
    )


def _research_list_response(
    request: HttpRequest,
    *,
    records: tuple[dict[str, Any], ...],
    filters: dict[str, str],
    detail_level: str,
    audit_type: str,
) -> JsonResponse:
    pagination = _research_pagination(request)
    if isinstance(pagination, JsonResponse):
        return pagination
    limit, offset = pagination
    count = len(records)
    items = records[offset : offset + limit]

    def page_url(new_offset: int) -> str:
        values = {
            **filters,
            "detail": detail_level,
            "limit": str(limit),
            "offset": str(new_offset),
        }
        return f"{request.path}?{urlencode(values)}"

    try:
        audit_research_exploration_read(
            request,
            object_type=audit_type,
            object_id="collection",
            filters=filters | {"limit": limit, "offset": offset},
            detail_level=detail_level,
        )
    except PermissionDenied:
        return _error(
            request,
            status=403,
            code="PERMISSION_DENIED",
            message="연구 탐색 권한을 확인할 수 없습니다.",
            action="research.view 권한이 포함된 역할을 확인해 주세요.",
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _research_audit_error(request)
    return _json_response(
        ResearchListResponse(
            page=ResearchPageMetadata(
                count=count,
                limit=limit,
                offset=offset,
                next=(page_url(offset + limit) if offset + limit < count else None),
                previous=(page_url(max(0, offset - limit)) if offset > 0 else None),
                filters=filters,
                detail_level=cast(Literal["summary", "technical"], detail_level),
            ),
            items=tuple(ResearchResource.model_validate(item) for item in items),
        )
    )


def _research_detail_response(
    request: HttpRequest,
    *,
    record: dict[str, Any],
    detail_level: str,
    object_type: str,
) -> JsonResponse:
    try:
        audit_research_exploration_read(
            request,
            object_type=object_type,
            object_id=f"{record['logical_id']}:{record['version']}",
            detail_level=detail_level,
        )
    except PermissionDenied:
        return _error(
            request,
            status=403,
            code="PERMISSION_DENIED",
            message="연구 탐색 권한을 확인할 수 없습니다.",
            action="research.view 권한이 포함된 역할을 확인해 주세요.",
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _research_audit_error(request)
    return _json_response(ResearchResource.model_validate(record))


def research_lineage_list(request: HttpRequest) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, RESEARCH_EXPLORATION_PERMISSION):
        return denied
    detail = _research_detail_level(request)
    if isinstance(detail, JsonResponse):
        return detail
    filters = {
        key: str(request.GET.get(key) or "")
        for key in ("record_type", "logical_id")
        if request.GET.get(key)
    }
    try:
        records = _research_service().list_records(
            section="lineage", filters=filters, detail_level=detail
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _research_query_error(request, exc)
    return _research_list_response(
        request,
        records=records,
        filters=filters,
        detail_level=detail,
        audit_type="research_lineage_collection",
    )


def research_lineage_detail(
    request: HttpRequest, record_type: str, logical_id: str, version: str
) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, RESEARCH_EXPLORATION_PERMISSION):
        return denied
    if invalid_id := _stable_research_identity(request, logical_id, version):
        return invalid_id
    detail = _research_detail_level(request)
    if isinstance(detail, JsonResponse):
        return detail
    try:
        record = _research_service().get_record(
            section="lineage",
            logical_id=logical_id,
            version=version,
            record_type=record_type,
            detail_level=detail,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _research_query_error(request, exc)
    return _research_detail_response(
        request,
        record=record,
        detail_level=detail,
        object_type=record_type,
    )


def validation_decision_list(request: HttpRequest) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, RESEARCH_EXPLORATION_PERMISSION):
        return denied
    detail = _research_detail_level(request)
    if isinstance(detail, JsonResponse):
        return detail
    filters = {
        key: str(request.GET.get(key) or "")
        for key in ("hypothesis_id", "decision", "failure_type", "negative_only")
        if request.GET.get(key)
    }
    try:
        records = _research_service().list_records(
            section="decisions", filters=filters, detail_level=detail
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _research_query_error(request, exc)
    return _research_list_response(
        request,
        records=records,
        filters=filters,
        detail_level=detail,
        audit_type="validation_decision_collection",
    )


def validation_decision_detail(
    request: HttpRequest, logical_id: str, version: str
) -> JsonResponse:
    return _generic_research_detail(
        request,
        section="decisions",
        logical_id=logical_id,
        version=version,
        object_type="validation_decision",
    )


def prospective_validation_list(request: HttpRequest) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, RESEARCH_EXPLORATION_PERMISSION):
        return denied
    detail = _research_detail_level(request)
    if isinstance(detail, JsonResponse):
        return detail
    filters = {
        key: str(request.GET.get(key) or "")
        for key in ("validation_id", "status")
        if request.GET.get(key)
    }
    try:
        records = _research_service().list_records(
            section="prospective", filters=filters, detail_level=detail
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _research_query_error(request, exc)
    return _research_list_response(
        request,
        records=records,
        filters=filters,
        detail_level=detail,
        audit_type="prospective_validation_collection",
    )


def prospective_validation_detail(
    request: HttpRequest, logical_id: str, version: str
) -> JsonResponse:
    return _generic_research_detail(
        request,
        section="prospective",
        logical_id=logical_id,
        version=version,
        object_type="prospective_validation",
    )


def dataset_artifact_list(request: HttpRequest) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, RESEARCH_EXPLORATION_PERMISSION):
        return denied
    detail = _research_detail_level(request)
    if isinstance(detail, JsonResponse):
        return detail
    filters = {
        key: str(request.GET.get(key) or "")
        for key in (
            "artifact_id",
            "market",
            "interval",
            "provider_id",
            "dataset_id",
            "quality_status",
            "start_ts",
            "end_ts",
            "as_of_ts",
            "known_at",
        )
        if request.GET.get(key)
    }
    try:
        records = _research_service().list_records(
            section="datasets", filters=filters, detail_level=detail
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _research_query_error(request, exc)
    return _research_list_response(
        request,
        records=records,
        filters=filters,
        detail_level=detail,
        audit_type="dataset_artifact_collection",
    )


def dataset_artifact_detail(
    request: HttpRequest, logical_id: str, version: str
) -> JsonResponse:
    return _generic_research_detail(
        request,
        section="datasets",
        logical_id=logical_id,
        version=version,
        object_type="dataset_artifact",
    )


def feature_definition_list(request: HttpRequest) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, RESEARCH_EXPLORATION_PERMISSION):
        return denied
    detail = _research_detail_level(request)
    if isinstance(detail, JsonResponse):
        return detail
    filters = {
        key: str(request.GET.get(key) or "")
        for key in ("feature_id", "strategy", "input_name")
        if request.GET.get(key)
    }
    try:
        records = _research_service().list_records(
            section="features", filters=filters, detail_level=detail
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _research_query_error(request, exc)
    return _research_list_response(
        request,
        records=records,
        filters=filters,
        detail_level=detail,
        audit_type="feature_definition_collection",
    )


def feature_definition_detail(
    request: HttpRequest, logical_id: str, version: str
) -> JsonResponse:
    return _generic_research_detail(
        request,
        section="features",
        logical_id=logical_id,
        version=version,
        object_type="feature_definition",
    )


def research_package_list(request: HttpRequest) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, RESEARCH_EXPLORATION_PERMISSION):
        return denied
    detail = _research_detail_level(request)
    if isinstance(detail, JsonResponse):
        return detail
    filter_names = (
        "market",
        "instrument",
        "hypothesis_type",
        "status",
        "researcher",
        "dataset",
        "period_start",
        "period_end",
        "prospective_status",
    )
    filters = {
        key: str(request.GET.get(key) or "")
        for key in filter_names
        if request.GET.get(key)
    }
    try:
        records = _research_service().list_records(
            section="packages", filters=filters, detail_level=detail
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _research_query_error(request, exc)
    return _research_list_response(
        request,
        records=records,
        filters=filters,
        detail_level=detail,
        audit_type="research_package_collection",
    )


def research_package_detail(
    request: HttpRequest, logical_id: str, version: str
) -> JsonResponse:
    return _generic_research_detail(
        request,
        section="packages",
        logical_id=logical_id,
        version=version,
        object_type="research_package",
    )


def _generic_research_detail(
    request: HttpRequest,
    *,
    section: str,
    logical_id: str,
    version: str,
    object_type: str,
) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, RESEARCH_EXPLORATION_PERMISSION):
        return denied
    if invalid_id := _stable_research_identity(request, logical_id, version):
        return invalid_id
    detail = _research_detail_level(request)
    if isinstance(detail, JsonResponse):
        return detail
    try:
        record = _research_service().get_record(
            section=section,
            logical_id=logical_id,
            version=version,
            detail_level=detail,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _research_query_error(request, exc)
    return _research_detail_response(
        request,
        record=record,
        detail_level=detail,
        object_type=object_type,
    )


def research_package_lineage_view(
    request: HttpRequest, logical_id: str, version: str
) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, RESEARCH_EXPLORATION_PERMISSION):
        return denied
    if invalid_id := _stable_research_identity(request, logical_id, version):
        return invalid_id
    try:
        payload = _research_service().package_lineage(
            package_id=logical_id, version=version
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _research_query_error(request, exc)
    try:
        audit_research_exploration_read(
            request,
            object_type="research_package_lineage",
            object_id=f"{logical_id}:{version}",
            detail_level="technical",
        )
    except PermissionDenied:
        return _error(
            request,
            status=403,
            code="PERMISSION_DENIED",
            message="연구 탐색 권한을 확인할 수 없습니다.",
            action="research.view 권한이 포함된 역할을 확인해 주세요.",
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _research_audit_error(request)
    return _json_response(
        ResearchProjectionResponse(kind="research_package_lineage", payload=payload)
    )


def research_package_diff_view(request: HttpRequest) -> JsonResponse:
    if invalid := _require_method(request, "GET"):
        return invalid
    if denied := _require_permission(request, RESEARCH_EXPLORATION_PERMISSION):
        return denied
    values = {
        key: str(request.GET.get(key) or "")
        for key in (
            "left_package_id",
            "left_version",
            "right_package_id",
            "right_version",
        )
    }
    if invalid_id := _stable_research_identity(request, *values.values()):
        return invalid_id
    try:
        payload = _research_service().package_diff(**values)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _research_query_error(request, exc)
    try:
        audit_research_exploration_read(
            request,
            object_type="research_package_diff",
            object_id=(
                f"{values['left_package_id']}:{values['left_version']}:"
                f"{values['right_package_id']}:{values['right_version']}"
            ),
            filters=values,
            detail_level="technical",
        )
    except PermissionDenied:
        return _error(
            request,
            status=403,
            code="PERMISSION_DENIED",
            message="연구 탐색 권한을 확인할 수 없습니다.",
            action="research.view 권한이 포함된 역할을 확인해 주세요.",
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return _research_audit_error(request)
    return _json_response(
        ResearchProjectionResponse(kind="research_package_diff", payload=payload)
    )


def csrf_failure(
    request: HttpRequest,
    reason: str = "",
) -> JsonResponse | Any:
    if request.path.startswith("/api/"):
        return _error(
            request,
            status=403,
            code="CSRF_VERIFICATION_FAILED",
            message="요청 위조 방지 토큰을 확인하지 못했습니다.",
            action="현재 세션의 csrftoken 값을 X-CSRFToken 헤더로 보내 주세요.",
        )
    return django_csrf_failure(request, reason=reason)
