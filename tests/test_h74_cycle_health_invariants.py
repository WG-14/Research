from __future__ import annotations

import sqlite3

import pytest

from bithumb_bot.db_core import ensure_schema, init_portfolio, set_portfolio
from bithumb_bot.execution import apply_fill_and_trade, record_order_if_missing
from bithumb_bot.h74_cycle_state import ensure_h74_cycle_schema, upsert_h74_cycle_fill
from bithumb_bot.h74_position_ownership import h74_position_ownership_contract_from_payload
from bithumb_bot.runtime_readiness import compute_runtime_readiness_snapshot


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    ensure_h74_cycle_schema(conn)
    init_portfolio(conn)
    return conn


def _h74_order(conn: sqlite3.Connection, *, client_order_id: str = "h74-buy") -> None:
    contract_hash = h74_position_ownership_contract_from_payload(
        {
            "cycle_id": "cycle-1",
            "h74_cycle_id": "cycle-1",
            "authority_hash": "sha256:a",
            "strategy_instance_id": "h74-source-observation",
            "probe_run_id": "probe-run-1",
            "pair": "KRW-BTC",
            "entry_side": "BUY",
            "entry_plan_id": "h74-buy",
            "position_mode": "fixed_fill_qty_until_exit",
            "hold_policy": "hold_acquired_fill_qty_until_max_holding_exit",
        }
    ).contract_hash
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
        h74_position_ownership_contract_hash=contract_hash,
        probe_run_id="probe-run-1",
        status="FILLED",
    )


