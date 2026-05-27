from __future__ import annotations

import logging

from . import runtime_state
from .config import settings
from .db_core import ensure_db
from .dust import build_dust_display_context, build_position_state_model
from .lifecycle import summarize_position_lots, summarize_reserved_exit_qty
from .observability import format_log_kv


RUN_LOG = logging.getLogger("bithumb_bot.run")
LIVE_UNRESOLVED_ORDER_STATUSES = (
    "PENDING_SUBMIT",
    "NEW",
    "PARTIAL",
    "SUBMIT_UNKNOWN",
    "ACCOUNTING_PENDING",
    "CANCEL_REQUESTED",
)


def select_latest_candle(conn, *, pair: str, interval: str):
    return conn.execute(
        """
        SELECT ts, close
        FROM candles
        WHERE pair=? AND interval=?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (pair, interval),
    ).fetchone()


def select_latest_closed_candle(
    conn,
    *,
    pair: str,
    interval: str,
    interval_sec: int,
    now_ms: int,
    is_closed_candle,
):
    cursor = conn.execute(
        """
        SELECT ts, close
        FROM candles
        WHERE pair=? AND interval=?
        ORDER BY ts DESC
        LIMIT 5
        """,
        (pair, interval),
    )
    if hasattr(cursor, "fetchall"):
        rows = cursor.fetchall()
    else:
        row = cursor.fetchone()
        if row is None:
            return None, None
        return row, None
    if not rows:
        return None, None

    latest_row = rows[0]
    latest_ts = int(latest_row["ts"]) if hasattr(latest_row, "keys") else int(latest_row[0])
    incomplete_ts = None
    if not is_closed_candle(candle_ts_ms=latest_ts, now_ms=now_ms, interval_sec=interval_sec):
        incomplete_ts = latest_ts

    for row in rows:
        candle_ts_ms = int(row["ts"]) if hasattr(row, "keys") else int(row[0])
        if is_closed_candle(candle_ts_ms=candle_ts_ms, now_ms=now_ms, interval_sec=interval_sec):
            return row, incomplete_ts

    return None, incomplete_ts


def open_order_snapshot(now_ms: int) -> tuple[int, float | None]:
    conn = ensure_db()
    try:
        placeholders = ",".join("?" for _ in LIVE_UNRESOLVED_ORDER_STATUSES)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS open_count, MIN(created_ts) AS oldest_created_ts
            FROM orders
            WHERE status IN ({placeholders})
            """,
            LIVE_UNRESOLVED_ORDER_STATUSES,
        ).fetchone()
        open_count = int(row["open_count"])
        oldest_created_ts = (
            int(row["oldest_created_ts"])
            if row["oldest_created_ts"] is not None
            else None
        )
        if open_count <= 0 or oldest_created_ts is None:
            return 0, None
        age_sec = max(0.0, (now_ms - oldest_created_ts) / 1000)
        return open_count, age_sec
    finally:
        conn.close()


def mark_open_orders_recovery_required(reason: str, now_ms: int) -> int:
    conn = ensure_db()
    try:
        placeholders = ",".join("?" for _ in LIVE_UNRESOLVED_ORDER_STATUSES)
        res = conn.execute(
            f"""
            UPDATE orders
            SET status='RECOVERY_REQUIRED', updated_ts=?, last_error=?
            WHERE status IN ({placeholders})
            """,
            (now_ms, reason, *LIVE_UNRESOLVED_ORDER_STATUSES),
        )
        conn.commit()
        return int(res.rowcount or 0)
    finally:
        conn.close()


def count_open_orders() -> int:
    conn = ensure_db()
    try:
        placeholders = ",".join("?" for _ in LIVE_UNRESOLVED_ORDER_STATUSES)
        row = conn.execute(
            f"SELECT COUNT(*) AS open_order_count FROM orders WHERE status IN ({placeholders})",
            LIVE_UNRESOLVED_ORDER_STATUSES,
        ).fetchone()
        return int(row["open_order_count"] or 0) if row is not None else 0
    finally:
        conn.close()


