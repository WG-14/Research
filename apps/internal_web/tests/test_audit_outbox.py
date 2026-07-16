from __future__ import annotations

import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.query import QuerySet
from django.utils import timezone

import portal.audit as audit_module
from market_research.research.hash_chain import append_hash_chained_jsonl

from portal.audit import (
    AUDIT_PROJECTION_ALREADY_MARKED,
    AUDIT_PROJECTION_PROJECTED,
    append_web_audit_event,
    project_web_audit_event,
    record_web_audit_event,
    validate_web_audit_outbox,
)
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
def test_append_before_marker_gap_is_detected_and_reprojection_is_idempotent(
    monkeypatch,
    tmp_path,
    settings,
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    original_append = audit_module._append_payload

    def append_then_fail(payload):
        original_append(payload)
        raise OSError("simulated interruption after append")

    monkeypatch.setattr("portal.audit._append_payload", append_then_fail)
    with transaction.atomic():
        event = _record()

    validation = validate_web_audit_outbox()
    assert validation["status"] == "FAIL"
    assert validation["unmarked_projection_count"] == 1
    assert any(
        reason.startswith("audit_projection_unmarked:")
        for reason in validation["reasons"]
    )

    monkeypatch.setattr("portal.audit._append_payload", original_append)
    projection = project_web_audit_event(event.pk)
    event.refresh_from_db()
    assert projection.outcome == AUDIT_PROJECTION_PROJECTED
    assert event.projected_at is not None
    assert len(settings.INTERNAL_WEB_AUDIT_PATH.read_text().splitlines()) == 1
    assert validate_web_audit_outbox()["status"] == "PASS"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_projection_completion_with_same_row_is_successful(
    monkeypatch,
    tmp_path,
    settings,
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    original_append = audit_module._append_payload

    def fail_projection(_payload):
        raise OSError("leave event pending")

    monkeypatch.setattr("portal.audit._append_payload", fail_projection)
    with transaction.atomic():
        event = _record()
    monkeypatch.setattr("portal.audit._append_payload", original_append)

    original_update = QuerySet.update

    def complete_but_report_lost_race(queryset, **kwargs):
        updated = original_update(queryset, **kwargs)
        if queryset.model is WebAuditEvent and "projection_row_hash" in kwargs:
            assert updated == 1
            return 0
        return updated

    monkeypatch.setattr(QuerySet, "update", complete_but_report_lost_race)
    projection = project_web_audit_event(event.pk)

    event.refresh_from_db()
    assert projection.outcome == AUDIT_PROJECTION_ALREADY_MARKED
    assert event.projected_at is not None
    assert len(settings.INTERNAL_WEB_AUDIT_PATH.read_text().splitlines()) == 1


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_public_projection_primitive_revalidates_an_already_marked_event(
    tmp_path,
    settings,
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    with transaction.atomic():
        event = _record()

    repeated = project_web_audit_event(event.pk)
    assert repeated.outcome == AUDIT_PROJECTION_ALREADY_MARKED
    assert repeated.event_id == event.pk

    settings.INTERNAL_WEB_AUDIT_PATH.unlink()
    with pytest.raises(ValueError, match="projection_binding_invalid"):
        project_web_audit_event(event.pk)


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_marked_projection_uses_one_locked_hash_chain_snapshot(
    monkeypatch,
    tmp_path,
    settings,
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    with transaction.atomic():
        event = _record()
    event.refresh_from_db()

    monkeypatch.setattr(
        audit_module,
        "validate_web_audit",
        lambda: (_ for _ in ()).throw(AssertionError("unlocked validation used")),
    )
    monkeypatch.setattr(
        audit_module,
        "_read_audit_rows",
        lambda _path: (_ for _ in ()).throw(AssertionError("unlocked read used")),
    )

    repeated = project_web_audit_event(event.pk)
    assert repeated.outcome == AUDIT_PROJECTION_ALREADY_MARKED
    assert repeated.projection_row_hash == event.projection_row_hash


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_unterminated_audit_tail_fails_closed_without_projection_mutation(
    tmp_path,
    settings,
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    with transaction.atomic():
        event = _record()
    settings.INTERNAL_WEB_AUDIT_PATH.write_bytes(
        settings.INTERNAL_WEB_AUDIT_PATH.read_bytes().removesuffix(b"\n")
    )
    interrupted = settings.INTERNAL_WEB_AUDIT_PATH.read_bytes()

    validation = validate_web_audit_outbox()
    assert validation["status"] == "FAIL"
    assert "audit_stream_unterminated_final_line" in validation["reasons"]
    with pytest.raises(ValueError, match="web_audit_stream_invalid"):
        project_web_audit_event(event.pk)
    assert settings.INTERNAL_WEB_AUDIT_PATH.read_bytes() == interrupted


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_validator_detects_duplicate_projection(tmp_path, settings) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    with transaction.atomic():
        event = _record()

    append_hash_chained_jsonl(
        store=audit_module._AuditStore(),
        path=settings.INTERNAL_WEB_AUDIT_PATH,
        payload=dict(event.payload),
        label=audit_module.AUDIT_LABEL,
    )

    validation = validate_web_audit_outbox()
    assert validation["status"] == "FAIL"
    assert validation["duplicate_projection_count"] == 1
    assert any(
        reason.startswith("audit_projection_duplicate:")
        for reason in validation["reasons"]
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_validator_detects_orphan_outbox_projection(tmp_path, settings) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    payload = audit_module._build_audit_payload(
        action="orphan_projection",
        actor_id="actor-1",
        object_type="fixture",
        object_id="orphan-1",
        correlation_id=str(uuid.uuid4()),
        details={},
        delivery_mode=audit_module.AUDIT_DELIVERY_OUTBOX,
    )
    audit_module._append_payload(payload)

    validation = validate_web_audit_outbox()
    assert validation["status"] == "FAIL"
    assert validation["orphan_projection_count"] == 1
    assert any(
        reason.startswith("audit_projection_orphan:")
        for reason in validation["reasons"]
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_direct_audit_projection_is_not_an_outbox_orphan(tmp_path, settings) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    append_web_audit_event(
        action="audit_only",
        actor_id="actor-1",
        object_type="fixture",
        object_id="direct-1",
        correlation_id=str(uuid.uuid4()),
    )

    validation = validate_web_audit_outbox()
    assert validation["status"] == "PASS"
    assert validation["orphan_projection_count"] == 0


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_validator_recomputes_pending_intent_hash(
    monkeypatch,
    tmp_path,
    settings,
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    monkeypatch.setattr(
        "portal.audit._append_payload",
        lambda _payload: (_ for _ in ()).throw(OSError("projection unavailable")),
    )
    with transaction.atomic():
        event = _record()
    tampered = {**event.payload, "details": {"tampered": True}}
    WebAuditEvent.objects.filter(pk=event.pk).update(payload=tampered)

    validation = validate_web_audit_outbox()
    assert validation["status"] == "FAIL"
    assert any(
        reason.startswith("audit_intent_hash_mismatch:")
        for reason in validation["reasons"]
    )


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_malformed_audit_json_is_a_structured_failure(tmp_path, settings) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    settings.INTERNAL_WEB_AUDIT_PATH.write_text(
        '{"schema_version":2',
        encoding="utf-8",
    )

    validation = validate_web_audit_outbox()
    assert validation["status"] == "FAIL"
    assert validation["reasons"] == [
        "audit_stream_malformed_json:1",
        "audit_stream_unterminated_final_line",
    ]


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


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_production_segmented_audit_projects_and_reconciles_outbox(
    tmp_path,
    settings,
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "segmented-web-audit.jsonl"
    settings.INTERNAL_WEB_AUDIT_SEGMENT_ROWS = 2
    events = []
    for _index in range(5):
        with transaction.atomic():
            events.append(_record())

    validation = validate_web_audit_outbox()
    assert validation["status"] == "PASS"
    assert validation["row_count"] == 5
    assert validation["sealed_segment_count"] == 2
    assert validation["projected_event_count"] == 5
    repeated = project_web_audit_event(events[0].pk)
    assert repeated.outcome == AUDIT_PROJECTION_ALREADY_MARKED
    assert not settings.INTERNAL_WEB_AUDIT_PATH.exists()
    assert settings.INTERNAL_WEB_AUDIT_PATH.with_name(
        f"{settings.INTERNAL_WEB_AUDIT_PATH.name}.segments"
    ).is_dir()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_segmented_audit_full_reconciliation_detects_sealed_corruption(
    tmp_path,
    settings,
) -> None:
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "segmented-corrupt.jsonl"
    settings.INTERNAL_WEB_AUDIT_SEGMENT_ROWS = 2
    for _index in range(5):
        with transaction.atomic():
            _record()
    segment_root = settings.INTERNAL_WEB_AUDIT_PATH.with_name(
        f"{settings.INTERNAL_WEB_AUDIT_PATH.name}.segments"
    )
    first_segment = segment_root / "segments" / "segment-00000000.jsonl"
    rows = first_segment.read_text(encoding="utf-8").splitlines()
    first_segment.write_text(
        rows[0].replace("test_state_changed", "tampered_state")
        + "\n"
        + rows[1]
        + "\n",
        encoding="utf-8",
    )

    validation = validate_web_audit_outbox()
    assert validation["status"] == "FAIL"
    assert any("content_hash_mismatch:0" in reason for reason in validation["reasons"])
