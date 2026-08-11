from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse

from market_research.paths import ResearchPathManager
from market_research.research.research_project import (
    get_research_project,
    research_project_namespaces,
    research_project_registry_path,
)
from market_research.settings import ResearchSettings


pytestmark = pytest.mark.django_db


def _project_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ResearchPathManager:
    roots = tmp_path / "project-web-state"
    research_settings = ResearchSettings(
        data_root=roots / "data",
        artifact_root=roots / "artifacts",
        report_root=roots / "reports",
        cache_root=roots / "cache",
        db_path=None,
        max_workers=1,
        random_seed=0,
    )
    paths = ResearchPathManager.from_settings(
        research_settings,
        project_root=Path(__file__).resolve().parents[3],
    )
    monkeypatch.setattr(settings, "RESEARCH_PATHS", paths)
    monkeypatch.setattr(
        settings,
        "INTERNAL_WEB_AUDIT_PATH",
        paths.artifact_path("_internal_web", "audit", "web_audit.jsonl"),
    )
    return paths


def _group_user(group_name: str) -> Any:
    user = get_user_model().objects.create_user(
        username=f"project-{group_name}-{uuid.uuid4().hex}",
        password="test-password",
    )
    user.groups.add(Group.objects.get(name=group_name))
    return user


def test_authenticated_project_crud_workflow_binds_actor_and_hides_paths(
    client: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _project_paths(tmp_path, monkeypatch)
    owner = _group_user("research_runner")
    researcher = _group_user("research_runner")
    project_id = f"project-{uuid.uuid4().hex}"
    client.force_login(owner)

    created = client.post(
        reverse("portal:project-create"),
        {
            "project_id": project_id,
            "title": "Point-in-time liquidity study",
            "research_question": "Does the signal survive strict validation?",
            "asset_classes": "EQUITY,FUTURE",
            "markets": "KRX,NYSE",
            "reason": "Create an explicit governed research workspace",
        },
    )
    assert created.status_code == 302
    project = get_research_project(paths, project_id)
    assert project.owner_id == str(owner.pk)
    assert project.members[0].actor_id == str(owner.pk)
    assert project.version == 1

    revised = client.post(
        reverse("portal:project-revise", args=(project_id,)),
        {
            "expected_version": 1,
            "title": "Revised point-in-time liquidity study",
            "research_question": "Does the revised signal survive validation?",
            "asset_classes": "EQUITY",
            "markets": "KRX",
            "reason": "Narrow the preregistered scope",
        },
    )
    assert revised.status_code == 302

    members = client.post(
        reverse("portal:project-members", args=(project_id,)),
        {
            "expected_version": 2,
            "members": (f"{owner.pk},OWNER\n{researcher.pk},RESEARCHER"),
            "reason": "Assign a separate researcher",
        },
    )
    assert members.status_code == 302

    client.force_login(researcher)
    reference = client.post(
        reverse("portal:project-reference", args=(project_id,)),
        {
            "expected_version": 3,
            "kind": "RESULT",
            "object_id": "validated-result-1",
            "version": "1",
            "content_hash": f"sha256:{'a' * 64}",
            "reason": "Bind the validated result",
        },
    )
    assert reference.status_code == 302

    client.force_login(owner)
    transitioned = client.post(
        reverse("portal:project-transition", args=(project_id,)),
        {
            "expected_version": 4,
            "to_status": "ACTIVE",
            "superseded_by_project_id": "",
            "reason": "Begin governed research execution",
        },
    )
    assert transitioned.status_code == 302

    workspace = client.post(reverse("portal:project-workspace", args=(project_id,)))
    assert workspace.status_code == 302
    namespaces = research_project_namespaces(paths, project_id)
    assert namespaces.compute_root.is_dir()
    assert namespaces.cache_root.is_dir()

    list_response = client.get(
        reverse("portal:project-list"),
        {"q": "liquidity"},
    )
    detail_response = client.get(reverse("portal:project-detail", args=(project_id,)))
    impact_response = client.get(
        reverse("portal:project-impact"),
        {
            "kind": "RESULT",
            "object_id": "validated-result-1",
            "version": "1",
            "content_hash": f"sha256:{'a' * 64}",
        },
    )
    for response in (list_response, detail_response, impact_response):
        assert response.status_code == 200
        body = response.content.decode("utf-8")
        assert project_id in body
        assert str(paths.artifact_root) not in body
        assert str(paths.cache_root) not in body
        assert str(namespaces.compute_root) not in body
        assert str(namespaces.cache_root) not in body

    project = get_research_project(paths, project_id)
    assert project.status.value == "ACTIVE"
    assert project.version == 5
    assert len(project.object_refs) == 1
    assert research_project_registry_path(paths).is_file()

    audit_path = Path(settings.INTERNAL_WEB_AUDIT_PATH)
    audit_text = audit_path.read_text(encoding="utf-8")
    assert "research_project_workspace_opened" in audit_text
    assert str(paths.artifact_root) not in audit_text
    assert str(paths.cache_root) not in audit_text
    for line in audit_text.splitlines():
        payload = json.loads(line)
        assert "compute_root" not in payload["details"]
        assert "cache_root" not in payload["details"]


def test_project_routes_require_django_permission_and_project_membership(
    client: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _project_paths(tmp_path, monkeypatch)
    project_id = f"project-{uuid.uuid4().hex}"

    assert client.get(reverse("portal:project-list")).status_code == 302
    owner = _group_user("research_runner")
    client.force_login(owner)
    assert (
        client.post(
            reverse("portal:project-create"),
            {
                "project_id": project_id,
                "title": "Membership boundary",
                "research_question": "Can a non-member discover this project?",
                "asset_classes": "EQUITY",
                "markets": "KRX",
                "reason": "Test project-scoped authorization",
            },
        ).status_code
        == 302
    )
    assert get_research_project(paths, project_id).owner_id == str(owner.pk)

    outsider = get_user_model().objects.create_user(
        username=f"project-viewer-{uuid.uuid4().hex}",
        password="test-password",
    )
    outsider.groups.add(Group.objects.get(name="research_viewer"))
    client.force_login(outsider)

    listing = client.get(reverse("portal:project-list"))
    assert listing.status_code == 200
    assert project_id not in listing.content.decode("utf-8")
    assert (
        client.get(reverse("portal:project-detail", args=(project_id,))).status_code
        == 403
    )
    assert client.get(reverse("portal:project-create")).status_code == 403
    assert (
        client.post(reverse("portal:project-workspace", args=(project_id,))).status_code
        == 403
    )


def test_project_global_permissions_and_membership_roles_form_dual_gate(
    client: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _project_paths(tmp_path, monkeypatch)
    owner = _group_user("research_runner")
    researcher = _group_user("research_runner")
    reviewer = _group_user("research_reviewer")
    publisher = _group_user("research_approver")
    viewer = _group_user("research_viewer")
    project_id = f"project-{uuid.uuid4().hex}"

    expected_group_permissions = {
        "research_viewer": {"view_research_project"},
        "research_runner": {
            "create_research_project",
            "view_research_project",
            "manage_research_project",
            "write_research_project",
            "compute_research_project",
        },
        "research_reviewer": {
            "view_research_project",
            "manage_research_project",
            "write_research_project",
        },
        "research_approver": {
            "view_research_project",
            "write_research_project",
        },
    }
    for group_name, expected in expected_group_permissions.items():
        project_permissions = set(
            Group.objects.get(name=group_name)
            .permissions.filter(codename__endswith="research_project")
            .values_list("codename", flat=True)
        )
        assert project_permissions == expected

    client.force_login(reviewer)
    assert client.get(reverse("portal:project-create")).status_code == 403

    client.force_login(owner)
    created = client.post(
        reverse("portal:project-create"),
        {
            "project_id": project_id,
            "title": "Dual-gated project roles",
            "research_question": "Do global and project roles both bind?",
            "asset_classes": "EQUITY",
            "markets": "KRX",
            "reason": "Prove ordinary-role access without portal admin",
            # Browser-supplied authority fields are ignored. The adapter owns
            # the actor, event id, and timestamp.
            "actor_id": "spoofed-browser-actor",
            "event_id": "spoofed-browser-event",
            "recorded_at": "1900-01-01T00:00:00+00:00",
        },
    )
    assert created.status_code == 302
    first_event = json.loads(
        research_project_registry_path(paths)
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    assert first_event["actor_id"] == str(owner.pk)
    assert first_event["event_id"].startswith("web-")
    assert first_event["event_id"] != "spoofed-browser-event"
    assert first_event["recorded_at"] != "1900-01-01T00:00:00+00:00"

    members = client.post(
        reverse("portal:project-members", args=(project_id,)),
        {
            "expected_version": 1,
            "members": "\n".join(
                (
                    f"{owner.pk},OWNER",
                    f"{researcher.pk},RESEARCHER",
                    f"{reviewer.pk},REVIEWER",
                    f"{publisher.pk},PUBLISHER",
                    f"{viewer.pk},VIEWER",
                )
            ),
            "reason": "Assign separated project responsibilities",
        },
    )
    assert members.status_code == 302

    client.force_login(researcher)
    researcher_detail = client.get(reverse("portal:project-detail", args=(project_id,)))
    researcher_body = researcher_detail.content.decode("utf-8")
    assert researcher_detail.status_code == 200
    assert "객체 참조 연결" in researcher_body
    assert "격리 작업공간 준비" in researcher_body
    assert "구성원 관리" not in researcher_body
    assert "Lifecycle 변경" not in researcher_body
    assert (
        client.get(reverse("portal:project-revise", args=(project_id,))).status_code
        == 403
    )
    reference_form = client.get(
        reverse("portal:project-reference", args=(project_id,))
    ).content.decode("utf-8")
    assert all(
        f'value="{kind}"' in reference_form
        for kind in ("HYPOTHESIS", "CODE", "EXPERIMENT", "RESULT")
    )
    assert 'value="PACKAGE"' not in reference_form
    assert 'value="REVIEW"' not in reference_form
    forged = client.post(
        reverse("portal:project-reference", args=(project_id,)),
        {
            "expected_version": 2,
            "kind": "PACKAGE",
            "object_id": "forged-package",
            "version": "1",
            "content_hash": f"sha256:{'f' * 64}",
            "reason": "Must fail the project-role half of the gate",
        },
    )
    assert forged.status_code == 200
    assert get_research_project(paths, project_id).version == 2
    result_reference = client.post(
        reverse("portal:project-reference", args=(project_id,)),
        {
            "expected_version": 2,
            "kind": "RESULT",
            "object_id": "validated-result-role-gate",
            "version": "1",
            "content_hash": f"sha256:{'a' * 64}",
            "reason": "Researcher binds validated result",
        },
    )
    assert result_reference.status_code == 302
    assert (
        client.post(reverse("portal:project-workspace", args=(project_id,))).status_code
        == 302
    )

    client.force_login(reviewer)
    reviewer_detail = client.get(
        reverse("portal:project-detail", args=(project_id,))
    ).content.decode("utf-8")
    assert "객체 참조 연결" in reviewer_detail
    assert "Lifecycle 변경" in reviewer_detail
    assert "격리 작업공간 준비" not in reviewer_detail
    reviewer_form = client.get(
        reverse("portal:project-reference", args=(project_id,))
    ).content.decode("utf-8")
    assert 'value="REVIEW"' in reviewer_form
    assert 'value="RESULT"' not in reviewer_form
    review_reference = client.post(
        reverse("portal:project-reference", args=(project_id,)),
        {
            "expected_version": 3,
            "kind": "REVIEW",
            "object_id": "independent-review-role-gate",
            "version": "1",
            "content_hash": f"sha256:{'b' * 64}",
            "reason": "Reviewer binds review evidence",
        },
    )
    assert review_reference.status_code == 302
    transition = client.post(
        reverse("portal:project-transition", args=(project_id,)),
        {
            "expected_version": 4,
            "to_status": "ACTIVE",
            "superseded_by_project_id": "",
            "reason": "Reviewer activates the governed project",
        },
    )
    assert transition.status_code == 302
    assert (
        client.post(reverse("portal:project-workspace", args=(project_id,))).status_code
        == 403
    )

    client.force_login(publisher)
    publisher_detail = client.get(
        reverse("portal:project-detail", args=(project_id,))
    ).content.decode("utf-8")
    assert "객체 참조 연결" in publisher_detail
    assert "Lifecycle 변경" not in publisher_detail
    publisher_form = client.get(
        reverse("portal:project-reference", args=(project_id,))
    ).content.decode("utf-8")
    assert 'value="PACKAGE"' in publisher_form
    assert 'value="REVIEW"' not in publisher_form
    package_reference = client.post(
        reverse("portal:project-reference", args=(project_id,)),
        {
            "expected_version": 5,
            "kind": "PACKAGE",
            "object_id": "official-package-role-gate",
            "version": "1",
            "content_hash": f"sha256:{'c' * 64}",
            "reason": "Publisher binds immutable package reference",
        },
    )
    assert package_reference.status_code == 302
    assert (
        client.get(reverse("portal:project-transition", args=(project_id,))).status_code
        == 403
    )

    typed_reference_detail = client.get(
        reverse("portal:project-detail", args=(project_id,))
    ).content.decode("utf-8")
    assert "artifact resolver 결과가 아니라" in typed_reference_detail
    assert "official-package-role-gate" in typed_reference_detail
    assert f"sha256:{'c' * 64}" in typed_reference_detail
    assert 'href="official-package-role-gate"' not in typed_reference_detail

    client.force_login(viewer)
    viewer_detail = client.get(
        reverse("portal:project-detail", args=(project_id,))
    ).content.decode("utf-8")
    assert "권한 있는 변경" not in viewer_detail
    assert client.get(reverse("portal:project-create")).status_code == 403
    assert (
        client.get(reverse("portal:project-reference", args=(project_id,))).status_code
        == 403
    )
