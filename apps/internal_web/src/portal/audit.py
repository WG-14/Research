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

from market_research.application.adapter_contracts import (
    append_hash_chained_jsonl_idempotent,
    append_segmented_hash_chained_jsonl_idempotent,
    content_hash_payload,
    read_segmented_hash_chain_full_snapshot,
    sha256_prefixed,
    validate_hash_chained_jsonl,
    validate_segmented_hash_chain_incremental,
    verify_hash_chained_jsonl_event,
    verify_segmented_hash_chained_jsonl_event,
)
from market_research.storage_io import append_jsonl

from .models import WebAuditEvent
from .security import sanitize_audit_details


AUDIT_LABEL = "internal_web_audit"
AUDIT_SCHEMA_VERSION = 2
AUDIT_DELIVERY_DIRECT = "direct"
AUDIT_DELIVERY_OUTBOX = "transactional_outbox"
AUDIT_PROJECTION_PROJECTED = "PROJECTED"
AUDIT_PROJECTION_ALREADY_MARKED = "ALREADY_MARKED"
_CHAIN_FIELDS = frozenset({"sequence", "prior_hash", "row_hash"})
_IDENTITY_FIELDS = (
    "action",
    "actor_id",
    "object_type",
    "object_id",
    "correlation_id",
)


@dataclass(frozen=True, slots=True)
class _AuditStore:
    def append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        append_jsonl(path, payload)


@dataclass(frozen=True, slots=True)
class AuditProjectionResult:
    """Bounded result from one event projection attempt.

    This is a single-event processing primitive, not a pending-event scanner,
    retry loop, lease, or repair workflow.  The caller owns scheduling.
    """

    event_id: uuid.UUID
    projection_row_hash: str
    outcome: str


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
        delivery_mode=AUDIT_DELIVERY_DIRECT,
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
        delivery_mode=AUDIT_DELIVERY_OUTBOX,
    )
    event = WebAuditEvent.objects.create(
        id=uuid.UUID(str(payload["event_id"])),
        payload=payload,
        payload_hash=str(payload["intent_hash"]),
    )
    transaction.on_commit(
        lambda event_id=event.pk: project_web_audit_event(event_id),
        robust=True,
    )
    return event


def _build_audit_payload(
    *,
    action: str,
    actor_id: str,
    object_type: str,
    object_id: str,
    correlation_id: str,
    details: dict[str, Any] | None,
    delivery_mode: str,
) -> dict[str, Any]:
    if delivery_mode not in {AUDIT_DELIVERY_DIRECT, AUDIT_DELIVERY_OUTBOX}:
        raise ValueError("web_audit_delivery_mode_invalid")
    material = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "delivery_mode": delivery_mode,
        "event_id": str(uuid.uuid4()),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "action": str(action).strip()[:128],
        "actor_id": str(actor_id).strip()[:255],
        "object_type": str(object_type).strip()[:128],
        "object_id": str(object_id).strip()[:255],
        "correlation_id": str(correlation_id).strip()[:128],
        "details": sanitize_audit_details(details or {}),
    }
    if not all(material[key] for key in _IDENTITY_FIELDS):
        raise ValueError("web_audit_identity_fields_required")
    return {
        **material,
        "intent_hash": sha256_prefixed(
            content_hash_payload(material),
            label="internal_web_audit_intent",
        ),
    }


def _append_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if segment_rows := _audit_segment_rows():
        return append_segmented_hash_chained_jsonl_idempotent(
            path=audit_path(),
            payload=payload,
            label=AUDIT_LABEL,
            max_segment_rows=segment_rows,
        )
    return append_hash_chained_jsonl_idempotent(
        store=_AuditStore(),
        path=audit_path(),
        payload=payload,
        label=AUDIT_LABEL,
    )


