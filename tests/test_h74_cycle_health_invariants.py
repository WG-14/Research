from __future__ import annotations

import sqlite3

import pytest

from bithumb_bot.db_core import ensure_schema, init_portfolio
from bithumb_bot.execution import apply_fill_and_trade, record_order_if_missing
from bithumb_bot.h74_cycle_state import ensure_h74_cycle_schema, upsert_h74_cycle_fill
from bithumb_bot.runtime_readiness import compute_runtime_readiness_snapshot


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_h74_cycle_schema(conn)
    init_portfolio(conn)
    return conn


def _h74_order(conn: sqlite3.Connection, *, client_order_id: str = "h74-buy") -> None:
    record_order_if_missing(
        conn,
        client_order_id=client_order_id,
        side="BUY",
        qty_req=0.0008,
        price=100_000_000.0,
        strategy_name="daily_participation_sma",
        strategy_instance_id="h74-source-observation",
        cycle_id="cycle-1",
        authority_hash="sha256:a",
        probe_run_id="probe-run-1",
        status="FILLED",
    )


def test_h74_buy_filled_order_without_cycle_state_is_health_blocker() -> None:
    conn = _conn()
    _h74_order(conn)

    snapshot = compute_runtime_readiness_snapshot(conn)
    data = snapshot.as_dict()

    assert "h74_cycle_ownership_incomplete" in data["resume_blockers"]


def test_h74_cycle_state_qty_mismatch_is_health_blocker() -> None:
    conn = _conn()
    _h74_order(conn)
    conn.execute(
        "INSERT INTO fills(client_order_id, fill_id, fill_ts, price, qty, fee) VALUES (?, ?, ?, ?, ?, ?)",
        ("h74-buy", "fill-1", 1, 100_000_000.0, 0.0008, 32.0),
    )
    upsert_h74_cycle_fill(
        conn,
        cycle_id="cycle-1",
        authority_hash="sha256:a",
        strategy_instance_id="h74-source-observation",
        pair="KRW-BTC",
        side="BUY",
        qty=0.0003,
        client_order_id="h74-buy",
        fill_ts=1,
    )

    data = compute_runtime_readiness_snapshot(conn).as_dict()

    assert "h74_cycle_qty_mismatch" in data["resume_blockers"]


def test_h74_closed_cycle_and_flat_portfolio_is_clean() -> None:
    conn = _conn()
    upsert_h74_cycle_fill(
        conn,
        cycle_id="cycle-1",
        authority_hash="sha256:a",
        strategy_instance_id="h74-source-observation",
        pair="KRW-BTC",
        side="BUY",
        qty=0.0008,
        client_order_id="h74-buy",
        fill_ts=1,
    )
    upsert_h74_cycle_fill(
        conn,
        cycle_id="cycle-1",
        authority_hash="sha256:a",
        strategy_instance_id="h74-source-observation",
        pair="KRW-BTC",
        side="SELL",
        qty=0.0008,
        client_order_id="h74-sell",
        fill_ts=2,
    )

    data = compute_runtime_readiness_snapshot(conn).as_dict()

    assert "h74_cycle_ownership_incomplete" not in data["resume_blockers"]
    assert "h74_cycle_qty_mismatch" not in data["resume_blockers"]


def test_h74_buy_fill_reports_exit_authority_ready() -> None:
    conn = _conn()
    record_order_if_missing(
        conn,
        client_order_id="h74-buy",
        side="BUY",
        qty_req=0.0008,
        price=100_000_000.0,
        strategy_name="daily_participation_sma",
        strategy_instance_id="h74-source-observation",
        cycle_id="cycle-1",
        authority_hash="sha256:a",
        probe_run_id="probe-run-1",
        status="NEW",
    )

    result = apply_fill_and_trade(
        conn,
        client_order_id="h74-buy",
        side="BUY",
        fill_id="fill-1",
        fill_ts=1,
        price=100_000_000.0,
        qty=0.0008,
        fee=32.0,
        strategy_name="daily_participation_sma",
        pair="KRW-BTC",
    )

    assert result is not None
    assert result["h74_exit_authority_ready"] == 1
    assert result["h74_remaining_cycle_qty"] == pytest.approx(0.0008)


def test_h74_buy_fill_reports_exit_authority_not_ready_when_cycle_missing() -> None:
    conn = _conn()
    record_order_if_missing(
        conn,
        client_order_id="h74-buy",
        side="BUY",
        qty_req=0.0008,
        price=100_000_000.0,
        strategy_name="daily_participation_sma",
        strategy_instance_id="h74-source-observation",
        cycle_id=None,
        authority_hash="sha256:a",
        probe_run_id="probe-run-1",
        status="NEW",
    )

    with pytest.raises(RuntimeError, match="h74_cycle_ownership_incomplete"):
        apply_fill_and_trade(
            conn,
            client_order_id="h74-buy",
            side="BUY",
            fill_id="fill-1",
            fill_ts=1,
            price=100_000_000.0,
            qty=0.0008,
            fee=32.0,
            strategy_name="daily_participation_sma",
            pair="KRW-BTC",
        )
