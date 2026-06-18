from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable


DAILY_PARTICIPATION_CLAIM_STATUSES = (
    "unclaimed",
    "claim_pending",
    "submitted",
    "partially_filled",
    "fulfilled",
    "retryable_failed",
    "terminal_failed",
)
BLOCKING_DAILY_PARTICIPATION_CLAIM_STATUSES = ("claim_pending", "submitted", "partially_filled", "retryable_failed")
FULFILLED_DAILY_PARTICIPATION_CLAIM_STATUSES = ("fulfilled",)
TERMINAL_DAILY_PARTICIPATION_CLAIM_STATUSES = ("fulfilled", "terminal_failed")


@dataclass(frozen=True)
class DailyParticipationClaimKey:
    strategy_instance_id: str
    pair: str
    kst_day: str
    participation_policy_hash: str

    def as_tuple(self) -> tuple[str, str, str, str]:
        values = (
            self.strategy_instance_id.strip(),
            self.pair.strip(),
            self.kst_day.strip(),
            self.participation_policy_hash.strip(),
        )
        if any(not value for value in values):
            raise ValueError("daily_participation_claim_key_incomplete")
        return values


def ensure_daily_participation_claims_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_participation_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_instance_id TEXT NOT NULL,
            pair TEXT NOT NULL,
            kst_day TEXT NOT NULL,
            participation_policy_hash TEXT NOT NULL,
            daily_count_snapshot_hash TEXT,
            participation_decision_hash TEXT,
            fallback_mode TEXT,
            client_order_id TEXT,
            status TEXT NOT NULL,
            retry_allowed INTEGER NOT NULL DEFAULT 0,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL,
            UNIQUE(strategy_instance_id, pair, kst_day, participation_policy_hash)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_daily_participation_claims_scope_status
        ON daily_participation_claims(strategy_instance_id, pair, kst_day, participation_policy_hash, status)
        """
    )
    columns = {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in conn.execute("PRAGMA table_info(daily_participation_claims)").fetchall()
    }
    if "retry_allowed" not in columns:
        conn.execute("ALTER TABLE daily_participation_claims ADD COLUMN retry_allowed INTEGER NOT NULL DEFAULT 0")


def upsert_daily_participation_claim(
    conn: sqlite3.Connection,
    *,
    key: DailyParticipationClaimKey,
    status: str,
    ts_ms: int,
    client_order_id: str | None = None,
    daily_count_snapshot_hash: str | None = None,
    participation_decision_hash: str | None = None,
    fallback_mode: str | None = None,
    retry_allowed: bool = False,
) -> None:
    strategy_instance_id, pair, kst_day, policy_hash = key.as_tuple()
    normalized_status = normalize_daily_participation_claim_status(status)
    conn.execute(
        """
        INSERT INTO daily_participation_claims(
            strategy_instance_id, pair, kst_day, participation_policy_hash,
            daily_count_snapshot_hash, participation_decision_hash, fallback_mode,
            client_order_id, status, retry_allowed, created_ts, updated_ts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(strategy_instance_id, pair, kst_day, participation_policy_hash) DO UPDATE SET
            daily_count_snapshot_hash=COALESCE(excluded.daily_count_snapshot_hash, daily_count_snapshot_hash),
            participation_decision_hash=COALESCE(excluded.participation_decision_hash, participation_decision_hash),
            fallback_mode=COALESCE(excluded.fallback_mode, fallback_mode),
            client_order_id=COALESCE(excluded.client_order_id, client_order_id),
            status=excluded.status,
            retry_allowed=excluded.retry_allowed,
            updated_ts=excluded.updated_ts
        """,
        (
            strategy_instance_id,
            pair,
            kst_day,
            policy_hash,
            daily_count_snapshot_hash,
            participation_decision_hash,
            fallback_mode,
            client_order_id,
            normalized_status,
            1 if retry_allowed else 0,
            int(ts_ms),
            int(ts_ms),
        ),
    )


def pending_daily_participation_claim_count(
    conn: sqlite3.Connection,
    *,
    key: DailyParticipationClaimKey,
    retry_terminal_failed_claims: bool = False,
) -> int:
    strategy_instance_id, pair, kst_day, policy_hash = key.as_tuple()
    if not _table_columns(conn, "daily_participation_claims"):
        return 0
    statuses = ["claim_pending", "submitted", "partially_filled", "retryable_failed"]
    if not retry_terminal_failed_claims:
        statuses.append("terminal_failed")
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt
        FROM daily_participation_claims
        WHERE strategy_instance_id=?
          AND pair=?
          AND kst_day=?
          AND participation_policy_hash=?
          AND status IN ({placeholders})
          AND NOT (status='retryable_failed' AND retry_allowed=1)
        """,
        (strategy_instance_id, pair, kst_day, policy_hash, *statuses),
    ).fetchone()
    return int(rows["cnt"] if hasattr(rows, "keys") else rows[0])


