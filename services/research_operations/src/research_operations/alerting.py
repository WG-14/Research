"""Durable service-health alert delivery, acknowledgement, and escalation.

This module is deliberately limited to the offline platform's operational
health conditions.  It contains no market, strategy, account, order, or fill
vocabulary and cannot be used as a trading-monitoring adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import IO, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from psycopg import Connection

from .database import connection
from .errors import (
    AlertBindingConflict,
    AlertDeliveryClaimLost,
    AlertStateConflict,
    AlertTransportError,
)

OPEN = "OPEN"
ACKNOWLEDGED = "ACKNOWLEDGED"
RESOLVED = "RESOLVED"
PENDING = "PENDING"
CLAIMED = "CLAIMED"
DELIVERED = "DELIVERED"
FAILED = "FAILED"

ALLOWED_SERVICE_CONDITIONS = frozenset(
    {
        "audit_validation_failed",
        "backup_failed",
        "backup_stale",
        "certificate_expiry",
        "database_not_primary",
        "database_unavailable",
        "dead_letter_present",
        "job_receipt_unapplied",
        "migration_drift",
        "outbox_lag",
        "outbox_worker_missing",
        "preflight_failed",
        "quarantine",
        "readiness_failed",
        "research_worker_missing",
        "restore_drill_stale",
        "restore_rehearsal_failed",
        "worker_process_failed",
    }
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HTTP_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ServiceAlert:
    alert_id: uuid.UUID
    idempotency_key: str
    binding_hash: str
    condition_code: str
    severity: str
    source_actor_id: str
    status: str
    opened_at: datetime
    acknowledgment_deadline_at: datetime
    acknowledged_by: str
    acknowledgment_reason: str
    acknowledged_at: datetime | None
    resolved_by: str
    resolution_reason: str
    resolved_at: datetime | None
    escalation_level: int
    last_event_hash: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AlertDeliveryClaim:
    delivery_id: uuid.UUID
    alert_id: uuid.UUID
    delivery_key: str
    endpoint_id: str
    escalation_level: int
    worker_id: str
    lease_token: uuid.UUID
    fencing_token: int
    lease_expires_at: datetime
    attempt_count: int
    condition_code: str
    severity: str
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class AlertEvent:
    event_id: uuid.UUID
    alert_id: uuid.UUID
    sequence: int
    event_type: str
    actor_id: str
    reason_code: str
    occurred_at: datetime
    details_hash: str
    prior_event_hash: str
    event_hash: str


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        del req, fp, code, msg, headers, newurl
        return None


class LoopbackOrHttpsAlertTransport:
    """POST a bounded, secret-free alert envelope without following redirects."""

    def __init__(self, endpoint_url: str, *, timeout_seconds: float = 5.0) -> None:
        self.endpoint_url = _validated_endpoint_url(endpoint_url)
        if not 0.1 <= timeout_seconds <= 30:
            raise ValueError("alert_transport_timeout_invalid")
        self.timeout_seconds = timeout_seconds

    def send(self, claim: AlertDeliveryClaim) -> int:
        payload = json.dumps(
            {
                "alert_id": str(claim.alert_id),
                "condition_code": claim.condition_code,
                "delivery_id": str(claim.delivery_id),
                "escalation_level": claim.escalation_level,
                "idempotency_key": claim.delivery_key,
                "opened_at": _iso_utc(claim.opened_at),
                "schema_version": 1,
                "severity": claim.severity,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        request = Request(
            self.endpoint_url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": claim.delivery_key,
                "User-Agent": "research-operations-alert/1",
            },
        )
        try:
            opener = build_opener(_RejectRedirects())
            with opener.open(request, timeout=self.timeout_seconds) as response:
                response_code = int(response.status)
                response.read(4096)
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise AlertTransportError("alert_delivery_http_error") from exc
        if not 200 <= response_code <= 299:
            raise AlertTransportError("alert_delivery_http_status_invalid")
        return response_code


class ServiceAlertStore:
    """PostgreSQL projection plus append-only, hash-chained alert evidence."""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn

    def raise_alert(
        self,
        *,
        idempotency_key: str,
        condition_code: str,
        severity: str,
        source_actor_id: str,
        endpoint_id: str,
        acknowledgment_timeout_seconds: int,
        now: datetime | None = None,
    ) -> ServiceAlert:
        key = _identifier(idempotency_key, "alert_idempotency_key", maximum=255)
        condition = _condition(condition_code)
        normalized_severity = _severity(severity)
        source_actor = _identifier(source_actor_id, "source_actor_id", maximum=255)
        endpoint = _identifier(endpoint_id, "endpoint_id", maximum=128)
        _bounded_int(
            acknowledgment_timeout_seconds,
            "acknowledgment_timeout_seconds",
            minimum=1,
            maximum=86_400,
        )
        observed_at = _aware_utc(now or utcnow())
        binding = {
            "acknowledgment_timeout_seconds": acknowledgment_timeout_seconds,
            "condition_code": condition,
            "endpoint_id": endpoint,
            "idempotency_key": key,
            "severity": normalized_severity,
            "source_actor_id": source_actor,
        }
        binding_hash = _digest(binding)
        with connection(self._dsn) as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 761144))",
                (key,),
            )
            existing = conn.execute(
                _ALERT_SELECT + " WHERE idempotency_key = %s FOR UPDATE",
                (key,),
            ).fetchone()
            if existing is not None:
                alert = _alert(existing)
                if alert.binding_hash != binding_hash:
                    raise AlertBindingConflict("service_alert_binding_conflict")
                return alert

            alert_id = uuid.uuid4()
            event_id = uuid.uuid4()
            details_hash = _digest(
                {
                    "acknowledgment_timeout_seconds": (acknowledgment_timeout_seconds),
                    "endpoint_id": endpoint,
                }
            )
            opened_event_hash = _event_hash(
                event_id=event_id,
                alert_id=alert_id,
                sequence=1,
                event_type="OPENED",
                actor_id=source_actor,
                reason_code=condition,
                occurred_at=observed_at,
                details_hash=details_hash,
                prior_event_hash="",
            )
            deadline = observed_at + timedelta(seconds=acknowledgment_timeout_seconds)
            conn.execute(
                """
                INSERT INTO research_ops.service_alert (
                    alert_id, idempotency_key, binding_hash, condition_code,
                    severity, source_actor_id, status, opened_at,
                    acknowledgment_deadline_at, escalation_level,
                    last_event_hash, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, 0, %s, %s
                )
                """,
                (
                    alert_id,
                    key,
                    binding_hash,
                    condition,
                    normalized_severity,
                    source_actor,
                    observed_at,
                    deadline,
                    opened_event_hash,
                    observed_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO research_ops.service_alert_event (
                    event_id, alert_id, sequence, event_type, actor_id,
                    reason_code, occurred_at, details_hash, prior_event_hash,
                    event_hash
                ) VALUES (%s, %s, 1, 'OPENED', %s, %s, %s, %s, '', %s)
                """,
                (
                    event_id,
                    alert_id,
                    source_actor,
                    condition,
                    observed_at,
                    details_hash,
                    opened_event_hash,
                ),
            )
            _insert_delivery(
                conn,
                alert_id=alert_id,
                endpoint_id=endpoint,
                escalation_level=0,
                now=observed_at,
            )
            row = conn.execute(
                _ALERT_SELECT + " WHERE alert_id = %s",
                (alert_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("service_alert_insert_not_visible")
        return _alert(row)

    def claim_delivery(
        self,
        *,
        worker_id: str,
        endpoint_id: str | None = None,
        lease_seconds: int = 30,
        max_attempts: int = 8,
        now: datetime | None = None,
    ) -> AlertDeliveryClaim | None:
        worker = _identifier(worker_id, "alert_worker_id", maximum=255)
        endpoint = (
            None
            if endpoint_id is None
            else _identifier(endpoint_id, "endpoint_id", maximum=128)
        )
        _bounded_int(lease_seconds, "alert_lease_seconds", minimum=3, maximum=3600)
        _bounded_int(max_attempts, "alert_max_attempts", minimum=1, maximum=100)
        observed_at = _aware_utc(now or utcnow())
        lease_token = uuid.uuid4()
        with connection(self._dsn) as conn:
            exhausted = conn.execute(
                """
                SELECT delivery_id, alert_id, endpoint_id, escalation_level
                FROM research_ops.service_alert_delivery
                WHERE status = 'CLAIMED' AND lease_expires_at <= %s
                  AND attempt_count >= %s
                  AND (CAST(%s AS varchar) IS NULL OR endpoint_id = %s)
                ORDER BY lease_expires_at, delivery_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (observed_at, max_attempts, endpoint, endpoint),
            ).fetchone()
            if exhausted is not None:
                conn.execute(
                    """
                    UPDATE research_ops.service_alert_delivery
                    SET status = 'FAILED', claimed_by = '', lease_token = NULL,
                        lease_expires_at = NULL,
                        last_error_code = 'delivery_lease_retry_exhausted',
                        updated_at = %s
                    WHERE delivery_id = %s
                    """,
                    (observed_at, exhausted[0]),
                )
                _append_event(
                    conn,
                    alert_id=exhausted[1],
                    event_type="DELIVERY_FAILED",
                    actor_id=worker,
                    reason_code="delivery_lease_retry_exhausted",
                    occurred_at=observed_at,
                    details={
                        "delivery_id": str(exhausted[0]),
                        "endpoint_id": str(exhausted[2]),
                        "escalation_level": int(exhausted[3]),
                        "terminal": True,
                    },
                )
            row = conn.execute(
                """
                WITH candidate AS (
                    SELECT delivery_id
                    FROM research_ops.service_alert_delivery
                    WHERE (
                        (status = 'PENDING' AND available_at <= %(now)s)
                        OR
                        (status = 'CLAIMED' AND lease_expires_at <= %(now)s)
                    )
                    AND attempt_count < %(max_attempts)s
                    AND (
                        CAST(%(endpoint)s AS varchar) IS NULL
                        OR endpoint_id = %(endpoint)s
                    )
                    ORDER BY available_at, created_at, delivery_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE research_ops.service_alert_delivery AS delivery
                SET status = 'CLAIMED', claimed_by = %(worker)s,
                    lease_token = %(lease_token)s,
                    fencing_token = delivery.fencing_token + 1,
                    lease_expires_at = %(expires_at)s,
                    attempt_count = delivery.attempt_count + 1,
                    last_error_code = '', updated_at = %(now)s
                FROM candidate, research_ops.service_alert AS alert
                WHERE delivery.delivery_id = candidate.delivery_id
                  AND alert.alert_id = delivery.alert_id
                RETURNING delivery.delivery_id, delivery.alert_id,
                          delivery.delivery_key, delivery.endpoint_id,
                          delivery.escalation_level, delivery.claimed_by,
                          delivery.lease_token, delivery.fencing_token,
                          delivery.lease_expires_at, delivery.attempt_count,
                          alert.condition_code, alert.severity, alert.opened_at
                """,
                {
                    "now": observed_at,
                    "max_attempts": max_attempts,
                    "endpoint": endpoint,
                    "worker": worker,
                    "lease_token": lease_token,
                    "expires_at": observed_at + timedelta(seconds=lease_seconds),
                },
            ).fetchone()
            if row is not None:
                _append_event(
                    conn,
                    alert_id=row[1],
                    event_type="DELIVERY_CLAIMED",
                    actor_id=worker,
                    reason_code="delivery_claimed",
                    occurred_at=observed_at,
                    details={
                        "attempt_count": int(row[9]),
                        "delivery_id": str(row[0]),
                        "endpoint_id": str(row[3]),
                        "escalation_level": int(row[4]),
                        "fencing_token": int(row[7]),
                    },
                )
        return None if row is None else _claim(row)

    def mark_delivered(
        self,
        claim: AlertDeliveryClaim,
        *,
        response_code: int,
        now: datetime | None = None,
    ) -> None:
        if not 200 <= response_code <= 299:
            raise ValueError("alert_response_code_invalid")
        observed_at = _aware_utc(now or utcnow())
        with connection(self._dsn) as conn:
            row = conn.execute(
                """
                UPDATE research_ops.service_alert_delivery
                SET status = 'DELIVERED', claimed_by = '', lease_token = NULL,
                    lease_expires_at = NULL, response_code = %s,
                    delivered_at = %s, last_error_code = '', updated_at = %s
                WHERE delivery_id = %s AND alert_id = %s
                  AND status = 'CLAIMED' AND claimed_by = %s
                  AND lease_token = %s AND fencing_token = %s
                  AND lease_expires_at > %s
                RETURNING delivery_id
                """,
                (
                    response_code,
                    observed_at,
                    observed_at,
                    claim.delivery_id,
                    claim.alert_id,
                    claim.worker_id,
                    claim.lease_token,
                    claim.fencing_token,
                    observed_at,
                ),
            ).fetchone()
            if row is None:
                raise AlertDeliveryClaimLost("service_alert_delivery_claim_lost")
            _append_event(
                conn,
                alert_id=claim.alert_id,
                event_type="DELIVERED",
                actor_id=claim.worker_id,
                reason_code="delivery_accepted",
                occurred_at=observed_at,
                details={
                    "delivery_id": str(claim.delivery_id),
                    "endpoint_id": claim.endpoint_id,
                    "escalation_level": claim.escalation_level,
                    "response_code": response_code,
                },
            )

    def record_delivery_failure(
        self,
        claim: AlertDeliveryClaim,
        *,
        reason_code: str,
        max_attempts: int,
        retry_delay_seconds: int,
        now: datetime | None = None,
    ) -> str:
        reason = _reason(reason_code)
        _bounded_int(max_attempts, "alert_max_attempts", minimum=1, maximum=100)
        _bounded_int(
            retry_delay_seconds,
            "alert_retry_delay_seconds",
            minimum=0,
            maximum=3600,
        )
        observed_at = _aware_utc(now or utcnow())
        terminal = claim.attempt_count >= max_attempts
        status = FAILED if terminal else PENDING
        with connection(self._dsn) as conn:
            row = conn.execute(
                """
                UPDATE research_ops.service_alert_delivery
                SET status = %(status)s, available_at = %(available_at)s,
                    claimed_by = '', lease_token = NULL,
                    lease_expires_at = NULL, response_code = NULL,
                    last_error_code = %(reason)s, updated_at = %(now)s
                WHERE delivery_id = %(delivery_id)s AND alert_id = %(alert_id)s
                  AND status = 'CLAIMED' AND claimed_by = %(worker)s
                  AND lease_token = %(lease_token)s
                  AND fencing_token = %(fencing_token)s
                  AND lease_expires_at > %(now)s
                RETURNING delivery_id
                """,
                {
                    "status": status,
                    "available_at": observed_at
                    + timedelta(seconds=retry_delay_seconds),
                    "reason": reason,
                    "now": observed_at,
                    "delivery_id": claim.delivery_id,
                    "alert_id": claim.alert_id,
                    "worker": claim.worker_id,
                    "lease_token": claim.lease_token,
                    "fencing_token": claim.fencing_token,
                },
            ).fetchone()
            if row is None:
                raise AlertDeliveryClaimLost("service_alert_delivery_claim_lost")
            _append_event(
                conn,
                alert_id=claim.alert_id,
                event_type="DELIVERY_FAILED",
                actor_id=claim.worker_id,
                reason_code=reason,
                occurred_at=observed_at,
                details={
                    "attempt_count": claim.attempt_count,
                    "delivery_id": str(claim.delivery_id),
                    "endpoint_id": claim.endpoint_id,
                    "escalation_level": claim.escalation_level,
                    "terminal": terminal,
                },
            )
        return status

    def acknowledge(
        self,
        *,
        alert_id: uuid.UUID | str,
        actor_id: str,
        reason_code: str,
        now: datetime | None = None,
    ) -> ServiceAlert:
        normalized_id = _uuid(alert_id, "alert_id")
        actor = _identifier(actor_id, "acknowledgment_actor_id", maximum=255)
        reason = _reason(reason_code)
        observed_at = _aware_utc(now or utcnow())
        with connection(self._dsn) as conn:
            row = conn.execute(
                _ALERT_SELECT + " WHERE alert_id = %s FOR UPDATE",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise AlertStateConflict("service_alert_not_found")
            current = _alert(row)
            if current.source_actor_id == actor:
                raise AlertStateConflict("service_alert_actor_separation_required")
            if current.status in {ACKNOWLEDGED, RESOLVED}:
                if (
                    current.acknowledged_by == actor
                    and current.acknowledgment_reason == reason
                ):
                    return current
                raise AlertStateConflict("service_alert_acknowledgment_conflict")
            conn.execute(
                """
                UPDATE research_ops.service_alert
                SET status = 'ACKNOWLEDGED', acknowledged_by = %s,
                    acknowledgment_reason = %s, acknowledged_at = %s,
                    updated_at = %s
                WHERE alert_id = %s AND status = 'OPEN'
                """,
                (actor, reason, observed_at, observed_at, normalized_id),
            )
            _append_event(
                conn,
                alert_id=normalized_id,
                event_type="ACKNOWLEDGED",
                actor_id=actor,
                reason_code=reason,
                occurred_at=observed_at,
                details={"escalation_level": current.escalation_level},
            )
            updated = conn.execute(
                _ALERT_SELECT + " WHERE alert_id = %s",
                (normalized_id,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("service_alert_acknowledgment_not_visible")
        return _alert(updated)

    def escalate_due(
        self,
        *,
        actor_id: str,
        endpoint_id: str,
        repeat_after_seconds: int,
        maximum_level: int = 3,
        now: datetime | None = None,
    ) -> ServiceAlert | None:
        actor = _identifier(actor_id, "escalation_actor_id", maximum=255)
        endpoint = _identifier(endpoint_id, "endpoint_id", maximum=128)
        _bounded_int(
            repeat_after_seconds,
            "alert_repeat_after_seconds",
            minimum=1,
            maximum=86_400,
        )
        _bounded_int(maximum_level, "alert_maximum_level", minimum=1, maximum=32)
        observed_at = _aware_utc(now or utcnow())
        with connection(self._dsn) as conn:
            row = conn.execute(
                _ALERT_SELECT
                + """
                  WHERE status = 'OPEN'
                    AND acknowledgment_deadline_at <= %s
                    AND escalation_level < %s
                  ORDER BY acknowledgment_deadline_at, opened_at, alert_id
                  FOR UPDATE SKIP LOCKED
                  LIMIT 1
                """,
                (observed_at, maximum_level),
            ).fetchone()
            if row is None:
                return None
            current = _alert(row)
            next_level = current.escalation_level + 1
            conn.execute(
                """
                UPDATE research_ops.service_alert
                SET escalation_level = %s, acknowledgment_deadline_at = %s,
                    updated_at = %s
                WHERE alert_id = %s AND status = 'OPEN'
                """,
                (
                    next_level,
                    observed_at + timedelta(seconds=repeat_after_seconds),
                    observed_at,
                    current.alert_id,
                ),
            )
            _insert_delivery(
                conn,
                alert_id=current.alert_id,
                endpoint_id=endpoint,
                escalation_level=next_level,
                now=observed_at,
            )
            _append_event(
                conn,
                alert_id=current.alert_id,
                event_type="ESCALATED",
                actor_id=actor,
                reason_code="acknowledgment_deadline_exceeded",
                occurred_at=observed_at,
                details={
                    "endpoint_id": endpoint,
                    "escalation_level": next_level,
                },
            )
            updated = conn.execute(
                _ALERT_SELECT + " WHERE alert_id = %s",
                (current.alert_id,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("service_alert_escalation_not_visible")
        return _alert(updated)

    def resolve(
        self,
        *,
        alert_id: uuid.UUID | str,
        actor_id: str,
        reason_code: str,
        now: datetime | None = None,
    ) -> ServiceAlert:
        normalized_id = _uuid(alert_id, "alert_id")
        actor = _identifier(actor_id, "resolution_actor_id", maximum=255)
        reason = _reason(reason_code)
        observed_at = _aware_utc(now or utcnow())
        with connection(self._dsn) as conn:
            row = conn.execute(
                _ALERT_SELECT + " WHERE alert_id = %s FOR UPDATE",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise AlertStateConflict("service_alert_not_found")
            current = _alert(row)
            if current.status == RESOLVED:
                if current.resolved_by == actor and current.resolution_reason == reason:
                    return current
                raise AlertStateConflict("service_alert_resolution_conflict")
            if current.status != ACKNOWLEDGED:
                raise AlertStateConflict("service_alert_must_be_acknowledged")
            if current.source_actor_id == actor:
                raise AlertStateConflict("service_alert_actor_separation_required")
            conn.execute(
                """
                UPDATE research_ops.service_alert
                SET status = 'RESOLVED', resolved_by = %s,
                    resolution_reason = %s, resolved_at = %s, updated_at = %s
                WHERE alert_id = %s AND status = 'ACKNOWLEDGED'
                """,
                (actor, reason, observed_at, observed_at, normalized_id),
            )
            _append_event(
                conn,
                alert_id=normalized_id,
                event_type="RESOLVED",
                actor_id=actor,
                reason_code=reason,
                occurred_at=observed_at,
                details={"escalation_level": current.escalation_level},
            )
            updated = conn.execute(
                _ALERT_SELECT + " WHERE alert_id = %s",
                (normalized_id,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("service_alert_resolution_not_visible")
        return _alert(updated)

    def events(self, alert_id: uuid.UUID | str) -> tuple[AlertEvent, ...]:
        normalized_id = _uuid(alert_id, "alert_id")
        with connection(self._dsn, session_read_only=True) as conn:
            rows = conn.execute(
                """
                SELECT event_id, alert_id, sequence, event_type, actor_id,
                       reason_code, occurred_at, details_hash,
                       prior_event_hash, event_hash
                FROM research_ops.service_alert_event
                WHERE alert_id = %s
                ORDER BY sequence
                """,
                (normalized_id,),
            ).fetchall()
        return tuple(_event(row) for row in rows)

    def verify_event_chain(self, alert_id: uuid.UUID | str) -> str:
        normalized_id = _uuid(alert_id, "alert_id")
        events = self.events(normalized_id)
        if not events:
            raise AlertStateConflict("service_alert_evidence_missing")
        previous = ""
        for expected_sequence, event in enumerate(events, start=1):
            if (
                event.sequence != expected_sequence
                or event.prior_event_hash != previous
            ):
                raise AlertStateConflict("service_alert_evidence_chain_invalid")
            expected_hash = _event_hash(
                event_id=event.event_id,
                alert_id=event.alert_id,
                sequence=event.sequence,
                event_type=event.event_type,
                actor_id=event.actor_id,
                reason_code=event.reason_code,
                occurred_at=event.occurred_at,
                details_hash=event.details_hash,
                prior_event_hash=event.prior_event_hash,
            )
            if event.event_hash != expected_hash:
                raise AlertStateConflict("service_alert_evidence_hash_invalid")
            previous = event.event_hash
        with connection(self._dsn, session_read_only=True) as conn:
            row = conn.execute(
                "SELECT last_event_hash FROM research_ops.service_alert "
                "WHERE alert_id = %s",
                (normalized_id,),
            ).fetchone()
        if row is None or row[0] != previous:
            raise AlertStateConflict("service_alert_projection_hash_mismatch")
        return previous


_ALERT_SELECT = """
SELECT alert_id, idempotency_key, binding_hash, condition_code, severity,
       source_actor_id, status, opened_at, acknowledgment_deadline_at,
       acknowledged_by, acknowledgment_reason, acknowledged_at,
       resolved_by, resolution_reason, resolved_at, escalation_level,
       last_event_hash, updated_at
FROM research_ops.service_alert
"""


def _insert_delivery(
    conn: Connection[Any],
    *,
    alert_id: uuid.UUID,
    endpoint_id: str,
    escalation_level: int,
    now: datetime,
) -> uuid.UUID:
    delivery_id = uuid.uuid4()
    delivery_key = f"service-alert:{alert_id}:level:{escalation_level}:{endpoint_id}"
    conn.execute(
        """
        INSERT INTO research_ops.service_alert_delivery (
            delivery_id, alert_id, delivery_key, endpoint_id,
            escalation_level, status, available_at, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s, %s)
        """,
        (
            delivery_id,
            alert_id,
            delivery_key,
            endpoint_id,
            escalation_level,
            now,
            now,
            now,
        ),
    )
    return delivery_id


def _append_event(
    conn: Connection[Any],
    *,
    alert_id: uuid.UUID,
    event_type: str,
    actor_id: str,
    reason_code: str,
    occurred_at: datetime,
    details: dict[str, object],
) -> str:
    alert_row = conn.execute(
        "SELECT last_event_hash FROM research_ops.service_alert "
        "WHERE alert_id = %s FOR UPDATE",
        (alert_id,),
    ).fetchone()
    previous_row = conn.execute(
        """
        SELECT sequence, event_hash
        FROM research_ops.service_alert_event
        WHERE alert_id = %s
        ORDER BY sequence DESC
        LIMIT 1
        """,
        (alert_id,),
    ).fetchone()
    if alert_row is None or previous_row is None or alert_row[0] != previous_row[1]:
        raise AlertStateConflict("service_alert_evidence_projection_mismatch")
    sequence = int(previous_row[0]) + 1
    prior_event_hash = str(previous_row[1])
    event_id = uuid.uuid4()
    details_hash = _digest(details)
    event_hash = _event_hash(
        event_id=event_id,
        alert_id=alert_id,
        sequence=sequence,
        event_type=event_type,
        actor_id=actor_id,
        reason_code=reason_code,
        occurred_at=occurred_at,
        details_hash=details_hash,
        prior_event_hash=prior_event_hash,
    )
    conn.execute(
        """
        INSERT INTO research_ops.service_alert_event (
            event_id, alert_id, sequence, event_type, actor_id,
            reason_code, occurred_at, details_hash, prior_event_hash, event_hash
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event_id,
            alert_id,
            sequence,
            event_type,
            actor_id,
            reason_code,
            occurred_at,
            details_hash,
            prior_event_hash,
            event_hash,
        ),
    )
    conn.execute(
        """
        UPDATE research_ops.service_alert
        SET last_event_hash = %s, updated_at = greatest(updated_at, %s)
        WHERE alert_id = %s
        """,
        (event_hash, occurred_at, alert_id),
    )
    return event_hash


def _event_hash(
    *,
    event_id: uuid.UUID,
    alert_id: uuid.UUID,
    sequence: int,
    event_type: str,
    actor_id: str,
    reason_code: str,
    occurred_at: datetime,
    details_hash: str,
    prior_event_hash: str,
) -> str:
    return _digest(
        {
            "actor_id": actor_id,
            "alert_id": str(alert_id),
            "details_hash": details_hash,
            "event_id": str(event_id),
            "event_type": event_type,
            "occurred_at": _iso_utc(occurred_at),
            "prior_event_hash": prior_event_hash,
            "reason_code": reason_code,
            "sequence": sequence,
        }
    )


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _iso_utc(value: datetime) -> str:
    return _aware_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("service_alert_timestamp_must_be_timezone_aware")
    return value.astimezone(UTC)


def _identifier(value: str, name: str, *, maximum: int) -> str:
    if value != value.strip() or not 1 <= len(value) <= maximum:
        raise ValueError(f"{name}_invalid")
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{name}_invalid")
    return value


def _reason(value: str) -> str:
    if _REASON_RE.fullmatch(value) is None:
        raise ValueError("service_alert_reason_code_invalid")
    return value


def _condition(value: str) -> str:
    if value not in ALLOWED_SERVICE_CONDITIONS:
        raise ValueError("service_alert_condition_not_allowed")
    return value


def _severity(value: str) -> str:
    if value not in {"WARNING", "CRITICAL"}:
        raise ValueError("service_alert_severity_invalid")
    return value


def _bounded_int(value: int, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}_invalid")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}_invalid")
    return value


def _uuid(value: uuid.UUID | str, name: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{name}_invalid") from exc


def _validated_endpoint_url(value: str) -> str:
    if value != value.strip() or not 1 <= len(value) <= 2048:
        raise ValueError("alert_endpoint_url_invalid")
    if any(ord(character) < 32 for character in value):
        raise ValueError("alert_endpoint_url_invalid")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("alert_endpoint_url_invalid") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("alert_endpoint_url_invalid")
    if parsed.scheme == "http" and hostname.lower() not in _HTTP_LOOPBACK_HOSTS:
        raise ValueError("alert_endpoint_url_requires_https_or_loopback")
    return value


def _alert(row: Any) -> ServiceAlert:
    return ServiceAlert(
        alert_id=row[0],
        idempotency_key=str(row[1]),
        binding_hash=str(row[2]),
        condition_code=str(row[3]),
        severity=str(row[4]),
        source_actor_id=str(row[5]),
        status=str(row[6]),
        opened_at=row[7],
        acknowledgment_deadline_at=row[8],
        acknowledged_by=str(row[9]),
        acknowledgment_reason=str(row[10]),
        acknowledged_at=row[11],
        resolved_by=str(row[12]),
        resolution_reason=str(row[13]),
        resolved_at=row[14],
        escalation_level=int(row[15]),
        last_event_hash=str(row[16]),
        updated_at=row[17],
    )


def _claim(row: Any) -> AlertDeliveryClaim:
    return AlertDeliveryClaim(
        delivery_id=row[0],
        alert_id=row[1],
        delivery_key=str(row[2]),
        endpoint_id=str(row[3]),
        escalation_level=int(row[4]),
        worker_id=str(row[5]),
        lease_token=row[6],
        fencing_token=int(row[7]),
        lease_expires_at=row[8],
        attempt_count=int(row[9]),
        condition_code=str(row[10]),
        severity=str(row[11]),
        opened_at=row[12],
    )


def _event(row: Any) -> AlertEvent:
    return AlertEvent(
        event_id=row[0],
        alert_id=row[1],
        sequence=int(row[2]),
        event_type=str(row[3]),
        actor_id=str(row[4]),
        reason_code=str(row[5]),
        occurred_at=row[6],
        details_hash=str(row[7]),
        prior_event_hash=str(row[8]),
        event_hash=str(row[9]),
    )


__all__ = [
    "ACKNOWLEDGED",
    "ALLOWED_SERVICE_CONDITIONS",
    "AlertDeliveryClaim",
    "AlertEvent",
    "DELIVERED",
    "FAILED",
    "LoopbackOrHttpsAlertTransport",
    "OPEN",
    "PENDING",
    "RESOLVED",
    "ServiceAlert",
    "ServiceAlertStore",
]
