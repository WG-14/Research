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
    authority_hash: str | None = None,
    strategy_instance_id: str | None = None,
    pair: str = "KRW-BTC",
    interval: str = "1m",
) -> dict[str, Any]:
    end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = end - timedelta(days=int(days))
    payload: dict[str, Any] = {
        "artifact_type": "h74_live_observation_report",
        "strategy_name": "daily_participation_sma",
        "observation_start": start.isoformat(),
        "observation_end": end.isoformat(),
        "eligible_kst_days": int(days),
        "complete": False,
        "authority_hash": authority_hash,
        "strategy_instance_id": strategy_instance_id,
        "pair": pair,
        "interval": interval,
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
        metrics.update(
            _sqlite_metrics(
                conn,
                start_ts=int(start.timestamp() * 1000),
                end_ts=int(end.timestamp() * 1000),
                authority_hash=authority_hash,
                strategy_instance_id=strategy_instance_id,
                pair=pair,
            )
        )
    payload.update(metrics)
    elapsed_seconds = (end - start).total_seconds()
    payload["complete"] = bool(elapsed_seconds >= int(days) * 86400 and int(days) >= 7 and payload["daily_buy_filled_count"] >= int(days))
    return payload


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    return column in {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _time_filter(conn: sqlite3.Connection, table: str, column: str, start_ts: int, end_ts: int) -> tuple[str, list[Any]]:
    if _column_exists(conn, table, column):
        return f" AND {column} >= ? AND {column} < ?", [start_ts, end_ts]
    return "", []


def _order_scope_filter(
    conn: sqlite3.Connection,
    *,
    start_ts: int,
    end_ts: int,
    authority_hash: str | None,
    strategy_instance_id: str | None,
    pair: str,
) -> tuple[str, list[Any]]:
    clauses = ["strategy_name='daily_participation_sma'"]
    params: list[Any] = []
    if _column_exists(conn, "orders", "created_ts"):
        clauses.append("created_ts >= ? AND created_ts < ?")
        params.extend([start_ts, end_ts])
    if _column_exists(conn, "orders", "pair"):
        clauses.append("(pair IS NULL OR pair='' OR pair=?)")
        params.append(pair)
    if strategy_instance_id and _column_exists(conn, "orders", "strategy_instance_id"):
        clauses.append("strategy_instance_id=?")
        params.append(strategy_instance_id)
    if authority_hash and _column_exists(conn, "orders", "submit_truth_source_fields"):
        clauses.append("COALESCE(submit_truth_source_fields,'') LIKE ?")
        params.append(f"%{authority_hash}%")
    return " AND ".join(clauses), params


def _sqlite_metrics(
    conn: sqlite3.Connection,
    *,
    start_ts: int,
    end_ts: int,
    authority_hash: str | None,
    strategy_instance_id: str | None,
    pair: str,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    if _table_exists(conn, "orders"):
        scope_sql, scope_params = _order_scope_filter(
            conn,
            start_ts=start_ts,
            end_ts=end_ts,
            authority_hash=authority_hash,
            strategy_instance_id=strategy_instance_id,
            pair=pair,
        )
        metrics["daily_buy_intent_count"] = int(conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE {scope_sql} AND UPPER(side)='BUY'",
            scope_params,
        ).fetchone()[0])
        metrics["daily_buy_submitted_count"] = int(conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE {scope_sql} AND UPPER(side)='BUY' AND status NOT IN ('FAILED','CANCELED','CANCELLED')",
            scope_params,
        ).fetchone()[0])
        metrics["max_holding_exit_due_count"] = int(conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE {scope_sql} AND UPPER(side)='SELL' AND COALESCE(exit_rule_name,'')='max_holding_time'",
            scope_params,
        ).fetchone()[0])
        metrics["max_holding_exit_filled_count"] = int(conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE {scope_sql} AND UPPER(side)='SELL' AND COALESCE(exit_rule_name,'')='max_holding_time' AND status IN ('FILLED','ACCOUNTING_PENDING')",
            scope_params,
        ).fetchone()[0])
        metrics["manual_intervention_count"] = int(conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE {scope_sql} AND COALESCE(decision_reason,'') IN ('manual_flatten','operator_closeout')",
            scope_params,
        ).fetchone()[0])
        metrics["broker_local_mismatch_count"] = int(conn.execute(
            f"SELECT COUNT(*) FROM orders WHERE {scope_sql} AND COALESCE(last_error,'') LIKE '%mismatch%'",
            scope_params,
        ).fetchone()[0])
        day_expr = (
            "date(datetime(created_ts / 1000, 'unixepoch', '+9 hours'))"
            if _column_exists(conn, "orders", "created_ts")
            else "'unknown'"
        )
        duplicate_rows = conn.execute(
            f"""
            SELECT {day_expr} AS kst_day, COUNT(*) AS cnt
            FROM orders
            WHERE {scope_sql} AND UPPER(side)='BUY' AND status IN ('FILLED','ACCOUNTING_PENDING')
            GROUP BY {day_expr}
            """,
            scope_params,
        ).fetchall()
        metrics["duplicate_entry_block_count"] = sum(max(0, int(row[1]) - 1) for row in duplicate_rows)
    if _table_exists(conn, "fills"):
        time_sql, time_params = _time_filter(conn, "fills", "fill_ts", start_ts, end_ts)
        scope_sql, scope_params = _order_scope_filter(
            conn,
            start_ts=start_ts,
            end_ts=end_ts,
            authority_hash=authority_hash,
            strategy_instance_id=strategy_instance_id,
            pair=pair,
        )
        metrics["daily_buy_filled_count"] = int(conn.execute(
            f"""
            SELECT COUNT(*) FROM fills f JOIN orders o ON o.client_order_id=f.client_order_id
            WHERE {scope_sql} AND UPPER(o.side)='BUY'{time_sql.replace('fill_ts', 'f.fill_ts')}
            """,
            scope_params + time_params,
        ).fetchone()[0])
        price_expr = "COALESCE(f.price,0.0)" if _column_exists(conn, "fills", "price") else "0.0"
        qty_expr = "COALESCE(f.qty,0.0)" if _column_exists(conn, "fills", "qty") else "0.0"
        reference_expr = (
            "COALESCE(f.reference_price,0.0)" if _column_exists(conn, "fills", "reference_price") else "0.0"
        )
        slippage_expr = "f.slippage_bps" if _column_exists(conn, "fills", "slippage_bps") else "NULL"
        fill_ts_expr = "f.fill_ts" if _column_exists(conn, "fills", "fill_ts") else "0"
        created_ts_expr = "o.created_ts" if _column_exists(conn, "orders", "created_ts") else "0"
        fill_rows = conn.execute(
            f"""
            SELECT COALESCE(f.fee,0.0) AS fee, {price_expr} AS price, {qty_expr} AS qty,
                   {reference_expr} AS reference_price,
                   {slippage_expr} AS slippage_bps,
                   {fill_ts_expr} AS fill_ts,
                   {created_ts_expr} AS created_ts
            FROM fills f JOIN orders o ON o.client_order_id=f.client_order_id
            WHERE {scope_sql}{time_sql.replace('fill_ts', 'f.fill_ts')}
            """,
            scope_params + time_params,
        ).fetchall()
        metrics["fee_total_krw"] = float(sum(float(row[0] or 0.0) for row in fill_rows))
        notional = sum(float(row[1] or 0.0) * float(row[2] or 0.0) for row in fill_rows)
        metrics["observed_fee_bps"] = (float(metrics["fee_total_krw"]) / notional * 10_000.0) if notional > 0 else 0.0
        slippages: list[float] = []
        delays: list[float] = []
        for row in fill_rows:
            raw_slippage = row[4]
            if raw_slippage is not None:
                slippages.append(float(raw_slippage))
            elif float(row[3] or 0.0) > 0 and float(row[1] or 0.0) > 0:
                slippages.append(((float(row[1]) - float(row[3])) / float(row[3])) * 10_000.0)
            if _column_exists(conn, "orders", "created_ts") and _column_exists(conn, "fills", "fill_ts"):
                delays.append(max(0.0, (float(row[5] or 0) - float(row[6] or 0)) / 1000.0))
        metrics["slippage_bps_avg"] = sum(slippages) / len(slippages) if slippages else 0.0
        if delays:
            ordered = sorted(delays)
            metrics["exit_delay_seconds_p50"] = ordered[len(ordered) // 2]
            metrics["exit_delay_seconds_max"] = max(ordered)
    if _table_exists(conn, "daily_participation_claims"):
        rows = conn.execute("SELECT status, COUNT(*) FROM daily_participation_claims GROUP BY status").fetchall()
        status_counts = {str(row[0]): int(row[1]) for row in rows}
        metrics["claim_pending_count"] = status_counts.get("claim_pending", 0) + status_counts.get("submitted", 0)
        metrics["claim_fulfilled_count"] = status_counts.get("fulfilled", 0)
        metrics["claim_terminal_failed_count"] = status_counts.get("terminal_failed", 0)
    metrics.setdefault("duplicate_entry_block_count", 0)
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
