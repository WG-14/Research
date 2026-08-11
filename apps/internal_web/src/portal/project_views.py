"""Authenticated Django adapter for the UI-neutral research-project service."""

from __future__ import annotations

import re
from typing import Any

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from pydantic import ValidationError as PydanticValidationError

from market_research.application import (
    ActorContext,
    ApplicationAuthorizationError,
    ResearchProject,
    ResearchProjectApplicationService,
    ResearchProjectAuthorizationError,
    ResearchProjectConflictError,
    ResearchProjectCreateRequest,
    ResearchProjectError,
    ResearchProjectImpactQueryRequest,
    ResearchProjectIntegrityError,
    ResearchProjectMembersRequest,
    ResearchProjectNotFoundError,
    ResearchProjectObjectKind,
    ResearchProjectObjectRefInput,
    ResearchProjectPermission,
    ResearchProjectQueryRequest,
    ResearchProjectReferenceRequest,
    ResearchProjectRevisionRequest,
    ResearchProjectSearchRequest,
    ResearchProjectTransitionRequest,
    ResearchProjectWorkspaceRequest,
    has_research_project_permission,
    project_permission_for_reference,
)

from .audit import append_web_audit_event
from .security import actor_snapshot, sanitize_audit_details


PROJECT_VIEW_PERMISSION = "portal.view_research_project"
PROJECT_CREATE_PERMISSION = "portal.create_research_project"
PROJECT_MANAGE_PERMISSION = "portal.manage_research_project"
PROJECT_WRITE_PERMISSION = "portal.write_research_project"
PROJECT_COMPUTE_PERMISSION = "portal.compute_research_project"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_PROJECT_ROLES = (
    "OWNER",
    "RESEARCHER",
    "DATA_STEWARD",
    "VALIDATOR",
    "REVIEWER",
    "PUBLISHER",
    "VIEWER",
)
_REFERENCE_KINDS = (
    "HYPOTHESIS",
    "DATASET",
    "CODE",
    "EXPERIMENT",
    "RESULT",
    "VERIFICATION",
    "REVIEW",
    "PACKAGE",
)
_TRANSITION_STATUSES = (
    "ACTIVE",
    "CHALLENGED",
    "SUPERSEDED",
    "DEPRECATED",
    "REJECTED",
    "ARCHIVED",
)


class ProjectCreateForm(forms.Form):
    project_id = forms.RegexField(
        regex=_IDENTIFIER,
        max_length=255,
        label="Project ID",
    )
    title = forms.CharField(max_length=500, label="제목")
    research_question = forms.CharField(
        max_length=10_000,
        widget=forms.Textarea,
        label="연구 질문",
    )
    asset_classes = forms.CharField(
        max_length=1_000,
        label="자산군",
        help_text="쉼표로 구분합니다.",
    )
    markets = forms.CharField(
        max_length=1_000,
        label="시장",
        help_text="쉼표로 구분합니다.",
    )
    reason = forms.CharField(max_length=4_000, widget=forms.Textarea, label="사유")


class ProjectRevisionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    title = forms.CharField(max_length=500, label="제목")
    research_question = forms.CharField(
        max_length=10_000,
        widget=forms.Textarea,
        label="연구 질문",
    )
    asset_classes = forms.CharField(max_length=1_000, label="자산군")
    markets = forms.CharField(max_length=1_000, label="시장")
    reason = forms.CharField(max_length=4_000, widget=forms.Textarea, label="사유")


class ProjectMembersForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    members = forms.CharField(
        max_length=20_000,
        widget=forms.Textarea,
        label="구성원",
        help_text="한 줄에 actor_id,ROLE 형식으로 입력합니다.",
    )
    reason = forms.CharField(max_length=4_000, widget=forms.Textarea, label="사유")

    def clean_members(self) -> tuple[dict[str, str], ...]:
        values: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_line in str(self.cleaned_data["members"]).splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = tuple(part.strip() for part in line.split(","))
            if (
                len(parts) != 2
                or _IDENTIFIER.fullmatch(parts[0]) is None
                or parts[1] not in _PROJECT_ROLES
                or parts[0] in seen
            ):
                raise forms.ValidationError("구성원 형식이 올바르지 않습니다.")
            seen.add(parts[0])
            values.append({"actor_id": parts[0], "role": parts[1]})
        if not values:
            raise forms.ValidationError("구성원을 한 명 이상 입력해 주세요.")
        return tuple(values)


class ProjectReferenceForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    object_id = forms.RegexField(regex=_IDENTIFIER, max_length=255)
    version = forms.RegexField(regex=_IDENTIFIER, max_length=255)
    content_hash = forms.RegexField(regex=r"^sha256:[0-9a-f]{64}$", max_length=71)
    reason = forms.CharField(max_length=4_000, widget=forms.Textarea)

    def __init__(
        self,
        *args: Any,
        allowed_kinds: tuple[str, ...] = _REFERENCE_KINDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.fields["kind"] = forms.ChoiceField(
            choices=tuple((value, value) for value in allowed_kinds),
            help_text=(
                "이 화면은 authoritative artifact를 해석하지 않습니다. "
                "종류·ID·버전·SHA-256에 고정된 typed reference만 기록합니다."
            ),
        )


class ProjectTransitionForm(forms.Form):
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    to_status = forms.ChoiceField(
        choices=tuple((value, value) for value in _TRANSITION_STATUSES)
    )
    superseded_by_project_id = forms.RegexField(
        regex=_IDENTIFIER,
        max_length=255,
        required=False,
    )
    reason = forms.CharField(max_length=4_000, widget=forms.Textarea)


def _service() -> ResearchProjectApplicationService:
    return ResearchProjectApplicationService(settings.RESEARCH_PATHS)


def _actor(request: HttpRequest) -> ActorContext:
    actor_id, roles, permissions = actor_snapshot(request.user)
    user: Any = request.user
    display_name = (
        str(user.get_full_name()).strip() or str(user.get_username()) or actor_id
    )
    return ActorContext(
        actor_id=actor_id,
        display_name=display_name,
        roles=tuple(roles),
        permissions=frozenset(permissions),
        source="web",
    )


def _request_metadata(request: HttpRequest, *, reason: str) -> dict[str, Any]:
    correlation_id = str(getattr(request, "correlation_id", "")).strip()
    if not correlation_id:
        raise ValueError("project_request_correlation_id_required")
    return {
        "request_id": correlation_id,
        "idempotency_key": correlation_id,
        "event_id": f"web-{correlation_id}",
        "recorded_at": timezone.now().isoformat(),
        "reason": reason,
        "actor": _actor(request),
    }


def _csv(value: object) -> tuple[str, ...]:
    values = tuple(part.strip() for part in str(value).split(",") if part.strip())
    if not values or len(values) != len(set(values)):
        raise ValueError("project_scope_invalid")
    return values


def _project_context(
    request: HttpRequest,
    *,
    project: dict[str, Any],
    form: forms.Form | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    authority = ResearchProject.from_dict(project)
    actor_id, _roles, _permissions = actor_snapshot(request.user)

    def allows(
        django_permission: str,
        project_permission: ResearchProjectPermission,
    ) -> bool:
        return bool(
            request.user.has_perm(django_permission)
            and has_research_project_permission(
                authority,
                actor_id=actor_id,
                permission=project_permission,
            )
        )

    reference_kinds = _allowed_reference_kinds(
        request,
        project=authority,
    )
    return {
        "active_nav": "projects",
        "project": project,
        "form": form,
        "action": action,
        "can_revise_project": allows(
            PROJECT_MANAGE_PERMISSION,
            ResearchProjectPermission.REVISE,
        ),
        "can_manage_project_members": allows(
            PROJECT_MANAGE_PERMISSION,
            ResearchProjectPermission.MANAGE_MEMBERS,
        ),
        "can_attach_project_reference": bool(reference_kinds),
        "can_transition_project": allows(
            PROJECT_MANAGE_PERMISSION,
            ResearchProjectPermission.TRANSITION,
        ),
        "can_use_project_compute": allows(
            PROJECT_COMPUTE_PERMISSION,
            ResearchProjectPermission.USE_COMPUTE,
        ),
        "allowed_reference_kinds": reference_kinds,
    }


def _allowed_reference_kinds(
    request: HttpRequest,
    *,
    project: ResearchProject,
) -> tuple[str, ...]:
    if not request.user.has_perm(PROJECT_WRITE_PERMISSION):
        return ()
    actor_id, _roles, _permissions = actor_snapshot(request.user)
    return tuple(
        kind
        for kind in _REFERENCE_KINDS
        if has_research_project_permission(
            project,
            actor_id=actor_id,
            permission=project_permission_for_reference(
                ResearchProjectObjectKind(kind)
            ),
        )
    )


def _require_project_role(
    request: HttpRequest,
    *,
    project: dict[str, Any],
    permission: ResearchProjectPermission,
) -> None:
    actor_id, _roles, _permissions = actor_snapshot(request.user)
    if not has_research_project_permission(
        ResearchProject.from_dict(project),
        actor_id=actor_id,
        permission=permission,
    ):
        raise PermissionDenied("research_project_role_required")


def _handle_project_error(request: HttpRequest, exc: BaseException) -> HttpResponse:
    if isinstance(
        exc,
        (ApplicationAuthorizationError, ResearchProjectAuthorizationError),
    ):
        raise PermissionDenied("research_project_permission_required") from exc
    if isinstance(exc, ResearchProjectNotFoundError):
        raise Http404("research_project_not_found") from exc
    if isinstance(exc, ResearchProjectIntegrityError):
        return render(
            request,
            "portal/error.html",
            {
                "title": "프로젝트 authority를 사용할 수 없습니다",
                "message": (
                    "외부 registry 무결성과 문의 ID를 관리자에게 전달해 주세요."
                ),
            },
            status=503,
        )
    if isinstance(exc, ResearchProjectConflictError):
        return render(
            request,
            "portal/error.html",
            {
                "title": "프로젝트 버전이 변경되었습니다",
                "message": "최신 프로젝트를 다시 연 뒤 변경을 재검토해 주세요.",
            },
            status=409,
        )
    if isinstance(
        exc,
        (PydanticValidationError, ResearchProjectError, TypeError, ValueError),
    ):
        return render(
            request,
            "portal/error.html",
            {
                "title": "프로젝트 요청이 올바르지 않습니다",
                "message": "입력값과 현재 프로젝트 상태를 확인해 주세요.",
            },
            status=400,
        )
    return render(
        request,
        "portal/error.html",
        {
            "title": "프로젝트 authority를 사용할 수 없습니다",
            "message": "외부 registry 무결성과 문의 ID를 관리자에게 전달해 주세요.",
        },
        status=503,
    )


def _audit(
    request: HttpRequest,
    *,
    action: str,
    project: dict[str, Any],
    details: dict[str, Any] | None = None,
) -> None:
    actor_id, _roles, _permissions = actor_snapshot(request.user)
    safe_details = sanitize_audit_details(
        {
            "project_id": project["project_id"],
            "project_version": project["version"],
            "project_hash": project["content_hash"],
            **(details or {}),
        }
    )
    append_web_audit_event(
        action=action,
        actor_id=actor_id,
        object_type="research_project",
        object_id=str(project["project_id"]),
        correlation_id=str(getattr(request, "correlation_id", "")),
        details=safe_details if isinstance(safe_details, dict) else {},
    )


@login_required
@permission_required(PROJECT_VIEW_PERMISSION, raise_exception=True)
@require_GET
def project_list(request: HttpRequest) -> HttpResponse:
    try:
        result = _service().search(
            ResearchProjectSearchRequest.model_validate(
                {
                    "request_id": str(getattr(request, "correlation_id", "")),
                    "actor": _actor(request),
                    "query": str(request.GET.get("q") or "") or None,
                    "statuses": frozenset(request.GET.getlist("status")),
                    "asset_classes": frozenset(request.GET.getlist("asset_class")),
                    "markets": frozenset(request.GET.getlist("market")),
                    "include_archived": (request.GET.get("include_archived") == "1"),
                }
            )
        )
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    return render(
        request,
        "portal/project_list.html",
        {
            "active_nav": "projects",
            "projects": result.projects,
            "query": str(request.GET.get("q") or ""),
            "can_create_projects": request.user.has_perm(PROJECT_CREATE_PERMISSION),
        },
    )


@login_required
@permission_required(PROJECT_CREATE_PERMISSION, raise_exception=True)
@require_http_methods(["GET", "POST"])
def project_create(request: HttpRequest) -> HttpResponse:
    form = ProjectCreateForm(request.POST or None)
    if request.method == "GET" or not form.is_valid():
        return render(
            request,
            "portal/project_form.html",
            {"active_nav": "projects", "form": form, "action": "create"},
        )
    try:
        actor = _actor(request)
        result = _service().create(
            ResearchProjectCreateRequest.model_validate(
                {
                    **_request_metadata(
                        request,
                        reason=str(form.cleaned_data["reason"]),
                    ),
                    "project_id": form.cleaned_data["project_id"],
                    "title": form.cleaned_data["title"],
                    "research_question": form.cleaned_data["research_question"],
                    "owner_id": actor.actor_id,
                    "asset_classes": _csv(form.cleaned_data["asset_classes"]),
                    "markets": _csv(form.cleaned_data["markets"]),
                    "members": ({"actor_id": actor.actor_id, "role": "OWNER"},),
                }
            )
        )
        _audit(request, action="research_project_created", project=result.project)
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    messages.success(request, "연구 프로젝트를 생성했습니다.")
    return redirect("portal:project-detail", project_id=result.project["project_id"])


@login_required
@permission_required(PROJECT_VIEW_PERMISSION, raise_exception=True)
@require_GET
def project_detail(request: HttpRequest, project_id: str) -> HttpResponse:
    try:
        result = _service().get(
            ResearchProjectQueryRequest(
                request_id=str(getattr(request, "correlation_id", "")),
                actor=_actor(request),
                project_id=project_id,
            )
        )
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    return render(
        request,
        "portal/project_detail.html",
        _project_context(request, project=result.project),
    )


def _load_project(request: HttpRequest, project_id: str) -> dict[str, Any]:
    return (
        _service()
        .get(
            ResearchProjectQueryRequest(
                request_id=str(getattr(request, "correlation_id", "")),
                actor=_actor(request),
                project_id=project_id,
            )
        )
        .project
    )


def _render_action_form(
    request: HttpRequest,
    *,
    project: dict[str, Any],
    form: forms.Form,
    action: str,
) -> HttpResponse:
    return render(
        request,
        "portal/project_form.html",
        _project_context(
            request,
            project=project,
            form=form,
            action=action,
        ),
    )


@login_required
@permission_required(PROJECT_MANAGE_PERMISSION, raise_exception=True)
@require_http_methods(["GET", "POST"])
def project_revise(request: HttpRequest, project_id: str) -> HttpResponse:
    try:
        project = _load_project(request, project_id)
        _require_project_role(
            request,
            project=project,
            permission=ResearchProjectPermission.REVISE,
        )
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    initial = {
        "expected_version": project["version"],
        "title": project["title"],
        "research_question": project["research_question"],
        "asset_classes": ",".join(project["asset_classes"]),
        "markets": ",".join(project["markets"]),
    }
    form = ProjectRevisionForm(request.POST or None, initial=initial)
    if request.method == "GET" or not form.is_valid():
        return _render_action_form(
            request,
            project=project,
            form=form,
            action="revise",
        )
    try:
        result = _service().revise(
            ResearchProjectRevisionRequest.model_validate(
                {
                    **_request_metadata(
                        request,
                        reason=str(form.cleaned_data["reason"]),
                    ),
                    "project_id": project_id,
                    "expected_version": form.cleaned_data["expected_version"],
                    "title": form.cleaned_data["title"],
                    "research_question": form.cleaned_data["research_question"],
                    "asset_classes": _csv(form.cleaned_data["asset_classes"]),
                    "markets": _csv(form.cleaned_data["markets"]),
                }
            )
        )
        _audit(request, action="research_project_revised", project=result.project)
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    messages.success(request, "프로젝트 설명과 범위를 새 버전으로 기록했습니다.")
    return redirect("portal:project-detail", project_id=project_id)


@login_required
@permission_required(PROJECT_MANAGE_PERMISSION, raise_exception=True)
@require_http_methods(["GET", "POST"])
def project_members(request: HttpRequest, project_id: str) -> HttpResponse:
    try:
        project = _load_project(request, project_id)
        _require_project_role(
            request,
            project=project,
            permission=ResearchProjectPermission.MANAGE_MEMBERS,
        )
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    initial = {
        "expected_version": project["version"],
        "members": "\n".join(
            f"{member['actor_id']},{member['role']}" for member in project["members"]
        ),
    }
    form = ProjectMembersForm(request.POST or None, initial=initial)
    if request.method == "GET" or not form.is_valid():
        return _render_action_form(
            request,
            project=project,
            form=form,
            action="members",
        )
    try:
        result = _service().replace_members(
            ResearchProjectMembersRequest.model_validate(
                {
                    **_request_metadata(
                        request,
                        reason=str(form.cleaned_data["reason"]),
                    ),
                    "project_id": project_id,
                    "expected_version": form.cleaned_data["expected_version"],
                    "members": form.cleaned_data["members"],
                }
            )
        )
        _audit(
            request,
            action="research_project_members_replaced",
            project=result.project,
            details={"member_count": len(result.project["members"])},
        )
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    messages.success(request, "프로젝트 구성원을 새 버전으로 기록했습니다.")
    return redirect("portal:project-detail", project_id=project_id)


@login_required
@permission_required(PROJECT_WRITE_PERMISSION, raise_exception=True)
@require_http_methods(["GET", "POST"])
def project_reference(request: HttpRequest, project_id: str) -> HttpResponse:
    try:
        project = _load_project(request, project_id)
        allowed_reference_kinds = _allowed_reference_kinds(
            request,
            project=ResearchProject.from_dict(project),
        )
        if not allowed_reference_kinds:
            raise PermissionDenied("research_project_reference_role_required")
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    form = ProjectReferenceForm(
        request.POST or None,
        initial={"expected_version": project["version"]},
        allowed_kinds=allowed_reference_kinds,
    )
    if request.method == "GET" or not form.is_valid():
        return _render_action_form(
            request,
            project=project,
            form=form,
            action="reference",
        )
    try:
        reference = ResearchProjectObjectRefInput.model_validate(
            {
                "project_id": project_id,
                "kind": form.cleaned_data["kind"],
                "object_id": form.cleaned_data["object_id"],
                "version": form.cleaned_data["version"],
                "content_hash": form.cleaned_data["content_hash"],
            }
        )
        result = _service().attach_reference(
            ResearchProjectReferenceRequest.model_validate(
                {
                    **_request_metadata(
                        request,
                        reason=str(form.cleaned_data["reason"]),
                    ),
                    "project_id": project_id,
                    "expected_version": form.cleaned_data["expected_version"],
                    "reference": reference,
                }
            )
        )
        _audit(
            request,
            action="research_project_reference_attached",
            project=result.project,
            details={
                "reference_kind": reference.kind,
                "reference_id": reference.object_id,
                "reference_version": reference.version,
                "reference_hash": reference.content_hash,
            },
        )
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    messages.success(request, "해시 고정 typed reference를 연결했습니다.")
    return redirect("portal:project-detail", project_id=project_id)


@login_required
@permission_required(PROJECT_MANAGE_PERMISSION, raise_exception=True)
@require_http_methods(["GET", "POST"])
def project_transition(request: HttpRequest, project_id: str) -> HttpResponse:
    try:
        project = _load_project(request, project_id)
        _require_project_role(
            request,
            project=project,
            permission=ResearchProjectPermission.TRANSITION,
        )
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    form = ProjectTransitionForm(
        request.POST or None,
        initial={"expected_version": project["version"]},
    )
    if request.method == "GET" or not form.is_valid():
        return _render_action_form(
            request,
            project=project,
            form=form,
            action="transition",
        )
    try:
        result = _service().transition(
            ResearchProjectTransitionRequest.model_validate(
                {
                    **_request_metadata(
                        request,
                        reason=str(form.cleaned_data["reason"]),
                    ),
                    "project_id": project_id,
                    "expected_version": form.cleaned_data["expected_version"],
                    "to_status": form.cleaned_data["to_status"],
                    "superseded_by_project_id": (
                        form.cleaned_data["superseded_by_project_id"] or None
                    ),
                }
            )
        )
        _audit(
            request,
            action="research_project_transitioned",
            project=result.project,
            details={"status": result.project["status"]},
        )
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    messages.success(request, "프로젝트 lifecycle 상태를 변경했습니다.")
    return redirect("portal:project-detail", project_id=project_id)


@login_required
@permission_required(PROJECT_VIEW_PERMISSION, raise_exception=True)
@require_GET
def project_impact(request: HttpRequest) -> HttpResponse:
    supplied = any(
        request.GET.get(key) for key in ("kind", "object_id", "version", "content_hash")
    )
    projects: tuple[dict[str, Any], ...] = ()
    if supplied:
        try:
            result = _service().impacted_projects(
                ResearchProjectImpactQueryRequest.model_validate(
                    {
                        "request_id": str(getattr(request, "correlation_id", "")),
                        "actor": _actor(request),
                        "kind": request.GET.get("kind"),
                        "object_id": request.GET.get("object_id"),
                        "version": request.GET.get("version") or None,
                        "content_hash": request.GET.get("content_hash") or None,
                        "include_archived": request.GET.get("include_archived") != "0",
                    }
                )
            )
            projects = result.projects
        except (
            ApplicationAuthorizationError,
            OSError,
            PydanticValidationError,
            ResearchProjectError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            return _handle_project_error(request, exc)
    return render(
        request,
        "portal/project_list.html",
        {
            "active_nav": "projects",
            "projects": projects,
            "impact_mode": True,
            "reference_kinds": _REFERENCE_KINDS,
            "can_create_projects": request.user.has_perm(PROJECT_CREATE_PERMISSION),
        },
    )


@login_required
@permission_required(PROJECT_COMPUTE_PERMISSION, raise_exception=True)
@require_POST
def project_workspace(request: HttpRequest, project_id: str) -> HttpResponse:
    try:
        result = _service().workspace(
            ResearchProjectWorkspaceRequest(
                request_id=str(getattr(request, "correlation_id", "")),
                actor=_actor(request),
                project_id=project_id,
            )
        )
        project = _load_project(request, project_id)
        _audit(
            request,
            action="research_project_workspace_opened",
            project=project,
            details={
                "workspace_available": True,
                "project_version": result.project_version,
            },
        )
    except (
        ApplicationAuthorizationError,
        OSError,
        PydanticValidationError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        return _handle_project_error(request, exc)
    # Absolute compute/cache roots are an internal service result and are never
    # copied into browser-visible context, messages, or audit details.
    messages.success(request, "격리된 프로젝트 작업공간을 사용할 수 있습니다.")
    return redirect("portal:project-detail", project_id=project_id)


__all__ = [
    "PROJECT_COMPUTE_PERMISSION",
    "PROJECT_CREATE_PERMISSION",
    "PROJECT_MANAGE_PERMISSION",
    "PROJECT_VIEW_PERMISSION",
    "PROJECT_WRITE_PERMISSION",
    "project_create",
    "project_detail",
    "project_impact",
    "project_list",
    "project_members",
    "project_reference",
    "project_revise",
    "project_transition",
    "project_workspace",
]
