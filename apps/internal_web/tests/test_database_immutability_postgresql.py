from __future__ import annotations

import uuid

import pytest
from django.db import DatabaseError, connection, transaction

from portal.audit import record_web_audit_event


pytestmark = pytest.mark.postgresql


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_postgresql_rejects_raw_audit_intent_mutation_below_the_orm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    settings,
) -> None:
    """Exercise the installed triggers only on an externally supplied PostgreSQL DB."""

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL trigger integration requires PostgreSQL")
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "web-audit.jsonl"
    monkeypatch.setattr(
        "portal.audit._append_payload",
        lambda _payload: (_ for _ in ()).throw(OSError("leave event pending")),
    )
    with transaction.atomic():
        event = record_web_audit_event(
            action="test_state_changed",
            actor_id="actor-1",
            object_type="fixture",
            object_id="object-1",
            correlation_id=str(uuid.uuid4()),
            details={"result_hash": "sha256:" + "a" * 64},
        )

    with pytest.raises(DatabaseError, match="intent_mutation_rejected"):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE portal_webauditevent
                    SET payload = %s
                    WHERE id = %s
                    """,
                    ['{"tampered":true}', event.pk],
                )

    with pytest.raises(DatabaseError, match="immutable_row_mutation_rejected"):
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("TRUNCATE TABLE portal_webauditevent")
