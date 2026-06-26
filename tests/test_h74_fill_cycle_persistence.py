from __future__ import annotations

import sqlite3

import pytest

from bithumb_bot.db_core import ensure_schema, init_portfolio
from bithumb_bot.execution import apply_fill_and_trade, record_order_if_missing
from bithumb_bot.h74_cycle_state import ensure_h74_cycle_schema, load_h74_cycle_inventory


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_h74_cycle_schema(conn)
    init_portfolio(conn)
    return conn


def _order(conn: sqlite3.Connection, *, cycle_id: str | None = "cycle-1") -> None:
    record_order_if_missing(
        conn,
        client_order_id="h74-buy",
        side="BUY",
        qty_req=0.0008,
        price=100_000_000.0,
        strategy_name="daily_participation_sma",
        strategy_instance_id="h74-source-observation",
        cycle_id=cycle_id,
        authority_hash="sha256:a",
        probe_run_id="probe-run-1",
        status="NEW",
    )


def test_h74_buy_fill_creates_cycle_state() -> None:
    conn = _conn()
    _order(conn)

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

    inventory = load_h74_cycle_inventory(conn, cycle_id="cycle-1")
    assert result is not None
    assert result["h74_cycle_ownership_created"] == 1
    assert result["h74_exit_authority_ready"] == 1
    assert inventory is not None
    assert inventory.acquired_qty == pytest.approx(0.0008)
    assert conn.execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"] == 1


def test_h74_partial_buy_fills_accumulate_same_cycle() -> None:
    conn = _conn()
    _order(conn)

    for fill_id, qty, ts in (("fill-1", 0.0003, 1), ("fill-2", 0.0005, 2)):
        apply_fill_and_trade(
            conn,
            client_order_id="h74-buy",
            side="BUY",
            fill_id=fill_id,
            fill_ts=ts,
            price=100_000_000.0,
            qty=qty,
            fee=12.0,
            strategy_name="daily_participation_sma",
            pair="KRW-BTC",
        )

    inventory = load_h74_cycle_inventory(conn, cycle_id="cycle-1")
    assert inventory is not None
    assert inventory.acquired_qty == pytest.approx(0.0008)
    assert inventory.remaining_cycle_qty == pytest.approx(0.0008)
    assert conn.execute("SELECT COUNT(*) AS n FROM h74_cycle_state").fetchone()["n"] == 1


def test_h74_buy_fill_without_cycle_id_fails_closed() -> None:
    conn = _conn()
    _order(conn, cycle_id=None)

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

    assert conn.execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM h74_cycle_state").fetchone()["n"] == 0