def _h74_sell_order(conn: sqlite3.Connection, *, client_order_id: str = "h74-sell") -> None:
    contract_hash = h74_position_ownership_contract_from_payload(
        {
            "cycle_id": "cycle-1",
            "h74_cycle_id": "cycle-1",
            "authority_hash": "sha256:a",
            "strategy_instance_id": "h74-source-observation",
            "probe_run_id": "probe-run-1",
            "pair": "KRW-BTC",
            "entry_side": "BUY",
            "entry_plan_id": "h74-buy",
            "position_mode": "fixed_fill_qty_until_exit",
            "hold_policy": "hold_acquired_fill_qty_until_max_holding_exit",
        }
    ).contract_hash
    record_order_if_missing(
        conn,
        client_order_id=client_order_id,
        side="SELL",
        qty_req=0.0008,
        price=100_000_000.0,
        strategy_name="daily_participation_sma",
        strategy_instance_id="h74-source-observation",
        cycle_id="cycle-1",
        authority_hash="sha256:a",
        h74_position_ownership_contract_hash=contract_hash,
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


def test_h74_sell_filled_updates_cycle_sold_qty() -> None:
    conn = _conn()
    _h74_order(conn)
    result = apply_fill_and_trade(
        conn,
        client_order_id="h74-buy",
        side="BUY",
        fill_id="buy-fill",
        fill_ts=1,
        price=100_000_000.0,
        qty=0.0008,
        fee=32.0,
        strategy_name="daily_participation_sma",
        pair="KRW-BTC",
    )
    assert result is not None
    _h74_sell_order(conn)
    sell_result = apply_fill_and_trade(
        conn,
        client_order_id="h74-sell",
        side="SELL",
        fill_id="sell-fill",
        fill_ts=2,
        price=100_000_000.0,
        qty=0.0003,
        fee=12.0,
        strategy_name="daily_participation_sma",
        pair="KRW-BTC",
    )

    row = conn.execute("SELECT sold_qty, state FROM h74_cycle_state WHERE cycle_id='cycle-1'").fetchone()
    data = compute_runtime_readiness_snapshot(conn).as_dict()

    assert sell_result is not None
    assert row["sold_qty"] == pytest.approx(0.0003)
    assert row["state"] == "HOLDING"
    assert "h74_cycle_sold_qty_mismatch" not in data["resume_blockers"]


def test_h74_sell_fill_closes_cycle_when_remaining_zero() -> None:
    conn = _conn()
    _h74_order(conn)
    apply_fill_and_trade(
        conn,
        client_order_id="h74-buy",
        side="BUY",
        fill_id="buy-fill",
        fill_ts=1,
        price=100_000_000.0,
        qty=0.0008,
        fee=32.0,
        strategy_name="daily_participation_sma",
        pair="KRW-BTC",
    )
    _h74_sell_order(conn)
    apply_fill_and_trade(
        conn,
        client_order_id="h74-sell",
        side="SELL",
        fill_id="sell-fill",
        fill_ts=2,
        price=100_000_000.0,
        qty=0.0008,
        fee=32.0,
        strategy_name="daily_participation_sma",
        pair="KRW-BTC",
    )

    row = conn.execute("SELECT sold_qty, state FROM h74_cycle_state WHERE cycle_id='cycle-1'").fetchone()

    assert row["sold_qty"] == pytest.approx(0.0008)
    assert row["state"] == "CLOSED"


def test_h74_closed_cycle_requires_flat_portfolio_and_accounting() -> None:
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
    set_portfolio(conn, cash_krw=1_000_000.0, asset_qty=0.0001)

    data = compute_runtime_readiness_snapshot(conn).as_dict()

    assert "h74_closed_cycle_not_flat" in data["resume_blockers"]


def test_h74_cycle_mismatch_blocks_new_entry() -> None:
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

    readiness = compute_runtime_readiness_snapshot(conn)

    assert "h74_cycle_qty_mismatch" in readiness.resume_blockers
    assert readiness.new_entry_allowed is False


def test_h74_trade_row_missing_for_h74_fill_is_health_blocker() -> None:
    conn = _conn()
    _h74_order(conn)
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
    conn.execute(
        """
        INSERT INTO fills(client_order_id, fill_id, fill_ts, price, qty, fee)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("h74-buy", "fill-1", 1, 100_000_000.0, 0.0008, 32.0),
    )

    readiness = compute_runtime_readiness_snapshot(conn)

    assert "h74_trade_missing" in readiness.resume_blockers
    assert readiness.new_entry_allowed is False


def test_h74_sell_fill_without_lifecycle_is_health_blocker() -> None:
    conn = _conn()
    _h74_order(conn)
    apply_fill_and_trade(
        conn,
        client_order_id="h74-buy",
        side="BUY",
        fill_id="buy-fill",
        fill_ts=1,
        price=100_000_000.0,
        qty=0.0008,
        fee=32.0,
        strategy_name="daily_participation_sma",
        pair="KRW-BTC",
    )
    _h74_sell_order(conn)
    apply_fill_and_trade(
        conn,
        client_order_id="h74-sell",
        side="SELL",
        fill_id="sell-fill",
        fill_ts=2,
        price=100_000_000.0,
        qty=0.0008,
        fee=32.0,
        strategy_name="daily_participation_sma",
        pair="KRW-BTC",
    )
    conn.execute("DELETE FROM trade_lifecycles WHERE exit_client_order_id=?", ("h74-sell",))

    readiness = compute_runtime_readiness_snapshot(conn)

    assert "h74_lifecycle_missing" in readiness.resume_blockers
    assert readiness.new_entry_allowed is False


def test_h74_open_lot_qty_mismatch_with_cycle_remaining_is_health_blocker() -> None:
    conn = _conn()
    _h74_order(conn)
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
    conn.execute(
        """
        INSERT INTO open_position_lots(
            pair, entry_trade_id, entry_client_order_id, entry_ts, entry_price,
            qty_open, executable_lot_count, position_semantic_basis,
            position_state, strategy_name, cycle_id, authority_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "KRW-BTC",
            1,
            "h74-buy",
            1,
            100_000_000.0,
            0.0003,
            3,
            "lot-native",
            "open_exposure",
            "daily_participation_sma",
            "cycle-1",
            "sha256:a",
        ),
    )

    readiness = compute_runtime_readiness_snapshot(conn)

    assert "h74_open_lot_cycle_qty_mismatch" in readiness.resume_blockers
    assert readiness.new_entry_allowed is False


def test_h74_fill_apply_cycle_persistence_failure_is_operator_actionable(monkeypatch) -> None:
    conn = _conn()
    _h74_order(conn)

    def _fail_upsert(*_args, **_kwargs):
        raise RuntimeError("forced_cycle_persistence_failure")

    monkeypatch.setattr("bithumb_bot.h74_cycle_state.upsert_h74_cycle_fill", _fail_upsert)

    with pytest.raises(RuntimeError, match="forced_cycle_persistence_failure"):
        apply_fill_and_trade(
            conn,
            client_order_id="h74-buy",
            side="BUY",
            fill_id="buy-fill",
            fill_ts=1,
            price=100_000_000.0,
            qty=0.0008,
            fee=32.0,
            strategy_name="daily_participation_sma",
            pair="KRW-BTC",
        )

    assert conn.execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM h74_cycle_state").fetchone()["n"] == 0


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
        h74_position_ownership_contract_hash=h74_position_ownership_contract_from_payload(
            {
                "cycle_id": "cycle-1",
                "h74_cycle_id": "cycle-1",
                "authority_hash": "sha256:a",
                "strategy_instance_id": "h74-source-observation",
                "probe_run_id": "probe-run-1",
                "pair": "KRW-BTC",
                "entry_side": "BUY",
                "entry_plan_id": "h74-buy",
                "position_mode": "fixed_fill_qty_until_exit",
                "hold_policy": "hold_acquired_fill_qty_until_max_holding_exit",
            }
        ).contract_hash,
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
