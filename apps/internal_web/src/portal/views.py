from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.views import LoginView, LogoutView
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from market_research.application import (
    ApplicationAuthorizationError,
    ReportComparisonRequest,
    ResearchApplicationService,
)
from market_research.research.governance import GovernanceError
from market_research.research_composition import builtin_strategy_registry

from .audit import append_web_audit_event
from .forms import (
    CandidateApprovalForm,
    HistoricalDecisionReportImportForm,
    HumanReviewForm,
    ManifestExperimentConflict,
    ManifestUploadForm,
)
from .governance import approve_job_candidate, load_review_context, record_job_review
from .jobs import (
    ActiveJobConflict,
    IdempotencyConflict,
    enqueue_research_job,
    jobs_visible_to,
    request_job_cancellation,
)
from .login_throttle import ThrottledAuthenticationForm
from .models import ManifestUpload, ResearchJob
from .presenters import build_safe_download_payload, load_safe_result, safe_error_message
from .reports import compare_visible_reports, list_visible_reports
from .security import actor_snapshot
from .storage import read_verified_manifest_bytes


STATUS_LABELS = {
    ResearchJob.Status.QUEUED: "대기 중",
    ResearchJob.Status.RUNNING: "실행 중",
    ResearchJob.Status.SUCCEEDED: "완료",
    ResearchJob.Status.FAILED: "실패",
    ResearchJob.Status.CANCEL_REQUESTED: "취소 요청됨",
    ResearchJob.Status.CANCELLED: "취소됨",
}

STAGE_LABELS = {
    "starting": "연구 환경을 준비하고 있습니다",
    "readiness_scan": "데이터 준비 상태를 확인하고 있습니다",
    "workload_estimate": "예상 작업량을 계산하고 있습니다",
    "validation": "연구 검증을 실행하고 있습니다",
    "complete": "결과와 해시를 확인했습니다",
    "failed": "작업을 안전하게 중단했습니다",
    "cancelled": "취소가 완료되었습니다",
}


class PortalLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True
    authentication_form = ThrottledAuthenticationForm


class PortalLogoutView(LogoutView):
    next_page = reverse_lazy("portal:login")


def _base_context(request: HttpRequest, *, active_nav: str) -> dict[str, Any]:
    groups = list(request.user.groups.values_list("name", flat=True))
    role_names = {
        "research_admin": "관리자",
        "research_approver": "승인자",
        "research_reviewer": "검토자",
        "research_runner": "연구 실행자",
        "research_viewer": "일반 사용자",
    }
    role_label = next((role_names[item] for item in groups if item in role_names), None)
    if request.user.is_superuser:
        role_label = "관리자"
    return {"active_nav": active_nav, "role_label": role_label or "일반 사용자"}


def _correlation_id(request: HttpRequest) -> str:
    return str(getattr(request, "correlation_id", uuid.uuid4()))


def _manifests_visible_to(user: Any):
    queryset = ManifestUpload.objects.select_related("owner")
    if user.has_perm("portal.view_all_research_manifests"):
        return queryset
    return queryset.filter(owner=user)


