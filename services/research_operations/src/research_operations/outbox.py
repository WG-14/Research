"""Durable scanner and fenced delivery state for Research audit intents."""

from __future__ import annotations

import math
import os
import random
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .database import (
    assert_claim_admission_open,
    assert_mutation_admission_open,
    connection,
)
from .errors import ClaimLost, OutboxBindingConflict, OutboxReplayRejected
from .release import configured_release, configured_release_bundle_digest

PENDING = "PENDING"
CLAIMED = "CLAIMED"
PROJECTED = "PROJECTED"
DEAD_LETTER = "DEAD_LETTER"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*[^\s,;]+"
)


@dataclass(frozen=True, slots=True)
class OutboxClaim:
    event_id: uuid.UUID
    event_type: str
    payload_hash: str
    worker_id: str
    lease_token: uuid.UUID
    fencing_token: int
    lease_expires_at: datetime
    attempt_count: int


@dataclass(frozen=True, slots=True)
class OutboxMetrics:
    pending_count: int
    claimed_count: int
    dead_letter_count: int
    oldest_pending_age_seconds: float


@dataclass(frozen=True, slots=True)
class FailureDisposition:
    event_id: uuid.UUID
    status: str
    available_at: datetime | None
    attempt_count: int


def utcnow() -> datetime:
    return datetime.now(UTC)


def bounded_retry_delay(
    attempt_count: int,
    *,
    base_seconds: float = 1.0,
    maximum_seconds: float = 300.0,
    random_source: random.Random | None = None,
) -> float:
    """Return exponential backoff plus bounded positive jitter."""

    if attempt_count < 1:
        raise ValueError("outbox_attempt_count_invalid")
    if base_seconds <= 0 or maximum_seconds < base_seconds:
        raise ValueError("outbox_retry_bounds_invalid")
    exponential = min(maximum_seconds, base_seconds * math.pow(2, attempt_count - 1))
    jitter_cap = min(maximum_seconds - exponential, exponential * 0.25)
    if jitter_cap <= 0:
        return maximum_seconds
    source = random_source or random.SystemRandom()
    return exponential + source.uniform(0.0, jitter_cap)


def sanitize_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    text = _SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return text[:512]


