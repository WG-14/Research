from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from portal.login_throttle import (
    login_is_blocked,
    login_subject_hashes,
    record_login_failure,
)
from portal.models import LoginThrottle


pytestmark = pytest.mark.django_db


def _configure_low_limit(settings) -> None:
    settings.INTERNAL_WEB_LOGIN_FAILURE_LIMIT = 2
    settings.INTERNAL_WEB_LOGIN_FAILURE_WINDOW_SECONDS = 300
    settings.INTERNAL_WEB_LOGIN_BLOCK_SECONDS = 300


def test_login_failures_store_only_hmac_subjects_and_block_valid_credentials(
    client,
    runner_user,
    settings,
) -> None:
    _configure_low_limit(settings)
    remote_addr = "203.0.113.27"
    login_url = reverse("portal:login")

    for _ in range(2):
        response = client.post(
            login_url,
            {"username": runner_user.username, "password": "wrong-password"},
            REMOTE_ADDR=remote_addr,
        )
        assert response.status_code == 200
        assert "아이디 또는 비밀번호를 확인해 주세요." in response.content.decode(
            "utf-8"
        )

    rows = list(LoginThrottle.objects.order_by("subject_hash"))
    assert len(rows) == 2
    assert all(len(row.subject_hash) == 64 for row in rows)
    assert all(set(row.subject_hash) <= set("0123456789abcdef") for row in rows)
    assert all(row.failure_count == 2 for row in rows)
    assert all(row.blocked_until is not None for row in rows)
    serialized = " ".join(row.subject_hash for row in rows)
    assert runner_user.username not in serialized
    assert remote_addr not in serialized
    assert {field.name for field in LoginThrottle._meta.fields}.isdisjoint(
        {"username", "ip_address", "remote_address"}
    )

    blocked = client.post(
        login_url,
        {"username": runner_user.username, "password": "test-password"},
        REMOTE_ADDR=remote_addr,
    )
    assert blocked.status_code == 200
    assert "아이디 또는 비밀번호를 확인해 주세요." in blocked.content.decode("utf-8")
    assert "_auth_user_id" not in client.session


def test_successful_login_resets_only_account_failure_row(
    client,
    runner_user,
    settings,
) -> None:
    _configure_low_limit(settings)
    remote_addr = "203.0.113.28"
    login_url = reverse("portal:login")

    failed = client.post(
        login_url,
        {"username": runner_user.username, "password": "wrong-password"},
        REMOTE_ADDR=remote_addr,
    )
    assert failed.status_code == 200
    assert LoginThrottle.objects.count() == 2

    succeeded = client.post(
        login_url,
        {"username": runner_user.username, "password": "test-password"},
        REMOTE_ADDR=remote_addr,
    )

    assert succeeded.status_code == 302
    assert succeeded.url == reverse("portal:dashboard")
    assert str(client.session["_auth_user_id"]) == str(runner_user.pk)
    assert LoginThrottle.objects.count() == 1
    remaining = LoginThrottle.objects.get()
    assert remaining.subject_hash == login_subject_hashes(
        succeeded.wsgi_request,
        runner_user.username,
    )[1]


def test_login_throttle_window_resets_and_block_expires(rf, settings) -> None:
    _configure_low_limit(settings)
    request = rf.post("/login/", REMOTE_ADDR="2001:db8::1")
    subjects = login_subject_hashes(request, "ExampleUser")
    started_at = timezone.now()

    record_login_failure(subjects, observed_at=started_at)
    record_login_failure(subjects, observed_at=started_at + timedelta(seconds=301))
    assert set(LoginThrottle.objects.values_list("failure_count", flat=True)) == {1}

    record_login_failure(subjects, observed_at=started_at + timedelta(seconds=302))
    assert login_is_blocked(
        subjects,
        observed_at=started_at + timedelta(seconds=302),
    )
    assert not login_is_blocked(
        subjects,
        observed_at=started_at + timedelta(seconds=603),
    )


def test_subject_hashes_are_secret_key_dependent(rf, settings) -> None:
    request = rf.post("/login/", REMOTE_ADDR="203.0.113.29")
    first = login_subject_hashes(request, "ExampleUser")
    settings.SECRET_KEY = "different-test-key-with-sufficient-length"
    second = login_subject_hashes(request, "ExampleUser")

    assert first != second
