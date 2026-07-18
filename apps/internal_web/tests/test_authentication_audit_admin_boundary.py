from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.urls import reverse

from portal.auth_audit import (
    AUTH_AUDIT_FAILURE_POLICY,
    AUTH_LOGIN_FAILED,
    AUTH_LOGIN_SUCCEEDED,
    AUTH_LOGOUT,
)
from portal.audit import AUDIT_DELIVERY_OUTBOX, validate_web_audit_outbox
from portal.models import ManifestUpload, ResearchJob, WebAuditEvent


pytestmark = pytest.mark.django_db(transaction=True, serialized_rollback=True)


def _audit_payload(action: str) -> dict[str, object]:
    return WebAuditEvent.objects.get(payload__action=action).payload


def _assert_pseudonymous_payload(
    payload: dict[str, object], *, username: str, address: str, credential: str
) -> None:
    serialized = json.dumps(payload, sort_keys=True)
    assert username not in serialized
    assert address not in serialized
    assert credential not in serialized
    assert payload["object_type"] == "authentication_subject"
    assert str(payload["actor_id"]).split(":", 1)[1].isalnum()
    assert str(payload["object_id"]).startswith("account-subject:")
    assert len(str(payload["action"])) <= 128
    assert len(str(payload["actor_id"])) <= 255
    assert len(str(payload["object_type"])) <= 128
    assert len(str(payload["object_id"])) <= 255
    assert len(str(payload["correlation_id"])) <= 128
    assert uuid.UUID(str(payload["correlation_id"]))
    assert payload["delivery_mode"] == AUDIT_DELIVERY_OUTBOX
    details = payload["details"]
    assert details["subject_scheme"] == "secret_hmac_sha256_v1"
    assert len(details["account_subject_hash"]) == 64
    assert len(details["network_subject_hash"]) == 64
    assert details["failure_policy"] == AUTH_AUDIT_FAILURE_POLICY


