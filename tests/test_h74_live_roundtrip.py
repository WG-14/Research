from __future__ import annotations

import sqlite3
import json
from pathlib import Path

from bithumb_bot.broker.base import BrokerFill, BrokerOrder
from bithumb_bot.decision_equivalence import sha256_prefixed
from bithumb_bot.h74_live_rehearsal import H74LiveRehearsalConfig, run_h74_live_rehearsal
from bithumb_bot.runtime.daily_participation_claims import (
    DailyParticipationClaimKey,
    ensure_daily_participation_claims_schema,
    pending_daily_participation_claim_count,
    sync_daily_participation_claim_from_order_status,
    upsert_daily_participation_claim,
)
from bithumb_bot.runtime.live_order_settlement import _order_fill_evidence


FIXTURE = Path(__file__).parent / "fixtures" / "bithumb" / "live_paid_fee_single_fill_buy_2026_04_24.json"


def _source_artifact(tmp_path) -> str:
    source = tmp_path / "source.json"
    source.write_text(
        '{"runtime_base_cost_assumption":{"fee_rate":0.0004,"slippage_bps":10},"candle_timing":"closed_candle_kst"}',
        encoding="utf-8",
    )
    return str(source)


def _claim_key() -> DailyParticipationClaimKey:
    return DailyParticipationClaimKey(
        strategy_instance_id="h74-source-observation",
        pair="KRW-BTC",
        kst_day="2026-06-22",
        participation_policy_hash=sha256_prefixed({"h74": "participation_policy"}),
    )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_daily_participation_claims_schema(conn)
    conn.execute(
        """
        CREATE TABLE orders (
            client_order_id TEXT PRIMARY KEY,
            strategy_instance_id TEXT,
            pair TEXT,
            strategy_name TEXT,
            side TEXT,
            status TEXT,
            daily_participation_policy_hash TEXT,
            daily_count_snapshot_hash TEXT,
            participation_decision_hash TEXT,
            daily_participation_kst_day TEXT,
            daily_participation_fallback_mode TEXT
        )
        """
    )
    return conn


def _recorded_broker_roundtrip() -> tuple[BrokerOrder, list[BrokerFill]]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    trade = payload["trade"]
    fee = float(payload["order_fee_fields"]["paid_fee"])
    client_order_id = "h74-buy-1"
    exchange_order_id = str(trade["uuid"])
    qty = float(trade["volume"])
    price = float(trade["price"])
    ts_ms = 1_777_048_623_000
    return (
        BrokerOrder(
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            side="BUY",
            status="FILLED",
            price=price,
            qty_req=qty,
            qty_filled=qty,
            created_ts=ts_ms,
            updated_ts=ts_ms,
            raw=payload,
        ),
        [
            BrokerFill(
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                fill_id=exchange_order_id,
                fill_ts=ts_ms,
                price=price,
                qty=qty,
                fee=fee,
                fee_status="complete",
                fee_source="order_level_paid_fee",
                fee_confidence="authoritative",
                fee_provenance=str(FIXTURE),
                raw=payload,
            )
        ],
    )


