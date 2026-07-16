from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone as django_timezone

from market_research.research.hash_chain import (
    append_hash_chained_jsonl,
    validate_hash_chained_jsonl,
)
from market_research.research.hashing import content_hash_payload, sha256_prefixed
from market_research.storage_io import append_jsonl

from .models import WebAuditEvent
from .security import sanitize_audit_details


AUDIT_LABEL = "internal_web_audit"


@dataclass(frozen=True, slots=True)
class _AuditStore:
    def append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        append_jsonl(path, payload)


def audit_path() -> Path:
    return Path(settings.INTERNAL_WEB_AUDIT_PATH)


def append_web_audit_event(
    *,
    action: str,
    actor_id: str,
    object_type: str,
    object_id: str,
    correlation_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append an audit-only event that has no related ORM state transition."""

    payload = _build_audit_payload(
        action=action,
        actor_id=actor_id,
        object_type=object_type,
        object_id=object_id,
        correlation_id=correlation_id,
        details=details,
    )
    return _append_payload(payload)


def record_web_audit_event(
    *,
    action: str,
    actor_id: str,
    object_type: str,
    object_id: str,
    correlation_id: str,
    details: dict[str, Any] | None = None,
) -> WebAuditEvent:
    """Persist an immutable outbox intent in the caller's DB transaction.

    The JSONL projection runs only after that transaction commits. A projection
    failure leaves a detectable pending intent; no automatic repair or retry is
    performed in this repository.
    """

    if not connection.in_atomic_block:
        raise RuntimeError("web_audit_state_event_requires_atomic_transaction")
    payload = _build_audit_payload(
        action=action,
        actor_id=actor_id,
        object_type=object_type,
        object_id=object_id,
        correlation_id=correlation_id,
        details=details,
    )
    event = WebAuditEvent.objects.create(
        id=uuid.UUID(str(payload["event_id"])),
        payload=payload,
        payload_hash=str(payload["intent_hash"]),
    )
    transaction.on_commit(lambda event_id=event.pk: _project_event(event_id))
    return event


def _build_audit_payload(
    *,
    action: str,
    actor_id: str,
    object_type: str,
    object_id: str,
    correlation_id: str,
    details: dict[str, Any] | None,
) -> dict[str, Any]:
    material = {
        "schema_version": 1,
        "event_id": str(uuid.uuid4()),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "action": str(action).strip()[:128],
        "actor_id": str(actor_id).strip()[:255],
        "object_type": str(object_type).strip()[:128],
        "object_id": str(object_id).strip()[:255],
        "correlation_id": str(correlation_id).strip()[:128],
        "details": sanitize_audit_details(details or {}),
    }
    if not all(
        material[key]
        for key in ("action", "actor_id", "object_type", "object_id", "correlation_id")
    ):
        raise ValueError("web_audit_identity_fields_required")
    return {
        **material,
        "intent_hash": sha256_prefixed(
            content_hash_payload(material),
            label="internal_web_audit_intent",
        ),
    }


def _append_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return append_hash_chained_jsonl(
        store=_AuditStore(),
        path=audit_path(),
        payload=payload,
        label=AUDIT_LABEL,
    )


def _project_event(event_id: uuid.UUID) -> None:
    event = WebAuditEvent.objects.get(pk=event_id)
    if event.projected_at is not None:
        return
    row = _append_payload(dict(event.payload))
    updated = WebAuditEvent.objects.filter(
        pk=event.pk,
        projected_at__isnull=True,
        projection_row_hash="",
    ).update(
        projection_row_hash=str(row["row_hash"]),
        projected_at=django_timezone.now(),
    )
    if updated != 1:
        raise RuntimeError("web_audit_projection_state_conflict")


def validate_web_audit() -> dict[str, Any]:
    return validate_hash_chained_jsonl(path=audit_path(), label=AUDIT_LABEL)


def validate_web_audit_outbox() -> dict[str, Any]:
    """Compare durable state intents with their append-only JSONL projections."""

    chain = validate_web_audit()
    rows_by_event_id: dict[str, dict[str, Any]] = {}
    path = audit_path()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict) and row.get("event_id"):
                rows_by_event_id[str(row["event_id"])] = row
    reasons = list(chain["reasons"])
    projected_count = 0
    pending_count = 0
    for event in WebAuditEvent.objects.all().iterator():
        row = rows_by_event_id.get(str(event.pk))
        if event.projected_at is None:
            pending_count += 1
            reasons.append(f"audit_intent_pending:{event.pk}")
            continue
        projected_count += 1
        if row is None:
            reasons.append(f"audit_projection_missing:{event.pk}")
        elif row.get("intent_hash") != event.payload_hash:
            reasons.append(f"audit_intent_hash_mismatch:{event.pk}")
        elif row.get("row_hash") != event.projection_row_hash:
            reasons.append(f"audit_projection_hash_mismatch:{event.pk}")
    return {
        **chain,
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "outbox_event_count": projected_count + pending_count,
        "projected_event_count": projected_count,
        "pending_event_count": pending_count,
    }