def test_login_success_failure_and_logout_commit_pseudonymous_outbox_events(
    client, runner_user, settings, tmp_path
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "authentication-audit.jsonl"
    login_url = reverse("portal:login")
    logout_url = reverse("portal:logout")
    address = "203.0.113.81"
    wrong_credential = "not-the-password"

    failed = client.post(
        login_url,
        {"username": runner_user.username, "password": wrong_credential},
        REMOTE_ADDR=address,
    )
    assert failed.status_code == 200
    assert "_auth_user_id" not in client.session
    failed_payload = _audit_payload(AUTH_LOGIN_FAILED)
    _assert_pseudonymous_payload(
        failed_payload,
        username=runner_user.username,
        address=address,
        credential=wrong_credential,
    )
    assert failed_payload["details"]["authenticated_subject"] is False

    succeeded = client.post(
        login_url,
        {"username": runner_user.username, "password": "test-password"},
        REMOTE_ADDR=address,
    )
    assert succeeded.status_code == 302
    assert str(client.session["_auth_user_id"]) == str(runner_user.pk)
    success_payload = _audit_payload(AUTH_LOGIN_SUCCEEDED)
    _assert_pseudonymous_payload(
        success_payload,
        username=runner_user.username,
        address=address,
        credential="test-password",
    )
    assert success_payload["details"]["authenticated_subject"] is True

    logged_out = client.post(logout_url, REMOTE_ADDR=address)
    assert logged_out.status_code == 302
    assert "_auth_user_id" not in client.session
    logout_payload = _audit_payload(AUTH_LOGOUT)
    _assert_pseudonymous_payload(
        logout_payload,
        username=runner_user.username,
        address=address,
        credential="test-password",
    )
    assert WebAuditEvent.objects.count() == 3


@pytest.mark.parametrize("credential", ("test-password", "wrong-password"))
def test_authentication_intent_insert_failure_never_establishes_session(
    credential, client, runner_user, settings, tmp_path, monkeypatch
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "authentication-audit.jsonl"
    monkeypatch.setattr(
        "portal.auth_audit.record_web_audit_event",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    response = client.post(
        reverse("portal:login"),
        {"username": runner_user.username, "password": credential},
        REMOTE_ADDR="203.0.113.82",
    )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert "_auth_user_id" not in client.session
    assert runner_user.username not in response.content.decode("utf-8")
    assert credential not in response.content.decode("utf-8")
    assert not WebAuditEvent.objects.exists()


def test_logout_terminates_session_when_audit_insert_fails(
    client, runner_user, settings, tmp_path, monkeypatch
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "authentication-audit.jsonl"
    client.force_login(runner_user)
    assert "_auth_user_id" in client.session
    initial_events = WebAuditEvent.objects.count()
    monkeypatch.setattr(
        "portal.auth_audit.record_web_audit_event",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    response = client.post(reverse("portal:logout"), REMOTE_ADDR="203.0.113.83")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert "_auth_user_id" not in client.session
    assert WebAuditEvent.objects.count() == initial_events


def test_projection_failure_keeps_pending_intent_and_does_not_erase_login(
    client, runner_user, settings, tmp_path, monkeypatch
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "authentication-audit.jsonl"
    monkeypatch.setattr(
        "portal.audit._append_payload",
        lambda _payload: (_ for _ in ()).throw(OSError("projection unavailable")),
    )

    response = client.post(
        reverse("portal:login"),
        {"username": runner_user.username, "password": "test-password"},
        REMOTE_ADDR="203.0.113.84",
    )

    assert response.status_code == 302
    assert "_auth_user_id" in client.session
    event = WebAuditEvent.objects.get(payload__action=AUTH_LOGIN_SUCCEEDED)
    assert event.projected_at is None
    assert event.projection_row_hash == ""
    readiness = validate_web_audit_outbox()
    assert readiness["status"] == "FAIL"
    assert readiness["pending_event_count"] == 1
    assert any(
        reason.startswith("audit_intent_pending:") for reason in readiness["reasons"]
    )


def test_django_admin_removes_auth_role_mutation_but_keeps_portal_models(
    client, settings, tmp_path
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "authentication-audit.jsonl"
    user_model = get_user_model()
    assert not admin.site.is_registered(user_model)
    assert not admin.site.is_registered(Group)
    assert not admin.site.is_registered(Permission)
    assert admin.site.is_registered(ManifestUpload)
    assert admin.site.is_registered(ResearchJob)

    superuser = user_model.objects.create_superuser(
        username="admin-boundary-fixture",
        password="test-password",
        email="admin@example.invalid",
    )
    target = user_model.objects.create_user(
        username="role-target-fixture",
        password="test-password",
    )
    approver = Group.objects.get(name="research_approver")
    client.force_login(superuser)

    response = client.post(
        f"/admin/auth/user/{target.pk}/change/",
        {
            "username": target.username,
            "groups": [str(approver.pk)],
            "is_active": "on",
        },
    )
    group_response = client.get(f"/admin/auth/group/{approver.pk}/change/")
    permission_response = client.get("/admin/auth/permission/")

    assert response.status_code == 404
    assert group_response.status_code == 404
    assert permission_response.status_code == 404
    assert not target.groups.filter(name="research_approver").exists()
    assert client.get(reverse("admin:index")).status_code == 200


def test_role_lifecycle_is_fixed_external_policy_in_settings_ui_and_docs(
    client, settings
) -> None:
    assert settings.INTERNAL_WEB_LOCAL_ROLE_MUTATION_SUPPORTED is False
    assert settings.INTERNAL_WEB_ROLE_LIFECYCLE_POLICY == (
        "external_identity_lifecycle_with_separate_approval_required"
    )
    login_page = client.get(reverse("portal:login")).content.decode("utf-8")
    assert "외부 identity lifecycle" in login_page
    assert "Django admin에서는 역할 부여를 지원하지 않습니다" in login_page
    docs = (
        Path(__file__).resolve().parents[3] / "docs/internal-web-architecture.md"
    ).read_text(encoding="utf-8")
    assert "Local role mutation is unsupported" in docs


def test_production_portal_has_no_direct_role_or_permission_mutation_path() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    forbidden = (
        re.compile(r"\.groups\.(?:add|set|remove|clear)\s*\("),
        re.compile(r"\.user_permissions\.(?:add|set|remove|clear)\s*\("),
        re.compile(r"\.permissions\.(?:add|set|remove|clear)\s*\("),
        re.compile(
            r"\b(?:Group|Permission)\.objects\."
            r"(?:create|get_or_create|update_or_create|update|delete)\s*\("
        ),
    )
    violations: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        for pattern in forbidden:
            if pattern.search(source):
                violations.append(f"{path.relative_to(source_root)}:{pattern.pattern}")
    assert violations == []
