from __future__ import annotations

import sqlite3
from typing import Any

from .canonical_decision import order_rules_snapshot_payload
from .core.sma_policy import PositionSnapshot
from .dust import build_dust_display_context, build_position_state_model
from .fee_authority import build_fee_authority_snapshot
from .lifecycle import OPEN_POSITION_STATE, summarize_position_lots, summarize_reserved_exit_qty
from .runtime_position_state_normalizer import load_last_reconcile_metadata
from .runtime_sma_context import safe_ratio
from .strategy.base import PositionContext


def load_sma_signal_rows(
    conn: sqlite3.Connection,
    *,
    pair: str,
    interval: str,
    through_ts_ms: int | None,
) -> list[sqlite3.Row | tuple[Any, ...]]:
    """Provider-owned SMA candle materialization from runtime DB schema."""
    columns = {
        str(item[0])
        for item in (conn.execute("SELECT * FROM candles LIMIT 0").description or ())
    }
    optional_columns = [
        name for name in ("high", "low", "volume") if name in columns
    ]
    select_columns = ["ts", "close", *optional_columns]
    query = f"SELECT {', '.join(select_columns)} FROM candles WHERE pair=? AND interval=?"
    params: list[object] = [pair, interval]
    if through_ts_ms is not None:
        query += " AND ts <= ?"
        params.append(int(through_ts_ms))
    query += " ORDER BY ts ASC"
    return conn.execute(query, tuple(params)).fetchall()