def latest_order_identifiers() -> tuple[str | None, str | None]:
    conn = ensure_db()
    try:
        row = conn.execute(
            """
            SELECT client_order_id, exchange_order_id
            FROM orders
            WHERE status IN ('PENDING_SUBMIT', 'NEW', 'PARTIAL', 'SUBMIT_UNKNOWN', 'ACCOUNTING_PENDING', 'RECOVERY_REQUIRED', 'CANCEL_REQUESTED')
            ORDER BY updated_ts DESC, created_ts DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None, None
        return row["client_order_id"], row["exchange_order_id"]
    finally:
        conn.close()


def open_order_identifiers_for_broker_revalidation() -> tuple[list[str], list[str]]:
    conn = ensure_db()
    try:
        placeholders = ",".join("?" for _ in LIVE_UNRESOLVED_ORDER_STATUSES)
        rows = conn.execute(
            f"""
            SELECT client_order_id, exchange_order_id
            FROM orders
            WHERE status IN ({placeholders})
            """,
            LIVE_UNRESOLVED_ORDER_STATUSES,
        ).fetchall()
    finally:
        conn.close()
    exchange_order_ids = sorted(
        {
            str(row["exchange_order_id"]).strip()
            for row in rows
            if str(row["exchange_order_id"] or "").strip()
        }
    )
    client_order_ids = sorted(
        {
            str(row["client_order_id"]).strip()
            for row in rows
            if str(row["client_order_id"] or "").strip()
        }
    )
    return client_order_ids, exchange_order_ids


def portfolio_cash_qty_with_position_state(*, pair: str):
    conn = ensure_db()
    try:
        portfolio = conn.execute(
            "SELECT cash_krw, asset_qty FROM portfolio WHERE id=1"
        ).fetchone()
        if portfolio is None:
            return 0.0, 0.0, None, None
        portfolio_cash = float(portfolio["cash_krw"])
        portfolio_qty = float(portfolio["asset_qty"])
        dust_context = build_dust_display_context(
            runtime_state.snapshot().last_reconcile_metadata
        )
        lot_snapshot = summarize_position_lots(conn, pair=pair)
        lot_definition = getattr(lot_snapshot, "lot_definition", None)
        position_state = build_position_state_model(
            raw_qty_open=portfolio_qty,
            metadata_raw=runtime_state.snapshot().last_reconcile_metadata,
            raw_total_asset_qty=max(
                portfolio_qty,
                float(lot_snapshot.raw_total_asset_qty),
                float(dust_context.raw_holdings.broker_qty),
            ),
            open_exposure_qty=float(lot_snapshot.raw_open_exposure_qty),
            dust_tracking_qty=float(lot_snapshot.dust_tracking_qty),
            open_lot_count=int(lot_snapshot.open_lot_count),
            dust_tracking_lot_count=int(lot_snapshot.dust_tracking_lot_count),
            internal_lot_size=(None if lot_definition is None else lot_definition.internal_lot_size),
            min_qty=(None if lot_definition is None else lot_definition.min_qty),
            qty_step=(None if lot_definition is None else lot_definition.qty_step),
            min_notional_krw=(None if lot_definition is None else lot_definition.min_notional_krw),
            max_qty_decimals=(None if lot_definition is None else lot_definition.max_qty_decimals),
        )
        return portfolio_cash, portfolio_qty, position_state, lot_definition
    finally:
        conn.close()


def position_summary(*, pair: str | None = None) -> str:
    pair = str(pair or settings.PAIR)
    conn = ensure_db()
    try:
        row = conn.execute("SELECT asset_qty FROM portfolio WHERE id=1").fetchone()
        state = runtime_state.snapshot()
        try:
            reserved_exit_qty = summarize_reserved_exit_qty(conn, pair=pair)
            lot_snapshot = summarize_position_lots(conn, pair=pair)
        except Exception as exc:
            RUN_LOG.warning(
                format_log_kv(
                    "[RUN] position summary unavailable",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            qty = float(row["asset_qty"] or 0.0) if row is not None else 0.0
            return f"position=unknown qty={qty:.8f} reason=lot_snapshot_unavailable"
    finally:
        conn.close()

    qty = float(row["asset_qty"] or 0.0) if row is not None else 0.0
    dust_context = build_dust_display_context(state.last_reconcile_metadata)
    lot_definition = getattr(lot_snapshot, "lot_definition", None)
    position_state = build_position_state_model(
        raw_qty_open=qty,
        metadata_raw=state.last_reconcile_metadata,
        raw_total_asset_qty=max(
            qty,
            float(lot_snapshot.raw_total_asset_qty),
            float(dust_context.raw_holdings.broker_qty),
        ),
        open_exposure_qty=float(lot_snapshot.raw_open_exposure_qty),
        dust_tracking_qty=float(lot_snapshot.dust_tracking_qty),
        open_lot_count=int(lot_snapshot.open_lot_count),
        dust_tracking_lot_count=int(lot_snapshot.dust_tracking_lot_count),
        reserved_exit_qty=reserved_exit_qty,
        internal_lot_size=(None if lot_definition is None else lot_definition.internal_lot_size),
        min_qty=(None if lot_definition is None else lot_definition.min_qty),
        qty_step=(None if lot_definition is None else lot_definition.qty_step),
        min_notional_krw=(None if lot_definition is None else lot_definition.min_notional_krw),
        max_qty_decimals=(None if lot_definition is None else lot_definition.max_qty_decimals),
    )
    normalized_exposure = position_state.normalized_exposure
    if normalized_exposure.terminal_state == "flat":
        return "flat"
    if normalized_exposure.has_executable_exposure:
        return f"open_exposure_qty={normalized_exposure.open_exposure_qty:.8f}"
    if normalized_exposure.has_dust_only_remainder:
        return f"dust_only_qty={normalized_exposure.dust_tracking_qty:.8f}"
    return f"non_executable_position_state={normalized_exposure.terminal_state}"
