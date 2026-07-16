from __future__ import annotations

import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from portal.audit import record_web_audit_event, validate_web_audit_outbox
from portal.models import LoginThrottle, WebAuditEvent


def _record() -> WebAuditEvent:
    return record_web_audit_event(
        action="test_state_changed",
        actor_id="actor-1",
        object_type="fixture",
        object_id="object-1",
        correlation_id=str(uuid.uuid4()),
        details={"result_hash": "sha256:" + "a" * 64},
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_committed_audit_intent_is_projected_and_cross_checked(
    tmp_path,
    settings,
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"

    with transaction.atomic():
        event = _record()
        assert event.projected_at is None

    event.refresh_from_db()
    assert event.projected_at is not None
    assert event.projection_row_hash.startswith("sha256:")
    validation = validate_web_audit_outbox()
    assert validation["status"] == "PASS"
    assert validation["projected_event_count"] == 1
    assert validation["pending_event_count"] == 0


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_state_and_audit_intent_roll_back_together(tmp_path, settings) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"

    with pytest.raises(RuntimeError, match="rollback"):
        with transaction.atomic():
            LoginThrottle.objects.create(
                subject_hash="b" * 64,
                failure_count=1,
                window_started_at=timezone.now(),
            )
            _record()
            raise RuntimeError("rollback")

    assert not LoginThrottle.objects.filter(subject_hash="b" * 64).exists()
    assert WebAuditEvent.objects.count() == 0
    assert not settings.INTERNAL_WEB_AUDIT_PATH.exists()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_projection_failure_leaves_detectable_committed_intent(
    monkeypatch,
    tmp_path,
    settings,
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"

    def fail_projection(_payload):
        raise OSError("simulated audit projection outage")

    monkeypatch.setattr("portal.audit._append_payload", fail_projection)
    with pytest.raises(OSError, match="projection outage"):
        with transaction.atomic():
            _record()

    event = WebAuditEvent.objects.get()
    assert event.projected_at is None
    assert event.projection_row_hash == ""
    validation = validate_web_audit_outbox()
    assert validation["status"] == "FAIL"
    assert validation["pending_event_count"] == 1
    assert any(reason.startswith("audit_intent_pending:") for reason in validation["reasons"])


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_audit_intent_payload_is_immutable(tmp_path, settings) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    with transaction.atomic():
        event = _record()
    event.refresh_from_db()
    event.payload = {"tampered": True}
    with pytest.raises(ValidationError, match="web_audit_event_is_immutable"):
        event.save()
    with pytest.raises(ValidationError, match="web_audit_event_is_immutable"):
        event.delete()