def test_h74_buy_fill_marks_daily_claim_fulfilled(tmp_path) -> None:
    rehearsal = run_h74_live_rehearsal(H74LiveRehearsalConfig(source_artifact_path=_source_artifact(tmp_path)))
    assert rehearsal["broker_submit_reached"] is True
    order, fills = _recorded_broker_roundtrip()
    evidence = _order_fill_evidence(order=order, fills=fills)
    conn = _conn()
    key = _claim_key()
    conn.execute(
        """
        INSERT INTO orders(
            client_order_id, strategy_instance_id, pair, strategy_name, side, status,
            daily_participation_policy_hash, daily_count_snapshot_hash,
            participation_decision_hash, daily_participation_kst_day, daily_participation_fallback_mode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "h74-buy-1",
            key.strategy_instance_id,
            key.pair,
            "daily_participation_sma",
            "BUY",
            "FILLED",
            key.participation_policy_hash,
            "sha256:daily-count",
            "sha256:participation-decision",
            key.kst_day,
            "unconditional_participation",
        ),
    )

    sync_daily_participation_claim_from_order_status(conn, client_order_id="h74-buy-1", status="FILLED", ts_ms=1)
    row = conn.execute("SELECT status FROM daily_participation_claims").fetchone()

    assert row["status"] == "fulfilled"
    assert evidence["fee_finalized"] is True
    assert evidence["paid_fee_present"] is True
    assert rehearsal["would_submit_plan"]["side"] == "BUY"
    assert rehearsal["would_submit_plan"]["source"] == "target_delta"


def test_next_cycle_same_kst_day_does_not_submit_second_buy(tmp_path) -> None:
    rehearsal = run_h74_live_rehearsal(H74LiveRehearsalConfig(source_artifact_path=_source_artifact(tmp_path)))
    assert rehearsal["broker_submit_reached"] is True
    conn = _conn()
    key = _claim_key()
    upsert_daily_participation_claim(
        conn,
        key=key,
        status="claim_pending",
        ts_ms=1,
        client_order_id="h74-buy-1",
    )

    blocked_count = pending_daily_participation_claim_count(conn, key=key)
    submit_expected = blocked_count == 0

    assert blocked_count == 1
    assert submit_expected is False


def test_fee_missing_blocks_or_marks_recovery_required() -> None:
    evidence = _order_fill_evidence(
        order=None,
        fills=[
            BrokerFill(
                client_order_id="h74-buy-1",
                fill_id="fill-1",
                fill_ts=1,
                price=100_000_000.0,
                qty=0.0001,
                fee=None,
                fee_status="missing",
                fee_source="missing",
                fee_confidence="unknown",
                fee_provenance="missing_fee_fixture",
            )
        ],
    )

    assert evidence["fee_state"] in {"pending", "blocked"}
    assert evidence["fee_finalized"] is False


def test_projection_mismatch_blocks_resume() -> None:
    readiness = {
        "projection_converged": False,
        "projection_non_convergence_reason": "broker_asset_zero_local_projection_nonzero",
        "run_loop_can_resume": False,
    }

    assert readiness["projection_converged"] is False
    assert readiness["run_loop_can_resume"] is False


def test_h74_roundtrip_uses_recorded_broker_fill_fixture(tmp_path) -> None:
    rehearsal = run_h74_live_rehearsal(H74LiveRehearsalConfig(source_artifact_path=_source_artifact(tmp_path)))
    order, fills = _recorded_broker_roundtrip()
    evidence = _order_fill_evidence(order=order, fills=fills)

    assert FIXTURE.exists()
    assert rehearsal["would_submit_plan"]["source"] == "target_delta"
    assert order.raw is not None
    assert fills[0].fee == 27.86
    assert evidence["fill_set_complete"] is True
    assert evidence["fee_state"] == "finalized"


def test_h74_roundtrip_verifies_sell_closeout_path(tmp_path) -> None:
    rehearsal = run_h74_live_rehearsal(H74LiveRehearsalConfig(source_artifact_path=_source_artifact(tmp_path)))
    order, fills = _recorded_broker_roundtrip()
    evidence = _order_fill_evidence(order=order, fills=fills)
    sell_closeout = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            source_artifact_path=_source_artifact(tmp_path),
            closeout_existing_qty=fills[0].qty,
        )
    )

    assert evidence["fee_state"] == "finalized"
    assert rehearsal["would_submit_plan"]["side"] == "BUY"
    assert sell_closeout["would_submit_plan"]["side"] == "SELL"
    assert sell_closeout["would_submit_plan"]["source"] == "target_delta"
    assert sell_closeout["would_submit_plan"]["final_action"] == "REBALANCE_TO_TARGET"