def project_web_audit_event(
    event_id: uuid.UUID | str,
) -> AuditProjectionResult:
    """Validate and project exactly one immutable outbox event.

    Duplicate execution converges through the event-ID hash-chain primitive.
    No payload or path override is accepted, and an already marked event is
    revalidated against the JSONL stream before success is reported.
    """

    try:
        normalized_event_id = uuid.UUID(str(event_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("web_audit_event_id_invalid") from exc
    try:
        event = WebAuditEvent.objects.get(pk=normalized_event_id)
    except WebAuditEvent.DoesNotExist as exc:
        raise ValueError("web_audit_event_not_found") from exc
    if _event_intent_reasons(event):
        raise ValueError("web_audit_intent_binding_invalid")
    if event.projected_at is not None:
        row = _verified_marked_projection(event)
        return AuditProjectionResult(
            event_id=event.pk,
            projection_row_hash=str(row["row_hash"]),
            outcome=AUDIT_PROJECTION_ALREADY_MARKED,
        )
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
        current = WebAuditEvent.objects.get(pk=event.pk)
        if current.projected_at is not None and current.projection_row_hash == str(
            row["row_hash"]
        ):
            return AuditProjectionResult(
                event_id=current.pk,
                projection_row_hash=current.projection_row_hash,
                outcome=AUDIT_PROJECTION_ALREADY_MARKED,
            )
        raise RuntimeError("web_audit_projection_state_conflict")
    return AuditProjectionResult(
        event_id=event.pk,
        projection_row_hash=str(row["row_hash"]),
        outcome=AUDIT_PROJECTION_PROJECTED,
    )


def _verified_marked_projection(event: WebAuditEvent) -> dict[str, Any]:
    try:
        if segment_rows := _audit_segment_rows():
            row = verify_segmented_hash_chained_jsonl_event(
                path=audit_path(),
                label=AUDIT_LABEL,
                max_segment_rows=segment_rows,
                event_id=str(event.pk),
                expected_payload=dict(event.payload),
            )
        else:
            row = verify_hash_chained_jsonl_event(
                path=audit_path(),
                label=AUDIT_LABEL,
                event_id=str(event.pk),
                expected_payload=dict(event.payload),
            )
    except (OSError, UnicodeError, TypeError, ValueError) as exc:
        if str(exc) in {
            "hash_chain_duplicate_event_id",
            "hash_chain_event_id_conflict",
            "hash_chain_event_id_missing",
            "segmented_hash_chain_duplicate_event_id",
            "segmented_hash_chain_event_id_conflict",
            "segmented_hash_chain_event_id_missing",
        }:
            raise ValueError("web_audit_projection_binding_invalid") from exc
        raise ValueError("web_audit_stream_invalid") from exc
    if row.get("row_hash") != event.projection_row_hash:
        raise ValueError("web_audit_projection_binding_invalid")
    return row


def validate_web_audit() -> dict[str, Any]:
    if segment_rows := _audit_segment_rows():
        return validate_segmented_hash_chain_incremental(
            path=audit_path(),
            label=AUDIT_LABEL,
            max_segment_rows=segment_rows,
        )
    return validate_hash_chained_jsonl(path=audit_path(), label=AUDIT_LABEL)


def validate_web_audit_outbox() -> dict[str, Any]:
    """Compare durable state intents with their append-only JSONL projections."""

    path = audit_path()
    segment_rows = _audit_segment_rows()
    if segment_rows:
        full_snapshot = read_segmented_hash_chain_full_snapshot(
            path=path,
            label=AUDIT_LABEL,
            max_segment_rows=segment_rows,
        )
        rows = [dict(row) for row in full_snapshot.rows]
        chain = full_snapshot.as_validation()
        read_reasons = list(full_snapshot.reasons)
        rows_complete = full_snapshot.status == "PASS"
    else:
        rows, read_reasons = _read_audit_rows(path)
        rows_complete = not read_reasons
        chain = {}
    if rows_complete and not segment_rows:
        try:
            chain = validate_web_audit()
        except (OSError, UnicodeError, ValueError, TypeError):
            chain = {
                "status": "FAIL",
                "reasons": ["audit_stream_validation_error"],
                "row_count": len(rows),
                "stream_hash": None,
            }
            rows_complete = False
        else:
            expected_stream_hash = rows[-1].get("row_hash") if rows else None
            if (
                chain.get("row_count") != len(rows)
                or chain.get("stream_hash") != expected_stream_hash
            ):
                chain = {
                    **chain,
                    "status": "FAIL",
                    "reasons": [
                        *chain.get("reasons", []),
                        "audit_stream_changed_during_validation",
                    ],
                }
                rows_complete = False
    elif not rows_complete and not chain:
        chain = {
            "status": "FAIL",
            "reasons": read_reasons,
            "row_count": len(rows),
            "stream_hash": None,
        }

    events = list(WebAuditEvent.objects.all().iterator())
    event_ids = {str(event.pk) for event in events}
    rows_by_event_id: dict[str, list[dict[str, Any]]] = {}
    reasons = list(chain["reasons"])
    for index, row in enumerate(rows, start=1):
        raw_event_id = row.get("event_id")
        try:
            normalized_event_id = str(uuid.UUID(str(raw_event_id)))
        except (ValueError, TypeError, AttributeError):
            reasons.append(f"audit_projection_event_id_invalid:{index}")
            continue
        if raw_event_id != normalized_event_id:
            reasons.append(f"audit_projection_event_id_invalid:{index}")
            continue
        rows_by_event_id.setdefault(normalized_event_id, []).append(row)
        row_payload = _projection_payload(row)
        for reason in _payload_binding_reasons(row_payload):
            reasons.append(f"audit_projection_{reason}:{normalized_event_id}")

    duplicate_ids = {
        event_id
        for event_id, matching_rows in rows_by_event_id.items()
        if len(matching_rows) > 1
    }
    for event_id in duplicate_ids:
        reasons.append(f"audit_projection_duplicate:{event_id}")

    orphan_ids: set[str] = set()
    if rows_complete:
        for event_id, matching_rows in rows_by_event_id.items():
            if event_id in event_ids:
                continue
            if any(
                _projection_payload(row).get("delivery_mode") == AUDIT_DELIVERY_OUTBOX
                for row in matching_rows
            ):
                orphan_ids.add(event_id)
                reasons.append(f"audit_projection_orphan:{event_id}")

    projected_count = 0
    pending_count = 0
    unmarked_ids: set[str] = set()
    for event in events:
        event_id = str(event.pk)
        matching_rows = rows_by_event_id.get(event_id, [])
        for reason in _event_intent_reasons(event):
            reasons.append(f"audit_intent_{reason}:{event_id}")
        if event.projected_at is None:
            pending_count += 1
            reasons.append(f"audit_intent_pending:{event_id}")
            if rows_complete and matching_rows:
                unmarked_ids.add(event_id)
                reasons.append(f"audit_projection_unmarked:{event_id}")
        else:
            projected_count += 1

        if not rows_complete:
            continue
        if not matching_rows:
            if event.projected_at is not None:
                reasons.append(f"audit_projection_missing:{event_id}")
            continue
        if len(matching_rows) != 1:
            continue

        row = matching_rows[0]
        row_payload = _projection_payload(row)
        if row_payload != event.payload:
            reasons.append(f"audit_projection_payload_mismatch:{event_id}")
        if row.get("intent_hash") != event.payload_hash:
            reasons.append(f"audit_intent_hash_mismatch:{event_id}")
        if (
            event.projected_at is not None
            and row.get("row_hash") != event.projection_row_hash
        ):
            reasons.append(f"audit_projection_hash_mismatch:{event_id}")
    return {
        **chain,
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "outbox_event_count": len(events),
        "projected_event_count": projected_count,
        "pending_event_count": pending_count,
        "duplicate_projection_count": len(duplicate_ids),
        "orphan_projection_count": len(orphan_ids),
        "unmarked_projection_count": len(unmarked_ids),
    }


def _projection_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in _CHAIN_FIELDS}


def _payload_binding_reasons(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["payload_invalid"]
    reasons: list[str] = []
    schema_version = payload.get("schema_version")
    if schema_version not in {1, AUDIT_SCHEMA_VERSION}:
        reasons.append("schema_version_invalid")
    if schema_version == AUDIT_SCHEMA_VERSION and payload.get("delivery_mode") not in {
        AUDIT_DELIVERY_DIRECT,
        AUDIT_DELIVERY_OUTBOX,
    }:
        reasons.append("delivery_mode_invalid")
    for field in _IDENTITY_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            reasons.append(f"identity_{field}_invalid")
    try:
        normalized_event_id = str(uuid.UUID(str(payload.get("event_id"))))
    except (ValueError, TypeError, AttributeError):
        reasons.append("event_id_invalid")
    else:
        if payload.get("event_id") != normalized_event_id:
            reasons.append("event_id_invalid")
    intent_hash = payload.get("intent_hash")
    try:
        expected_intent_hash = _intent_hash(payload)
    except (TypeError, ValueError):
        reasons.append("payload_invalid")
    else:
        if intent_hash != expected_intent_hash:
            reasons.append("hash_mismatch")
    return reasons


def _event_intent_reasons(event: WebAuditEvent) -> list[str]:
    payload = event.payload
    reasons = _payload_binding_reasons(payload)
    if not isinstance(payload, dict):
        return reasons
    if payload.get("event_id") != str(event.pk):
        reasons.append("event_id_mismatch")
    if payload.get("intent_hash") != event.payload_hash:
        reasons.append("record_hash_mismatch")
    if (
        payload.get("schema_version") == AUDIT_SCHEMA_VERSION
        and payload.get("delivery_mode") != AUDIT_DELIVERY_OUTBOX
    ):
        reasons.append("delivery_mode_mismatch")
    return reasons


def _intent_hash(payload: dict[str, Any]) -> str:
    material = {key: value for key, value in payload.items() if key != "intent_hash"}
    return sha256_prefixed(
        content_hash_payload(material),
        label="internal_web_audit_intent",
    )


def _read_audit_rows(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        return [], []
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return [], ["audit_stream_unreadable"]
    rows: list[dict[str, Any]] = []
    reasons: list[str] = []
    if content and not content.endswith("\n"):
        reasons.append("audit_stream_unterminated_final_line")
    lines = content.splitlines()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError, TypeError):
            reasons.append(f"audit_stream_malformed_json:{line_number}")
            continue
        if not isinstance(value, dict):
            reasons.append(f"audit_stream_non_object:{line_number}")
            continue
        rows.append(value)
    return rows, reasons


def _audit_segment_rows() -> int:
    value = int(getattr(settings, "INTERNAL_WEB_AUDIT_SEGMENT_ROWS", 0))
    if value == 0:
        return 0
    if not 2 <= value <= 1_000_000:
        raise RuntimeError("internal_web_audit_segment_rows_invalid")
    return value
