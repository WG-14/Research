from __future__ import annotations

import pytest

from bithumb_bot.execution_service import ExecutionSubmitPlan


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
