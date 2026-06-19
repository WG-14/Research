from __future__ import annotations

import sqlite3

from bithumb_bot.h74_observation_report import build_h74_observation_report


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE orders (
            client_order_id TEXT,
            strategy_name TEXT,
            side TEXT,
            status TEXT,
            exit_rule_name TEXT,
            decision_reason TEXT,
            last_error TEXT
        )
        """
    )
    conn.execute("CREATE TABLE fills (client_order_id TEXT, fee REAL)")
    conn.execute("CREATE TABLE daily_participation_claims (status TEXT)")
    return conn


def test_h74_observation_report_includes_daily_counts() -> None:
    conn = _conn()
    conn.execute("INSERT INTO orders VALUES ('b1','daily_participation_sma','BUY','FILLED','','','')")
    conn.execute("INSERT INTO fills VALUES ('b1', 10.0)")

    report = build_h74_observation_report(conn=conn, days=7)

    assert report["daily_buy_intent_count"] == 1
    assert report["daily_buy_filled_count"] == 1
    assert "duplicate_entry_block_count" in report


def test_h74_observation_report_distinguishes_strategy_exit_from_manual_flatten() -> None:
    conn = _conn()
    conn.execute("INSERT INTO orders VALUES ('s1','daily_participation_sma','SELL','FILLED','max_holding_time','','')")
    conn.execute("INSERT INTO orders VALUES ('s2','daily_participation_sma','SELL','FILLED','','manual_flatten','')")

    report = build_h74_observation_report(conn=conn, days=7)

    assert report["max_holding_exit_filled_count"] == 1
    assert report["manual_intervention_count"] == 1


def test_h74_observation_report_flags_duplicate_entry() -> None:
    conn = _conn()
    for index in range(8):
        conn.execute(
            "INSERT INTO orders VALUES (?, 'daily_participation_sma','BUY','FILLED','','','')",
            (f"b{index}",),
        )
        conn.execute("INSERT INTO fills VALUES (?, 0.0)", (f"b{index}",))

    report = build_h74_observation_report(conn=conn, days=7)

    assert report["duplicate_entry_block_count"] == 1


def test_h74_observation_report_includes_broker_local_mismatch_count() -> None:
    conn = _conn()
    conn.execute("INSERT INTO orders VALUES ('x','daily_participation_sma','BUY','FAILED','','','broker/local mismatch')")

    report = build_h74_observation_report(conn=conn, days=7)

    assert report["broker_local_mismatch_count"] == 1


def test_h74_observation_report_does_not_use_backtest_pnl_as_live_pnl() -> None:
    report = build_h74_observation_report(days=7)

    assert report["source_backtest_pnl"] is None
    assert report["live_observed_pnl"] is None
