from __future__ import annotations

import pytest

from bithumb_bot import runtime_state
from bithumb_bot.config import settings
from bithumb_bot.db_core import ensure_db, init_portfolio, record_broker_fill_observation
from bithumb_bot.execution import record_order_if_missing
from bithumb_bot.h74_cycle_state import upsert_h74_cycle_fill
from bithumb_bot.live_pipeline_smoke_preflight import (
    readiness_from_snapshot,
    validate_live_pipeline_smoke_step_readiness,
)
from bithumb_bot.runtime_readiness import compute_runtime_readiness_snapshot


@pytest.fixture
def readiness_db(tmp_path, monkeypatch):
    original_db_path = settings.DB_PATH
    original_mode = settings.MODE
    original_pair = settings.PAIR
    db_path = tmp_path / "runtime_readiness.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("MODE", "paper")
    object.__setattr__(settings, "DB_PATH", str(db_path))
    object.__setattr__(settings, "MODE", "paper")
    object.__setattr__(settings, "PAIR", "KRW-BTC")
    conn = ensure_db(str(db_path))
    init_portfolio(conn)
    conn.commit()
    runtime_state.record_reconcile_result(success=True, reason_code="ok", metadata={}, now_epoch_sec=1.0)
    try:
        yield conn
    finally:
        conn.close()
        runtime_state.record_reconcile_result(success=True, reason_code="ok", metadata={}, now_epoch_sec=1.0)
        object.__setattr__(settings, "DB_PATH", original_db_path)
        object.__setattr__(settings, "MODE", original_mode)
        object.__setattr__(settings, "PAIR", original_pair)


def _record_fee_gap_metadata() -> None:
    runtime_state.record_reconcile_result(
        success=True,
        reason_code="ok",
        metadata={
            "fee_gap_recovery_required": 1,
            "material_zero_fee_fill_count": 1,
            "material_zero_fee_fill_latest_ts": 1_700_000_000_000,
            "fee_gap_adjustment_count": 1,
            "fee_gap_adjustment_total_krw": 100.0,
            "fee_gap_adjustment_latest_event_ts": 1_700_000_000_100,
            "external_cash_adjustment_reason": "reconcile_fee_gap_cash_drift",
            "broker_asset_qty": 0.0,
            "broker_asset_available": 0.0,
            "broker_asset_locked": 0.0,
            "balance_source_base_currency": "BTC",
            "balance_source_quote_currency": "KRW",
            "balance_observed_ts_ms": 1_700_000_000_200,
            "balance_source_stale": False,
            "balance_source": "test_fixture",
        },
        now_epoch_sec=2.0,
    )


def test_runtime_readiness_separates_active_fill_blocker_from_fee_gap_closeout_policy(readiness_db):
    _record_fee_gap_metadata()

    snapshot = compute_runtime_readiness_snapshot(readiness_db)
    smoke_readiness = readiness_from_snapshot(snapshot)

    assert snapshot.fee_gap_closeout_blocking is True
    assert snapshot.active_fill_accounting_blocker is False
    assert snapshot.active_fee_accounting_blocker is False
    assert snapshot.new_entry_fee_blocker is False
    assert smoke_readiness.new_entry_fee_blocker is False
    validate_live_pipeline_smoke_step_readiness(smoke_readiness, expected_side="BUY")


def test_fee_gap_closeout_blocking_does_not_set_new_entry_fee_blocker_when_active_fee_clear(
    readiness_db,
):
    _record_fee_gap_metadata()

    snapshot = compute_runtime_readiness_snapshot(readiness_db)
    data = snapshot.as_dict()

    assert data["fee_gap_closeout_blocking"] is True
    assert data["fee_pending_count"] == 0
    assert data["broker_fill_latest_unresolved_fee_pending_count"] == 0
    assert data["fill_accounting_active_issue_count"] == 0
    assert data["new_entry_fee_blocker"] is False


def test_active_fee_accounting_blocker_reasons_identify_source(readiness_db):
    record_order_if_missing(
        readiness_db,
        client_order_id="runtime-readiness-fee-pending",
        side="BUY",
        qty_req=0.001,
        price=100_000_000.0,
        ts_ms=1_700_000_000_000,
        status="NEW",
    )
    record_broker_fill_observation(
        readiness_db,
        event_ts=1_700_000_000_060,
        client_order_id="runtime-readiness-fee-pending",
        exchange_order_id="ex-runtime-readiness-fee-pending",
        fill_id="runtime-readiness-fill-1",
        fill_ts=1_700_000_000_050,
        side="BUY",
        price=100_000_000.0,
        qty=0.001,
        fee=26.86,
        fee_status="order_level_candidate",
        accounting_status="fee_pending",
        source="test_runtime_readiness",
        parse_warnings="order_level_fee_candidate",
        raw_payload={"fixture": "runtime_readiness_fee_pending"},
    )
    readiness_db.commit()

    snapshot = compute_runtime_readiness_snapshot(readiness_db)
    data = snapshot.as_dict()
    smoke_readiness = readiness_from_snapshot(snapshot)

    assert data["active_fill_accounting_blocker"] is True
    assert data["new_entry_fee_blocker"] is True
    assert "unapplied_principal_pending_count" in data["active_fill_accounting_blocker_reasons"]
    assert smoke_readiness.new_entry_fee_blocker is True


def test_h74_cross_table_invariant_blocks_resume(readiness_db):
    record_order_if_missing(
        readiness_db,
        client_order_id="h74-buy",
        side="BUY",
        qty_req=0.0008,
        price=100_000_000.0,
        strategy_name="daily_participation_sma",
        strategy_instance_id="h74-source-observation",
        cycle_id="cycle-1",
        authority_hash="sha256:a",
        probe_run_id="probe-run-1",
        ts_ms=1,
        status="FILLED",
    )
    readiness_db.execute(
        """
        INSERT INTO fills(client_order_id, fill_id, fill_ts, price, qty, fee)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("h74-buy", "fill-1", 1, 100_000_000.0, 0.0008, 32.0),
    )
    upsert_h74_cycle_fill(
        readiness_db,
        cycle_id="cycle-1",
        authority_hash="sha256:a",
        strategy_instance_id="h74-source-observation",
        pair="KRW-BTC",
        side="BUY",
        qty=0.0008,
        client_order_id="h74-buy",
        fill_ts=1,
    )

    snapshot = compute_runtime_readiness_snapshot(readiness_db)
    data = snapshot.as_dict()

    assert "h74_trade_missing" in data["resume_blockers"]
    assert data["resume_ready"] is False
    assert data["new_entry_allowed"] is False


def test_health_reports_h74_cycle_schema_present(readiness_db):
    snapshot = compute_runtime_readiness_snapshot(readiness_db)
    data = snapshot.as_dict()

    assert "h74_cycle_schema_missing" not in data["resume_blockers"]


def test_health_reports_h74_cycle_schema_missing(readiness_db):
    readiness_db.execute("DROP TABLE h74_cycle_state")
    readiness_db.commit()

    snapshot = compute_runtime_readiness_snapshot(readiness_db)
    data = snapshot.as_dict()

    assert "h74_cycle_schema_missing" in data["resume_blockers"]
    assert data["run_loop_can_resume"] is False