def sync_daily_participation_claim_from_order_status(
    conn: sqlite3.Connection,
    *,
    client_order_id: str,
    status: str,
    ts_ms: int,
) -> None:
    row = conn.execute(
        """
        SELECT strategy_instance_id, pair, daily_participation_kst_day,
               daily_participation_policy_hash, daily_count_snapshot_hash,
               participation_decision_hash, daily_participation_fallback_mode
        FROM orders
        WHERE client_order_id=?
          AND LOWER(COALESCE(strategy_name, ''))='daily_participation_sma'
          AND UPPER(side)='BUY'
        """,
        (client_order_id,),
    ).fetchone()
    if row is None:
        return
    policy_hash = str(row["daily_participation_policy_hash"] or "").strip()
    kst_day = str(row["daily_participation_kst_day"] or "").strip()
    if not policy_hash or not kst_day:
        return
    normalized_status, retry_allowed = _claim_status_from_order_status(status)
    key = DailyParticipationClaimKey(
        strategy_instance_id=str(row["strategy_instance_id"] or ""),
        pair=str(row["pair"] or ""),
        kst_day=kst_day,
        participation_policy_hash=policy_hash,
    )
    upsert_daily_participation_claim(
        conn,
        key=key,
        status=normalized_status,
        ts_ms=int(ts_ms),
        client_order_id=client_order_id,
        daily_count_snapshot_hash=str(row["daily_count_snapshot_hash"] or "") or None,
        participation_decision_hash=str(row["participation_decision_hash"] or "") or None,
        fallback_mode=str(row["daily_participation_fallback_mode"] or "") or None,
        retry_allowed=retry_allowed,
    )


def normalize_daily_participation_claim_status(status: str) -> str:
    normalized = str(status or "").strip()
    legacy_aliases = {
        "intent": "claim_pending",
        "pending": "claim_pending",
        "filled": "fulfilled",
        "cancelled": "terminal_failed",
        "canceled": "terminal_failed",
        "rejected": "terminal_failed",
        "failed": "terminal_failed",
    }
    lowered = normalized.lower()
    canonical = legacy_aliases.get(lowered, lowered)
    if canonical not in DAILY_PARTICIPATION_CLAIM_STATUSES:
        return "claim_pending"
    return canonical


def _claim_status_from_order_status(status: str) -> tuple[str, bool]:
    normalized = str(status or "").strip().upper()
    if normalized in {"FILLED", "ACCOUNTING_PENDING"}:
        return "fulfilled", False
    if normalized in {"CANCELED", "CANCELLED"}:
        return "terminal_failed", False
    if normalized == "FAILED":
        return "terminal_failed", False
    if normalized in {"SUBMIT_UNKNOWN", "RECOVERY_REQUIRED"}:
        return "retryable_failed", False
    if normalized == "PARTIAL":
        return "partially_filled", False
    if normalized in {"NEW", "CANCEL_REQUESTED"}:
        return "submitted", False
    return "claim_pending", False


def reconstruct_daily_participation_claims_from_orders(conn: sqlite3.Connection, *, now_ms: int) -> int:
    columns = _table_columns(conn, "orders")
    required = {
        "client_order_id",
        "status",
        "strategy_name",
        "side",
        "daily_participation_policy_hash",
        "daily_participation_kst_day",
    }
    if not required.issubset(columns):
        return 0
    rows = conn.execute(
        """
        SELECT client_order_id, status
        FROM orders
        WHERE LOWER(COALESCE(strategy_name, ''))='daily_participation_sma'
          AND UPPER(side)='BUY'
          AND COALESCE(daily_participation_policy_hash, '') <> ''
          AND COALESCE(daily_participation_kst_day, '') <> ''
        """
    ).fetchall()
    for row in rows:
        sync_daily_participation_claim_from_order_status(
            conn,
            client_order_id=str(row["client_order_id"] or ""),
            status=str(row["status"] or ""),
            ts_ms=int(now_ms),
        )
    return len(rows)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}


def claim_rows(conn: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    rows: Iterable[sqlite3.Row] = conn.execute(
        """
        SELECT strategy_instance_id, pair, kst_day, participation_policy_hash, status, retry_allowed, client_order_id
        FROM daily_participation_claims
        ORDER BY strategy_instance_id, pair, kst_day, participation_policy_hash
        """
    ).fetchall()
    return tuple({key: row[key] for key in row.keys()} for row in rows)


__all__ = [
    "DailyParticipationClaimKey",
    "ensure_daily_participation_claims_schema",
    "pending_daily_participation_claim_count",
    "reconstruct_daily_participation_claims_from_orders",
    "sync_daily_participation_claim_from_order_status",
    "upsert_daily_participation_claim",
]