def latest_sma_signal_close(
    conn: sqlite3.Connection,
    *,
    pair: str,
    interval: str,
    through_ts_ms: int,
) -> float | None:
    try:
        row = conn.execute(
            """
            SELECT close
            FROM candles
            WHERE pair=? AND interval=? AND ts <= ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (pair, interval, int(through_ts_ms)),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None or row[0] is None:
        return None
    return float(row[0])


def load_sma_position_context(
    conn: sqlite3.Connection,
    *,
    pair: str,
    candle_ts: int,
    market_price: float,
    signal_context: dict[str, Any],
    slippage_bps: float,
    entry_edge_buffer_ratio: float,
) -> tuple[PositionContext, object, object, dict[str, object]]:
    """Provider-owned SMA position materialization from runtime DB schema."""
    dust_context = build_dust_display_context(load_last_reconcile_metadata(conn))
    from . import runtime_sma_snapshot_builder as snapshot_builder

    resolution = snapshot_builder.get_effective_order_rules(pair)
    rules = resolution.rules
    order_rules_snapshot = order_rules_snapshot_payload(resolution, pair=pair)
    fee_authority = build_fee_authority_snapshot(resolution)
    reserved_exit_qty = summarize_reserved_exit_qty(conn, pair=pair)
    try:
        row = conn.execute(
            """
            SELECT
                MIN(entry_ts) AS entry_ts,
                SUM(entry_price * qty_open) / NULLIF(SUM(qty_open), 0.0) AS avg_entry_price,
                SUM(qty_open) AS qty_open
            FROM open_position_lots
            WHERE pair=? AND position_state=? AND qty_open > 1e-12
              AND COALESCE(position_semantic_basis, '')='lot-native'
              AND COALESCE(executable_lot_count, 0) > 0
              AND COALESCE(dust_tracking_lot_count, 0) = 0
            """,
            (pair, OPEN_POSITION_STATE),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None

    if row is None or row[0] is None or row[2] is None:
        lot_snapshot = summarize_position_lots(conn, pair=pair)
        lot_definition = getattr(lot_snapshot, "lot_definition", None)
        metadata_residue_qty = 0.0
        if dust_context.raw_holdings.present:
            metadata_residue_qty = max(
                0.0,
                float(dust_context.raw_holdings.local_qty),
                float(dust_context.raw_holdings.broker_qty),
            )
        tracked_qty = max(float(lot_snapshot.raw_total_asset_qty), metadata_residue_qty)
        dust_tracking_qty = max(float(lot_snapshot.dust_tracking_qty), metadata_residue_qty)
        raw_qty_open = (
            tracked_qty
            if (
                tracked_qty > 1e-12
                and dust_context.classification.classification == "harmless_dust"
                and not dust_context.effective_flat_due_to_harmless_dust
            )
            else 0.0
        )
        position_state = build_position_state_model(
            raw_qty_open=raw_qty_open,
            metadata_raw=dust_context.classification,
            raw_total_asset_qty=tracked_qty,
            open_exposure_qty=0.0,
            dust_tracking_qty=dust_tracking_qty,
            reserved_exit_qty=reserved_exit_qty,
            open_lot_count=lot_snapshot.open_lot_count,
            dust_tracking_lot_count=lot_snapshot.dust_tracking_lot_count,
            internal_lot_size=(None if lot_definition is None else lot_definition.internal_lot_size),
            market_price=float(market_price),
            min_qty=(float(rules.min_qty) if lot_definition is None or lot_definition.min_qty is None else lot_definition.min_qty),
            qty_step=(float(rules.qty_step) if lot_definition is None or lot_definition.qty_step is None else lot_definition.qty_step),
            min_notional_krw=(
                float(rules.min_notional_krw)
                if lot_definition is None or lot_definition.min_notional_krw is None
                else lot_definition.min_notional_krw
            ),
            max_qty_decimals=(
                int(rules.max_qty_decimals)
                if lot_definition is None or lot_definition.max_qty_decimals is None
                else lot_definition.max_qty_decimals
            ),
            exit_fee_ratio=float(fee_authority.taker_ask_fee_rate),
            exit_slippage_bps=float(slippage_bps),
            exit_buffer_ratio=float(entry_edge_buffer_ratio),
        )
        exposure = position_state.normalized_exposure
        return (
            PositionContext(
                in_position=bool(exposure.normalized_exposure_active),
                qty_open=float(exposure.normalized_exposure_qty),
                recent_signal_context=dict(signal_context),
            ),
            exposure,
            position_state,
            order_rules_snapshot,
        )

    entry_ts = int(row[0])
    entry_price = float(row[1])
    tracked_open_qty = float(row[2])
    lot_snapshot = summarize_position_lots(conn, pair=pair)
    lot_definition = getattr(lot_snapshot, "lot_definition", None)
    position_state = build_position_state_model(
        raw_qty_open=tracked_open_qty,
        metadata_raw=dust_context.classification,
        raw_total_asset_qty=float(lot_snapshot.raw_total_asset_qty),
        open_exposure_qty=tracked_open_qty,
        dust_tracking_qty=float(lot_snapshot.dust_tracking_qty),
        reserved_exit_qty=reserved_exit_qty,
        open_lot_count=int(lot_snapshot.open_lot_count),
        dust_tracking_lot_count=int(lot_snapshot.dust_tracking_lot_count),
        internal_lot_size=(None if lot_definition is None else lot_definition.internal_lot_size),
        market_price=float(market_price),
        min_qty=(float(rules.min_qty) if lot_definition is None or lot_definition.min_qty is None else lot_definition.min_qty),
        qty_step=(float(rules.qty_step) if lot_definition is None or lot_definition.qty_step is None else lot_definition.qty_step),
        min_notional_krw=(
            float(rules.min_notional_krw)
            if lot_definition is None or lot_definition.min_notional_krw is None
            else lot_definition.min_notional_krw
        ),
        max_qty_decimals=(
            int(rules.max_qty_decimals)
            if lot_definition is None or lot_definition.max_qty_decimals is None
            else lot_definition.max_qty_decimals
        ),
        exit_fee_ratio=float(fee_authority.taker_ask_fee_rate),
        exit_slippage_bps=float(slippage_bps),
        exit_buffer_ratio=float(entry_edge_buffer_ratio),
    )
    exposure = position_state.normalized_exposure
    holding_time_sec = max(0.0, (int(candle_ts) - entry_ts) / 1000.0)
    unrealized_pnl = (float(market_price) - entry_price) * tracked_open_qty
    unrealized_pnl_ratio = safe_ratio(float(market_price) - entry_price, entry_price)

    return (
        PositionContext(
            in_position=bool(exposure.normalized_exposure_active),
            entry_ts=entry_ts,
            entry_price=entry_price,
            qty_open=tracked_open_qty,
            holding_time_sec=holding_time_sec,
            unrealized_pnl=unrealized_pnl,
            unrealized_pnl_ratio=unrealized_pnl_ratio,
            recent_signal_context=dict(signal_context),
        ),
        exposure,
        position_state,
        order_rules_snapshot,
    )


def policy_position_snapshot(
    *,
    position: PositionContext,
    exposure: object,
) -> PositionSnapshot:
    return PositionSnapshot(
        in_position=bool(position.in_position),
        entry_allowed=bool(exposure.entry_allowed),
        exit_allowed=bool(exposure.exit_allowed),
        entry_block_reason=str(exposure.entry_block_reason or ""),
        exit_block_reason=str(exposure.exit_block_reason or ""),
        terminal_state=str(exposure.terminal_state),
        entry_ts=position.entry_ts,
        entry_price=position.entry_price,
        qty_open=float(position.qty_open),
        holding_time_sec=float(position.holding_time_sec),
        unrealized_pnl=float(position.unrealized_pnl),
        unrealized_pnl_ratio=float(position.unrealized_pnl_ratio),
        raw_qty_open=float(exposure.raw_qty_open),
        raw_total_asset_qty=float(exposure.raw_total_asset_qty),
        open_lot_count=int(exposure.open_lot_count),
        dust_tracking_lot_count=int(exposure.dust_tracking_lot_count),
        reserved_exit_lot_count=int(exposure.reserved_exit_lot_count),
        sellable_executable_lot_count=int(exposure.sellable_executable_lot_count),
        dust_classification=str(exposure.dust_classification),
        dust_state=str(exposure.dust_state),
        effective_flat=bool(exposure.effective_flat),
        has_executable_exposure=bool(exposure.has_executable_exposure),
        has_any_position_residue=bool(exposure.has_any_position_residue),
        has_non_executable_residue=bool(exposure.has_non_executable_residue),
        has_dust_only_remainder=bool(exposure.has_dust_only_remainder),
    )
