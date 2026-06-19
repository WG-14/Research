from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


H74_OBSERVATION_REPORT_FIELDS = (
    "observation_start",
    "observation_end",
    "eligible_kst_days",
    "daily_buy_intent_count",
    "daily_buy_submitted_count",
    "daily_buy_filled_count",
    "duplicate_entry_block_count",
    "claim_pending_count",
    "claim_fulfilled_count",
    "claim_terminal_failed_count",
    "max_holding_exit_due_count",
    "max_holding_exit_filled_count",
    "exit_delay_seconds_p50",
    "exit_delay_seconds_max",
    "fee_total_krw",
    "observed_fee_bps",
    "slippage_bps_avg",
    "broker_local_mismatch_count",
    "manual_intervention_count",
)


def build_h74_observation_report(
    *,
    conn: sqlite3.Connection | None = None,
    days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = end - timedelta(days=int(days))
    payload: dict[str, Any] = {
        "artifact_type": "h74_live_observation_report",
        "strategy_name": "daily_participation_sma",
        "observation_start": start.isoformat(),
        "observation_end": end.isoformat(),
        "eligible_kst_days": int(days),
        "complete": int(days) >= 7,
        "source_backtest_pnl": None,
        "live_observed_pnl": None,
    }
    metrics = {field: 0 for field in H74_OBSERVATION_REPORT_FIELDS if field not in payload}
    metrics["exit_delay_seconds_p50"] = 0.0
    metrics["exit_delay_seconds_max"] = 0.0
    metrics["fee_total_krw"] = 0.0
    metrics["observed_fee_bps"] = 0.0
    metrics["slippage_bps_avg"] = 0.0
    if conn is not None:
        metrics.update(_sqlite_metrics(conn))
    payload.update(metrics)
    return payload


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _sqlite_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    if _table_exists(conn, "orders"):
        metrics["daily_buy_intent_count"] = int(conn.execute(
            "SELECT COUNT(*) FROM orders WHERE strategy_name='daily_participation_sma' AND UPPER(side)='BUY'"
        ).fetchone()[0])
        metrics["daily_buy_submitted_count"] = int(conn.execute(
            "SELECT COUNT(*) FROM orders WHERE strategy_name='daily_participation_sma' AND UPPER(side)='BUY' AND status NOT IN ('FAILED','CANCELED','CANCELLED')"
        ).fetchone()[0])
        metrics["max_holding_exit_due_count"] = int(conn.execute(
            "SELECT COUNT(*) FROM orders WHERE strategy_name='daily_participation_sma' AND UPPER(side)='SELL' AND COALESCE(exit_rule_name,'')='max_holding_time'"
        ).fetchone()[0])
        metrics["max_holding_exit_filled_count"] = int(conn.execute(
            "SELECT COUNT(*) FROM orders WHERE strategy_name='daily_participation_sma' AND UPPER(side)='SELL' AND COALESCE(exit_rule_name,'')='max_holding_time' AND status IN ('FILLED','ACCOUNTING_PENDING')"
        ).fetchone()[0])
        metrics["manual_intervention_count"] = int(conn.execute(
            "SELECT COUNT(*) FROM orders WHERE COALESCE(decision_reason,'') IN ('manual_flatten','operator_smoke','operator_closeout')"
        ).fetchone()[0])
        metrics["broker_local_mismatch_count"] = int(conn.execute(
            "SELECT COUNT(*) FROM orders WHERE COALESCE(last_error,'') LIKE '%mismatch%'"
        ).fetchone()[0])
    if _table_exists(conn, "fills"):
        metrics["daily_buy_filled_count"] = int(conn.execute(
            """
            SELECT COUNT(*) FROM fills f JOIN orders o ON o.client_order_id=f.client_order_id
            WHERE o.strategy_name='daily_participation_sma' AND UPPER(o.side)='BUY'
            """
        ).fetchone()[0])
        metrics["fee_total_krw"] = float(conn.execute("SELECT COALESCE(SUM(fee),0.0) FROM fills").fetchone()[0] or 0.0)
    if _table_exists(conn, "daily_participation_claims"):
        rows = conn.execute("SELECT status, COUNT(*) FROM daily_participation_claims GROUP BY status").fetchall()
        status_counts = {str(row[0]): int(row[1]) for row in rows}
        metrics["claim_pending_count"] = status_counts.get("claim_pending", 0) + status_counts.get("submitted", 0)
        metrics["claim_fulfilled_count"] = status_counts.get("fulfilled", 0)
        metrics["claim_terminal_failed_count"] = status_counts.get("terminal_failed", 0)
    metrics["duplicate_entry_block_count"] = max(0, int(metrics.get("daily_buy_filled_count", 0)) - int(metrics.get("eligible_kst_days", 7)))
    return metrics


def cmd_h74_observation_report(*, db_path: str | None = None, days: int = 7, as_json: bool = False) -> int:
    conn = sqlite3.connect(f"file:{Path(db_path).expanduser().resolve()}?mode=ro", uri=True) if db_path else None
    try:
        report = build_h74_observation_report(conn=conn, days=days)
    finally:
        if conn is not None:
            conn.close()
    if as_json:
        print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    else:
        print(f"h74_observation_report complete={report['complete']} days={days}")
    return 0