def _manifest_summary(record: ManifestUpload) -> ManifestUpload:
    """Attach non-authoritative display fields without changing stored metadata."""

    try:
        payload = json.loads(read_verified_manifest_bytes(record).decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        payload = {}
    record.market = str(payload.get("market") or "—")  # type: ignore[attr-defined]
    record.interval = str(payload.get("interval") or "—")  # type: ignore[attr-defined]
    return record


def _decorate_job(job: ResearchJob) -> ResearchJob:
    job.display_name = job.manifest.display_name  # type: ignore[attr-defined]
    job.public_id = str(job.pk).split("-")[0].upper()  # type: ignore[attr-defined]
    job.status_label = STATUS_LABELS.get(job.status, job.status)  # type: ignore[attr-defined]
    job.capability_label = dict(ResearchJob.Capability.choices).get(  # type: ignore[attr-defined]
        job.capability_id,
        job.capability_id,
    )
    job.progress_message = _progress_message(job)  # type: ignore[attr-defined]
    job.safe_error_message = safe_error_message(job)  # type: ignore[attr-defined]
    job.can_cancel = (  # type: ignore[attr-defined]
        job.status in {ResearchJob.Status.QUEUED, ResearchJob.Status.RUNNING}
        and (
            (
                job.owner_id == getattr(job, "_viewer_id", None)
                and getattr(job, "_viewer_can_cancel", False)
            )
            or getattr(job, "_viewer_can_manage", False)
        )
    )
    return job


def _progress_message(job: ResearchJob) -> str:
    if job.status == ResearchJob.Status.QUEUED:
        return "대기열에 안전하게 저장되었습니다. 브라우저를 닫아도 유지됩니다."
    if job.status == ResearchJob.Status.RUNNING:
        return "현재 단계가 끝나면 다음 상태와 heartbeat가 기록됩니다."
    if job.status == ResearchJob.Status.CANCEL_REQUESTED:
        return "안전한 단계 경계에서 취소를 적용하고 있습니다."
    return "최종 상태와 결과 무결성이 저장되었습니다."


@login_required
@permission_required("portal.view_researchjob", raise_exception=True)
@require_GET
def dashboard(request: HttpRequest) -> HttpResponse:
    visible = jobs_visible_to(request.user)
    now = timezone.now()
    metrics = visible.aggregate(
        active=Count(
            "id",
            filter=Q(
                status__in=(
                    ResearchJob.Status.QUEUED,
                    ResearchJob.Status.RUNNING,
                    ResearchJob.Status.CANCEL_REQUESTED,
                )
            ),
        ),
        review=Count(
            "id",
            filter=Q(
                capability_id=ResearchJob.Capability.VALIDATE,
                status=ResearchJob.Status.SUCCEEDED,
                research_outcome=ResearchJob.ResearchOutcome.PASS,
            ),
        ),
        completed=Count(
            "id",
            filter=Q(
                status=ResearchJob.Status.SUCCEEDED,
                finished_at__gte=now - timedelta(days=7),
            ),
        ),
    )
    # Integrity is verified when each result is opened or downloaded.  The
    # dashboard must not claim a global state it has not just checked.
    metrics["integrity"] = "결과별 확인"
    jobs = [_decorate_job(job) for job in visible[:6]]
    return render(
        request,
        "portal/dashboard.html",
        {**_base_context(request, active_nav="dashboard"), "metrics": metrics, "jobs": jobs},
    )


@login_required
@permission_required("portal.upload_research_manifest", raise_exception=True)
def manifest_upload(request: HttpRequest) -> HttpResponse:
    form = ManifestUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            record, created = form.save(
                owner=request.user,
                correlation_id=_correlation_id(request),
            )
        except ManifestExperimentConflict:
            form.add_error(
                None,
                "동일한 연구 식별값이 이미 등록되어 있습니다. 새 식별값으로 다시 등록해 주세요.",
            )
        except (PermissionDenied, ValidationError, ValueError):
            form.add_error(None, "파일을 안전하게 저장하지 못했습니다. 입력 내용을 확인해 주세요.")
        else:
            messages.success(
                request,
                "연구 정의 파일을 등록했습니다." if created else "동일한 원본 파일을 다시 사용합니다.",
            )
            return redirect("portal:manifest-detail", pk=record.pk)
    return render(
        request,
        "portal/manifest_upload.html",
        {**_base_context(request, active_nav="new"), "form": form},
    )


@login_required
@permission_required("portal.view_manifestupload", raise_exception=True)
@require_GET
def manifest_detail(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    record = get_object_or_404(_manifests_visible_to(request.user), pk=pk)
    record = _manifest_summary(record)
    return render(
        request,
        "portal/manifest_detail.html",
        {
            **_base_context(request, active_nav="new"),
            "manifest": record,
            "idempotency_key": uuid.uuid4(),
        },
    )


@login_required
@permission_required("portal.submit_research_job", raise_exception=True)
@require_POST
def manifest_preflight(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    record = get_object_or_404(_manifests_visible_to(request.user), pk=pk)
    return _enqueue_and_redirect(
        request,
        manifest=record,
        capability_id=ResearchJob.Capability.PREFLIGHT,
    )


def _enqueue_and_redirect(
    request: HttpRequest,
    *,
    manifest: ManifestUpload,
    capability_id: str,
    source_preflight_job: ResearchJob | None = None,
) -> HttpResponse:
    try:
        key = str(uuid.UUID(str(request.POST.get("idempotency_key") or "")))
    except ValueError:
        messages.error(request, "요청 식별값이 만료되었습니다. 화면을 새로 고쳐 다시 시도해 주세요.")
        return redirect("portal:manifest-detail", pk=manifest.pk)

    active = ResearchJob.objects.filter(
        owner=request.user,
        status__in=(
            ResearchJob.Status.QUEUED,
            ResearchJob.Status.RUNNING,
            ResearchJob.Status.CANCEL_REQUESTED,
        ),
    ).first()
    if active is not None:
        messages.error(request, "이미 진행 중인 작업이 있습니다. 완료 또는 취소 후 다시 요청해 주세요.")
        return redirect("portal:job-detail", pk=active.pk)

    try:
        enqueued = enqueue_research_job(
            owner=request.user,
            manifest=manifest,
            capability_id=capability_id,
            idempotency_key=key,
            correlation_id=_correlation_id(request),
            source_preflight_job=source_preflight_job,
        )
    except IdempotencyConflict:
        messages.error(request, "동일한 요청 식별값이 다른 입력에 사용되었습니다.")
        return redirect("portal:manifest-detail", pk=manifest.pk)
    except ActiveJobConflict as exc:
        messages.error(
            request,
            "이미 진행 중인 작업이 있습니다. 완료 또는 취소 후 다시 요청해 주세요.",
        )
        return redirect("portal:job-detail", pk=exc.existing_job.pk)
    except (PermissionDenied, ValidationError):
        raise PermissionDenied("research_job_submission_denied")
    if not enqueued.created:
        messages.info(request, "같은 요청이 이미 등록되어 기존 작업으로 이동합니다.")
    return redirect("portal:job-detail", pk=enqueued.job.pk)


@login_required
@permission_required("portal.view_researchjob", raise_exception=True)
@require_GET
def job_list(request: HttpRequest) -> HttpResponse:
    queryset = jobs_visible_to(request.user)
    current_status = str(request.GET.get("status") or "")
    if current_status:
        if current_status not in ResearchJob.Status.values:
            raise Http404("unknown job status")
        queryset = queryset.filter(status=current_status)
    page = Paginator(queryset, 25).get_page(request.GET.get("page"))
    page.object_list = [_decorate_job(job) for job in page.object_list]
    return render(
        request,
        "portal/job_list.html",
        {
            **_base_context(request, active_nav="jobs"),
            "page_obj": page,
            "status_choices": [(value, STATUS_LABELS[value]) for value in ResearchJob.Status.values],
            "current_status": current_status,
        },
    )


def _job_for_request(request: HttpRequest, pk: uuid.UUID) -> ResearchJob:
    job = get_object_or_404(jobs_visible_to(request.user), pk=pk)
    job._viewer_id = request.user.pk  # type: ignore[attr-defined]
    job._viewer_can_cancel = request.user.has_perm(  # type: ignore[attr-defined]
        "portal.cancel_own_research_job"
    )
    job._viewer_can_manage = request.user.has_perm("portal.manage_research_web")  # type: ignore[attr-defined]
    return _decorate_job(job)


@login_required
@permission_required("portal.view_researchjob", raise_exception=True)
@require_GET
def job_detail(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    job = _job_for_request(request, pk)
    result: dict[str, Any] = {}
    if job.status == ResearchJob.Status.SUCCEEDED:
        try:
            result, _safe_payload = load_safe_result(job)
        except ValidationError:
            result = {
                "title": "결과 무결성을 확인할 수 없습니다",
                "summary": "원본 결과를 표시하지 않았습니다. 관리자에게 문의 ID를 전달해 주세요.",
                "final_status": "확인 필요",
                "integrity_status": "검증 실패",
            }
    return render(
        request,
        "portal/job_detail.html",
        {
            **_base_context(request, active_nav="jobs"),
            "job": job,
            "result": result,
            "idempotency_key": uuid.uuid4(),
        },
    )


@login_required
@permission_required("portal.view_researchjob", raise_exception=True)
@require_GET
def job_status(request: HttpRequest, pk: uuid.UUID) -> JsonResponse:
    job = _job_for_request(request, pk)
    return JsonResponse(
        {
            "status": job.status,
            "status_label": STATUS_LABELS.get(job.status, job.status),
            "stage": STAGE_LABELS.get(job.progress_stage, job.progress_stage or "대기 중"),
            "message": job.progress_message,
            "updated_at": timezone.localtime(job.updated_at).strftime("%H:%M:%S"),
            "terminal": job.is_terminal,
            "version": job.version,
        }
    )


@login_required
@require_POST
def job_cancel(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    _job_for_request(request, pk)
    job = request_job_cancellation(
        actor=request.user,
        job_id=pk,
        correlation_id=_correlation_id(request),
    )
    if job.status in {
        ResearchJob.Status.CANCEL_REQUESTED,
        ResearchJob.Status.CANCELLED,
    }:
        messages.info(request, "취소 요청을 기록했습니다.")
    else:
        messages.info(request, "이미 완료된 작업은 취소 상태로 바꾸지 않았습니다.")
    return redirect("portal:job-detail", pk=pk)


@login_required
@permission_required("portal.submit_research_job", raise_exception=True)
@require_POST
def job_submit_validation(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    source = _job_for_request(request, pk)
    if source.capability_id != ResearchJob.Capability.PREFLIGHT or source.status != ResearchJob.Status.SUCCEEDED:
        raise PermissionDenied("validation_requires_completed_preflight")
    try:
        _summary, payload = load_safe_result(source)
    except ValidationError as exc:
        raise PermissionDenied("preflight_result_integrity_failed") from exc
    if payload.get("report_kind") != "internal_web_preflight" or payload.get("status") != "PASS":
        messages.error(request, "사전 점검의 보완 항목을 해결한 뒤 검증할 수 있습니다.")
        return redirect("portal:job-detail", pk=pk)

    # Current engine artifacts are experiment-scoped.  Until a reviewed
    # run-scoped namespace exists, repeated validation is deliberately blocked.
    prior = ResearchJob.objects.filter(
        manifest=source.manifest,
        capability_id=ResearchJob.Capability.VALIDATE,
    ).order_by("created_at").first()
    if prior is not None:
        messages.info(request, "이 입력의 검증 작업이 이미 존재하여 해당 기록으로 이동합니다.")
        return redirect("portal:job-detail", pk=prior.pk)
    return _enqueue_and_redirect(
        request,
        manifest=source.manifest,
        capability_id=ResearchJob.Capability.VALIDATE,
        source_preflight_job=source,
    )


@login_required
@permission_required("portal.view_researchjob", raise_exception=True)
@require_GET
def job_download(request: HttpRequest, pk: uuid.UUID) -> JsonResponse:
    job = _job_for_request(request, pk)
    if job.status != ResearchJob.Status.SUCCEEDED:
        raise Http404("job result unavailable")
    try:
        _summary, payload = load_safe_result(job)
    except ValidationError as exc:
        raise Http404("job result integrity failure") from exc
    try:
        download_payload = build_safe_download_payload(job, payload)
    except ValueError as exc:
        raise Http404("job result projection integrity failure") from exc
    response = JsonResponse(
        download_payload,
        json_dumps_params={"ensure_ascii": False, "indent": 2, "sort_keys": True},
    )
    response["Content-Disposition"] = (
        f'attachment; filename="research-result-{str(job.pk)[:8]}.json"'
    )
    return response


def _report_catalog_context(
    request: HttpRequest,
    *,
    comparison: dict[str, Any] | None = None,
    import_form: HistoricalDecisionReportImportForm | None = None,
) -> dict[str, Any]:
    try:
        reports = list_visible_reports(request.user, limit=50, offset=0)
    except ApplicationAuthorizationError as exc:
        raise PermissionDenied("research_report_view_permission_required") from exc
    return {
        **_base_context(request, active_nav="reports"),
        "reports": reports,
        "comparison": comparison,
        "report_import_form": (
            import_form
            if import_form is not None
            else (
                HistoricalDecisionReportImportForm(operator=request.user)
                if request.user.has_perm("portal.import_research_report")
                else None
            )
        ),
    }


@login_required
@permission_required("portal.view_researchjob", raise_exception=True)
@require_GET
def report_list(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "portal/report_catalog.html",
        _report_catalog_context(request),
    )


@login_required
@permission_required("portal.import_research_report", raise_exception=True)
@require_POST
def report_import(request: HttpRequest) -> HttpResponse:
    form = HistoricalDecisionReportImportForm(
        request.POST,
        operator=request.user,
    )
    if not form.is_valid():
        messages.error(request, "가져오기 입력값을 다시 확인해 주세요.")
        return render(
            request,
            "portal/report_catalog.html",
            _report_catalog_context(request, import_form=form),
            status=400,
        )
    try:
        result = form.save(correlation_id=_correlation_id(request))
    except (PermissionDenied, ValidationError, ValueError):
        messages.error(
            request,
            "보고서의 경로, hash 또는 증거 바인딩을 확인하지 못했습니다.",
        )
        return render(
            request,
            "portal/report_catalog.html",
            _report_catalog_context(request, import_form=form),
            status=400,
        )
    messages.success(
        request,
        (
            "검증된 CLI 보고서를 카탈로그에 등록했습니다."
            if result.created
            else "동일한 검증 보고서가 이미 등록되어 기존 항목을 사용합니다."
        ),
    )
    return redirect("portal:report-list")


@login_required
@permission_required("portal.view_researchjob", raise_exception=True)
@require_POST
def report_compare(request: HttpRequest) -> HttpResponse:
    try:
        comparison_request = ReportComparisonRequest(
            request_id=_correlation_id(request),
            report_ids=tuple(request.POST.getlist("report_ids")),
        )
        comparison = compare_visible_reports(
            request.user,
            comparison_request,
            service=ResearchApplicationService(
                paths=settings.RESEARCH_PATHS,
                strategy_registry=builtin_strategy_registry(),
            ),
        )
    except (ApplicationAuthorizationError, ValidationError, ValueError):
        messages.error(
            request,
            "비교할 수 있는 검증 완료 보고서를 2개 이상 선택해 주세요.",
        )
        return redirect("portal:report-list")

    actor_id, _roles, _permissions = actor_snapshot(request.user)
    append_web_audit_event(
        action="research_reports_compared",
        actor_id=actor_id,
        object_type="report_comparison",
        object_id=str(comparison["content_hash"]),
        correlation_id=_correlation_id(request),
        details={
            "source_report_ids": comparison["source_report_ids"],
            "source_report_hashes": comparison["source_report_hashes"],
            "source_comparison_hash": comparison["source_comparison_hash"],
            "projection_hash": comparison["content_hash"],
        },
    )
    return render(
        request,
        "portal/report_catalog.html",
        _report_catalog_context(request, comparison=comparison),
    )


def _require_review_or_approval_permission(request: HttpRequest) -> None:
    if not (
        request.user.has_perm("portal.record_research_review")
        or request.user.has_perm("portal.approve_research_candidate")
    ):
        raise PermissionDenied("research_review_or_approval_permission_required")


def _can_approve_candidate(user: Any) -> bool:
    """Require both capability permission and the explicit approval duty role.

    Django superuser status is not a substitute for separation-of-duties role
    assignment.  A superuser must also belong to ``research_approver`` before
    the approval form is exposed; the application service independently
    enforces the same role at execution time.
    """

    return bool(
        user.has_perm("portal.approve_research_candidate")
        and user.groups.filter(name="research_approver").exists()
    )


@login_required
@require_GET
def review_queue(request: HttpRequest) -> HttpResponse:
    _require_review_or_approval_permission(request)
    jobs = jobs_visible_to(request.user).filter(
        capability_id=ResearchJob.Capability.VALIDATE,
        status=ResearchJob.Status.SUCCEEDED,
        research_outcome=ResearchJob.ResearchOutcome.PASS,
    )
    return render(
        request,
        "portal/review_queue.html",
        {
            **_base_context(request, active_nav="review"),
            "jobs": [_decorate_job(job) for job in jobs[:50]],
        },
    )


@login_required
@require_GET
def review_detail(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    _require_review_or_approval_permission(request)
    job = _job_for_request(request, pk)
    context: dict[str, Any] | None = None
    try:
        context = load_review_context(job)
    except (GovernanceError, ValidationError, OSError, ValueError):
        context = None
    originator_ids = {str(job.owner_id), str(job.actor_id or "")}
    return render(
        request,
        "portal/review_detail.html",
        {
            **_base_context(request, active_nav="review"),
            "job": job,
            "governance": context,
            "is_originator": str(request.user.pk) in originator_ids,
            "can_approve": _can_approve_candidate(request.user),
            "review_form": HumanReviewForm(),
            "approval_form": CandidateApprovalForm(),
        },
    )


@login_required
@permission_required("portal.record_research_review", raise_exception=True)
@require_POST
def review_record(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    job = _job_for_request(request, pk)
    form = HumanReviewForm(request.POST)
    if form.is_valid():
        try:
            record_job_review(
                user=request.user,
                job=job,
                cleaned_data=form.cleaned_data,
                correlation_id=_correlation_id(request),
            )
        except (
            ApplicationAuthorizationError,
            GovernanceError,
            ValidationError,
            OSError,
            ValueError,
        ):
            messages.error(
                request,
                "검토 의견을 기록하지 못했습니다. 결과 상태와 업무 분리 조건을 확인해 주세요.",
            )
        else:
            messages.success(request, "검토 의견과 근거 hash를 기록했습니다.")
    else:
        messages.error(request, "검토 의견의 필수 입력을 확인해 주세요.")
    return redirect("portal:review-detail", pk=pk)


@login_required
@permission_required("portal.approve_research_candidate", raise_exception=True)
@require_POST
def review_approve(request: HttpRequest, pk: uuid.UUID) -> HttpResponse:
    if not _can_approve_candidate(request.user):
        raise PermissionDenied("research_approver_role_required")
    job = _job_for_request(request, pk)
    form = CandidateApprovalForm(request.POST)
    if not form.is_valid():
        messages.error(request, "승인 근거, 확인 항목, 현재 비밀번호를 확인해 주세요.")
        return redirect("portal:review-detail", pk=pk)
    if not request.user.check_password(str(form.cleaned_data["password"])):
        messages.error(request, "현재 비밀번호를 확인하지 못해 승인하지 않았습니다.")
        return redirect("portal:review-detail", pk=pk)
    try:
        result = approve_job_candidate(
            user=request.user,
            job=job,
            cleaned_data=form.cleaned_data,
            correlation_id=_correlation_id(request),
        )
    except (
        ApplicationAuthorizationError,
        GovernanceError,
        ValidationError,
        OSError,
        ValueError,
    ):
        messages.error(
            request,
            "승인을 기록하지 못했습니다. 최신 결과 hash, 미해결 요구사항, 업무 분리 조건을 확인해 주세요.",
        )
    else:
        messages.success(
            request,
            f"후보 승인을 기록했습니다. 승인 hash {result['approval_hash']}",
        )
    return redirect("portal:review-detail", pk=pk)


def error_response(
    request: HttpRequest,
    *,
    status: int,
    title: str,
    message: str,
) -> HttpResponse:
    if not getattr(request.user, "is_authenticated", False):
        return redirect("portal:login")
    return render(
        request,
        "portal/error.html",
        {
            **_base_context(request, active_nav=""),
            "error_title": title,
            "error_message": message,
            "correlation_id": _correlation_id(request),
        },
        status=status,
    )


def permission_denied(request: HttpRequest, exception: Exception) -> HttpResponse:
    return error_response(
        request,
        status=403,
        title="이 작업을 수행할 권한이 없습니다",
        message="현재 역할에서 사용할 수 없는 기능입니다. 필요한 경우 관리자에게 권한을 요청하세요.",
    )


def not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    return error_response(
        request,
        status=404,
        title="요청한 기록을 찾을 수 없습니다",
        message="삭제된 기록이거나 접근 권한이 없는 주소일 수 있습니다.",
    )


def server_error(request: HttpRequest) -> HttpResponse:
    return error_response(
        request,
        status=500,
        title="요청을 안전하게 완료하지 못했습니다",
        message="입력은 보존되었습니다. 문의 ID를 관리자에게 전달해 주세요.",
    )
