from __future__ import annotations

import sqlite3

import pytest

from bithumb_bot.db_core import ensure_schema
from bithumb_bot.execution_service import ExecutionSubmitPlan
from bithumb_bot.oms import add_fill, create_order
from bithumb_bot.runtime.daily_participation_count_provider import build_runtime_daily_count_snapshot_from_sqlite
from bithumb_bot.runtime.lifecycle_artifacts import build_max_holding_exit_delay_evidence
from bithumb_bot.strategy.daily_participation_policy import DailyParticipationPolicyConfig


def _daily_extra() -> dict[str, object]:
    return {
        "strategy_name": "daily_participation_sma",
        "strategy_instance_id": "daily:a",
        "pair": "KRW-BTC",
        "daily_count_snapshot_hash": "sha256:count",
        "participation_policy_hash": "sha256:policy",
        "participation_decision_hash": "sha256:decision",
        "fallback_mode": "unconditional_participation",
        "entry_signal_source": "daily_participation_fallback",
        "fee_authority_hash": "sha256:fee",
        "price_protection_hash": "sha256:price",
    }


def _plan(extra: dict[str, object] | None = None) -> ExecutionSubmitPlan:
    return ExecutionSubmitPlan(
        side="BUY",
        source="strategy_position",
        authority="configured_strategy_order_size",
        final_action="ENTER_STRATEGY_POSITION",
        qty=0.001,
        notional_krw=100_000.0,
        target_exposure_krw=100_000.0,
        current_effective_exposure_krw=0.0,
        delta_krw=100_000.0,
        submit_expected=True,
        pre_submit_proof_status="not_required",
        block_reason="none",
        idempotency_key="daily-buy",
        extra_payload=extra or _daily_extra(),
    )


def test_daily_buy_submit_plan_contains_policy_decision_and_count_hashes() -> None:
    payload = _plan().as_final_payload()

    assert payload["daily_count_snapshot_hash"] == "sha256:count"
    assert payload["participation_policy_hash"] == "sha256:policy"
    assert payload["participation_decision_hash"] == "sha256:decision"
    assert payload["fee_authority_hash"] == "sha256:fee"
    assert payload["price_protection_hash"] == "sha256:price"


def test_live_dry_run_submit_plan_contains_daily_participation_fields() -> None:
    payload = _plan().as_final_payload(extra={"live_dry_run": True})

    assert payload["entry_signal_source"] == "daily_participation_fallback"
    assert payload["fallback_mode"] == "unconditional_participation"
    assert payload["daily_count_snapshot_hash"] == "sha256:count"
    assert payload["participation_policy_hash"] == "sha256:policy"


def test_daily_real_order_blocks_missing_daily_policy_hash() -> None:
    extra = _daily_extra()
    extra.pop("participation_policy_hash")

    with pytest.raises(ValueError, match="daily_participation_submit_evidence_missing:.*participation_policy_hash"):
        _plan(extra).as_final_payload()


def test_daily_real_order_blocks_missing_fee_authority_or_price_protection() -> None:
    extra = _daily_extra()
    extra.pop("fee_authority_hash")
    with pytest.raises(ValueError, match="fee_authority"):
        _plan(extra).as_final_payload()

    extra = _daily_extra()
    extra.pop("price_protection_hash")
    with pytest.raises(ValueError, match="price_protection"):
        _plan(extra).as_final_payload()


def test_live_execution_evidence_records_spread_and_slippage_limits() -> None:
    payload = _plan().as_final_payload(
        extra={
            "live_real_order": True,
            "price_protection_max_slippage_bps": 5.0,
            "submit_spread_bps": 2.5,
        }
    )

    assert payload["price_protection_max_slippage_bps"] == 5.0
    assert payload["submit_spread_bps"] == 2.5

    with pytest.raises(ValueError, match="price_protection_positive_max_slippage"):
        _plan().as_final_payload(extra={"live_real_order": True, "price_protection_max_slippage_bps": 0.0})


def test_max_holding_exit_delay_is_reported() -> None:
    evidence = build_max_holding_exit_delay_evidence(
        entry_fill_ts_ms=1_700_000_000_000,
        max_holding_sec=60.0,
        actual_sell_fill_ts_ms=1_700_000_075_000,
    )

    assert evidence["target_exit_ts_ms"] == 1_700_000_060_000
    assert evidence["max_holding_exit_delay_ms"] == 15_000
    assert str(evidence["max_holding_exit_delay_evidence_hash"]).startswith("sha256:")


def test_partial_fill_does_not_mark_full_daily_fulfillment_without_policy() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    config = DailyParticipationPolicyConfig(
        enabled=True,
        timezone="Asia/Seoul",
        count_basis="filled",
        window_start_hour=0,
        window_end_hour=24,
        buy_fraction=0.05,
        max_order_krw=10000.0,
    )
    create_order(
        client_order_id="daily-partial",
        symbol="KRW-BTC",
        side="BUY",
        qty_req=1.0,
        price=100.0,
        strategy_name="daily_participation_sma",
        strategy_instance_id="daily:a",
        daily_participation_policy_hash=config.policy_hash(),
        daily_count_snapshot_hash="sha256:count",
        participation_decision_hash="sha256:decision",
        daily_participation_kst_day="2023-12-31",
        daily_participation_fallback_mode="unconditional_participation",
        status="PARTIAL",
        ts_ms=1_704_031_200_000,
        conn=conn,
    )
    add_fill(
        client_order_id="daily-partial",
        fill_id="partial-fill",
        fill_ts=1_704_031_260_000,
        price=100.0,
        qty=0.25,
        fee=0.1,
        conn=conn,
    )

    snapshot = build_runtime_daily_count_snapshot_from_sqlite(
        conn=conn,
        config=config,
        decision_ts=1_704_031_300_000,
        pair="KRW-BTC",
        strategy_instance_id="daily:a",
    )
    opt_in_snapshot = build_runtime_daily_count_snapshot_from_sqlite(
        conn=conn,
        config=DailyParticipationPolicyConfig(
            enabled=True,
            timezone="Asia/Seoul",
            count_basis="filled",
            window_start_hour=0,
            window_end_hour=24,
            buy_fraction=0.05,
            max_order_krw=10000.0,
            partial_fill_counts_as_fulfilled=True,
        ),
        decision_ts=1_704_031_300_000,
        pair="KRW-BTC",
        strategy_instance_id="daily:a",
    )

    assert snapshot.count_for_kst_day == 0
    assert snapshot.pending_claim_count == 1
    assert opt_in_snapshot.count_for_kst_day == 1