class OutboxStore:
    """Short-transaction PostgreSQL state boundary, safe across processes."""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn

    def scan(self, *, batch_size: int = 100) -> int:
        _positive_bounded(batch_size, "outbox_batch_size", maximum=10_000)
        with connection(self._dsn) as conn:
            assert_claim_admission_open(conn)
            rows = conn.execute(
                """
                WITH candidates AS (
                    SELECT source.id, source.payload_hash, source.created_at,
                           source.projected_at
                    FROM public.portal_webauditevent AS source
                    LEFT JOIN research_ops.outbox_delivery AS delivery
                      ON delivery.event_id = source.id
                    WHERE delivery.event_id IS NULL
                    ORDER BY source.created_at, source.id
                    LIMIT %s
                )
                INSERT INTO research_ops.outbox_delivery (
                    event_id, event_type, payload_hash, idempotency_key,
                    created_at, status, available_at, projected_at
                )
                SELECT id, 'internal_web_audit', payload_hash,
                       'internal_web_audit:' || id::text,
                       created_at,
                       CASE WHEN projected_at IS NULL THEN 'PENDING'
                            ELSE 'PROJECTED' END,
                       created_at,
                       projected_at
                FROM candidates
                ON CONFLICT (event_id) DO NOTHING
                RETURNING event_id
                """,
                (batch_size,),
            ).fetchall()
            conflict = conn.execute(
                """
                SELECT delivery.event_id
                FROM research_ops.outbox_delivery AS delivery
                JOIN public.portal_webauditevent AS source
                  ON source.id = delivery.event_id
                WHERE delivery.payload_hash <> source.payload_hash
                   OR delivery.idempotency_key <>
                      'internal_web_audit:' || source.id::text
                LIMIT 1
                """
            ).fetchone()
            if conflict is not None:
                raise OutboxBindingConflict(f"outbox_binding_conflict:{conflict[0]}")
        return len(rows)

    def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 30,
        max_attempts: int = 8,
        now: datetime | None = None,
    ) -> OutboxClaim | None:
        worker = _text(worker_id, "worker_id", maximum=255)
        _positive_bounded(lease_seconds, "lease_seconds", maximum=3600)
        _positive_bounded(max_attempts, "max_attempts", maximum=100)
        observed_at = now or utcnow()
        lease_token = uuid.uuid4()
        with connection(self._dsn) as conn:
            assert_claim_admission_open(conn)
            conn.execute(
                """
                UPDATE research_ops.outbox_delivery
                SET status = 'DEAD_LETTER', dead_letter_at = %s,
                    claimed_by = '', lease_token = NULL, lease_expires_at = NULL,
                    last_error_category = 'retry_exhausted',
                    last_error = 'worker lease expired after maximum attempts',
                    updated_at = %s
                WHERE status = 'CLAIMED' AND lease_expires_at <= %s
                  AND attempt_count >= %s
                """,
                (observed_at, observed_at, observed_at, max_attempts),
            )
            row = conn.execute(
                """
                WITH candidate AS (
                    SELECT event_id
                    FROM research_ops.outbox_delivery
                    WHERE (
                        (status = 'PENDING' AND available_at <= %(now)s)
                        OR
                        (status = 'CLAIMED' AND lease_expires_at <= %(now)s)
                    )
                    AND attempt_count < %(max_attempts)s
                    ORDER BY available_at, created_at, event_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE research_ops.outbox_delivery AS delivery
                SET status = 'CLAIMED', claimed_by = %(worker_id)s,
                    lease_token = %(lease_token)s,
                    fencing_token = delivery.fencing_token + 1,
                    lease_expires_at = %(lease_expires_at)s,
                    attempt_count = delivery.attempt_count + 1,
                    last_attempted_at = %(now)s,
                    updated_at = %(now)s
                FROM candidate
                WHERE delivery.event_id = candidate.event_id
                RETURNING delivery.event_id, delivery.event_type,
                          delivery.payload_hash, delivery.claimed_by,
                          delivery.lease_token, delivery.fencing_token,
                          delivery.lease_expires_at, delivery.attempt_count
                """,
                {
                    "now": observed_at,
                    "max_attempts": max_attempts,
                    "worker_id": worker,
                    "lease_token": lease_token,
                    "lease_expires_at": observed_at + timedelta(seconds=lease_seconds),
                },
            ).fetchone()
        if row is None:
            return None
        return OutboxClaim(
            event_id=row[0],
            event_type=row[1],
            payload_hash=row[2],
            worker_id=row[3],
            lease_token=row[4],
            fencing_token=int(row[5]),
            lease_expires_at=row[6],
            attempt_count=int(row[7]),
        )

    def heartbeat(
        self,
        claim: OutboxClaim,
        *,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> datetime:
        _positive_bounded(lease_seconds, "lease_seconds", maximum=3600)
        observed_at = now or utcnow()
        expires_at = observed_at + timedelta(seconds=lease_seconds)
        with connection(self._dsn) as conn:
            row = conn.execute(
                """
                UPDATE research_ops.outbox_delivery
                SET lease_expires_at = %s, updated_at = %s
                WHERE event_id = %s AND status = 'CLAIMED'
                  AND claimed_by = %s AND lease_token = %s
                  AND fencing_token = %s AND lease_expires_at > %s
                RETURNING lease_expires_at
                """,
                (
                    expires_at,
                    observed_at,
                    claim.event_id,
                    claim.worker_id,
                    claim.lease_token,
                    claim.fencing_token,
                    observed_at,
                ),
            ).fetchone()
        if row is None:
            raise ClaimLost("outbox_claim_lost")
        lease_expires_at = row[0]
        if not isinstance(lease_expires_at, datetime):
            raise RuntimeError("outbox_lease_expiry_invalid")
        return lease_expires_at

    def mark_projected(
        self,
        claim: OutboxClaim,
        *,
        now: datetime | None = None,
    ) -> None:
        observed_at = now or utcnow()
        with connection(self._dsn) as conn:
            row = conn.execute(
                """
                UPDATE research_ops.outbox_delivery
                SET status = 'PROJECTED', projected_at = %s,
                    claimed_by = '', lease_token = NULL, lease_expires_at = NULL,
                    last_error_category = '', last_error = '', updated_at = %s
                WHERE event_id = %s AND status = 'CLAIMED'
                  AND claimed_by = %s AND lease_token = %s
                  AND fencing_token = %s AND lease_expires_at > %s
                  AND payload_hash = %s
                RETURNING event_id
                """,
                (
                    observed_at,
                    observed_at,
                    claim.event_id,
                    claim.worker_id,
                    claim.lease_token,
                    claim.fencing_token,
                    observed_at,
                    claim.payload_hash,
                ),
            ).fetchone()
        if row is None:
            raise ClaimLost("outbox_claim_lost")

    def record_failure(
        self,
        claim: OutboxClaim,
        *,
        category: str,
        error: str,
        permanent: bool,
        max_attempts: int,
        retry_delay_seconds: float,
        now: datetime | None = None,
    ) -> FailureDisposition:
        normalized_category = _text(category, "error_category", maximum=64)
        normalized_error = " ".join(error.split())
        normalized_error = _SECRET_RE.sub(
            lambda match: f"{match.group(1)}=<redacted>",
            normalized_error,
        )[:512]
        _positive_bounded(max_attempts, "max_attempts", maximum=100)
        if retry_delay_seconds < 0 or retry_delay_seconds > 3600:
            raise ValueError("retry_delay_seconds_invalid")
        observed_at = now or utcnow()
        dead_letter = permanent or claim.attempt_count >= max_attempts
        status = DEAD_LETTER if dead_letter else PENDING
        available_at = (
            None
            if dead_letter
            else observed_at + timedelta(seconds=retry_delay_seconds)
        )
        with connection(self._dsn) as conn:
            row = conn.execute(
                """
                UPDATE research_ops.outbox_delivery
                SET status = %(status)s,
                    available_at = COALESCE(%(available_at)s, available_at),
                    last_error_category = %(category)s,
                    last_error = %(error)s,
                    claimed_by = '', lease_token = NULL, lease_expires_at = NULL,
                    dead_letter_at = CASE
                        WHEN %(dead_letter)s THEN %(now)s ELSE NULL END,
                    updated_at = %(now)s
                WHERE event_id = %(event_id)s AND status = 'CLAIMED'
                  AND claimed_by = %(worker_id)s AND lease_token = %(lease_token)s
                  AND fencing_token = %(fencing_token)s
                  AND lease_expires_at > %(now)s
                RETURNING attempt_count
                """,
                {
                    "status": status,
                    "available_at": available_at,
                    "category": normalized_category,
                    "error": normalized_error,
                    "dead_letter": dead_letter,
                    "now": observed_at,
                    "event_id": claim.event_id,
                    "worker_id": claim.worker_id,
                    "lease_token": claim.lease_token,
                    "fencing_token": claim.fencing_token,
                },
            ).fetchone()
        if row is None:
            raise ClaimLost("outbox_claim_lost")
        return FailureDisposition(
            event_id=claim.event_id,
            status=status,
            available_at=available_at,
            attempt_count=int(row[0]),
        )

    def requeue_dead_letter(
        self,
        *,
        event_id: uuid.UUID | str,
        expected_payload_hash: str,
        operator_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        normalized_id = _uuid(event_id, "event_id")
        _hash(expected_payload_hash, "expected_payload_hash")
        operator = _text(operator_id, "operator_id", maximum=255)
        normalized_reason = _text(reason, "reason", maximum=255)
        observed_at = now or utcnow()
        with connection(self._dsn) as conn:
            assert_mutation_admission_open(conn)
            row = conn.execute(
                """
                UPDATE research_ops.outbox_delivery AS delivery
                SET status = 'PENDING', available_at = %s, attempt_count = 0,
                    last_attempted_at = NULL, last_error_category = '',
                    last_error = '', dead_letter_at = NULL, updated_at = %s
                FROM public.portal_webauditevent AS source
                WHERE delivery.event_id = %s
                  AND delivery.status = 'DEAD_LETTER'
                  AND delivery.payload_hash = %s
                  AND source.id = delivery.event_id
                  AND source.payload_hash = delivery.payload_hash
                RETURNING delivery.event_id
                """,
                (observed_at, observed_at, normalized_id, expected_payload_hash),
            ).fetchone()
            if row is None:
                raise OutboxReplayRejected("outbox_requeue_binding_or_state_invalid")
            conn.execute(
                """
                INSERT INTO research_ops.outbox_operator_action (
                    action_id, event_id, action, expected_payload_hash,
                    operator_id, reason, created_at
                ) VALUES (%s, %s, 'REQUEUE', %s, %s, %s, %s)
                """,
                (
                    uuid.uuid4(),
                    normalized_id,
                    expected_payload_hash,
                    operator,
                    normalized_reason,
                    observed_at,
                ),
            )

    def metrics(self, *, now: datetime | None = None) -> OutboxMetrics:
        observed_at = now or utcnow()
        with connection(self._dsn) as conn:
            row = conn.execute(
                """
                SELECT
                    count(*) FILTER (WHERE status = 'PENDING'),
                    count(*) FILTER (WHERE status = 'CLAIMED'),
                    count(*) FILTER (WHERE status = 'DEAD_LETTER'),
                    COALESCE(EXTRACT(EPOCH FROM
                        (%s - min(created_at) FILTER (WHERE status = 'PENDING'))), 0)
                FROM research_ops.outbox_delivery
                """,
                (observed_at,),
            ).fetchone()
        if row is None:
            raise RuntimeError("outbox_metrics_query_returned_no_row")
        return OutboxMetrics(int(row[0]), int(row[1]), int(row[2]), float(row[3]))

    def worker_heartbeat(
        self,
        *,
        worker_id: str,
        state: str,
        event_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> None:
        worker = _text(worker_id, "worker_id", maximum=255)
        if state not in {"STARTING", "IDLE", "WORKING", "DRAINING", "STOPPED"}:
            raise ValueError("worker_state_invalid")
        observed_at = now or utcnow()
        release = configured_release()
        release_bundle_digest = configured_release_bundle_digest()
        with connection(self._dsn) as conn:
            conn.execute(
                """
                INSERT INTO research_ops.worker_heartbeat (
                    worker_id, process_id, state, current_event_id,
                    started_at, last_seen_at, stopped_at,
                    git_sha, release_id, build_digest,
                    release_bundle_digest, release_seen_at
                ) VALUES (%s, %s, %s, %s, %s, %s,
                          CASE WHEN %s = 'STOPPED' THEN %s ELSE NULL END,
                          %s, %s, %s, %s, %s)
                ON CONFLICT (worker_id) DO UPDATE SET
                    process_id = EXCLUDED.process_id,
                    state = EXCLUDED.state,
                    current_event_id = EXCLUDED.current_event_id,
                    started_at = CASE
                        WHEN EXCLUDED.state = 'STARTING'
                        THEN EXCLUDED.started_at
                        ELSE research_ops.worker_heartbeat.started_at END,
                    last_seen_at = EXCLUDED.last_seen_at,
                    stopped_at = EXCLUDED.stopped_at,
                    git_sha = EXCLUDED.git_sha,
                    release_id = EXCLUDED.release_id,
                    build_digest = EXCLUDED.build_digest,
                    release_bundle_digest = EXCLUDED.release_bundle_digest,
                    release_seen_at = EXCLUDED.release_seen_at
                """,
                (
                    worker,
                    os.getpid(),
                    state,
                    event_id,
                    observed_at,
                    observed_at,
                    state,
                    observed_at,
                    release.git_sha,
                    release.release_id,
                    release.build_digest,
                    release_bundle_digest,
                    observed_at,
                ),
            )


def _positive_bounded(value: int, field: str, *, maximum: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(f"{field}_invalid")


def _text(value: object, field: str, *, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ValueError(f"{field}_invalid")
    return normalized


def _hash(value: str, field: str) -> str:
    normalized = str(value or "")
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field}_invalid")
    return normalized


def _uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field}_invalid") from exc


__all__ = [
    "CLAIMED",
    "DEAD_LETTER",
    "PENDING",
    "PROJECTED",
    "FailureDisposition",
    "OutboxClaim",
    "OutboxMetrics",
    "OutboxStore",
    "bounded_retry_delay",
    "sanitize_error",
]
