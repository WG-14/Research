from __future__ import annotations

import pytest

from bithumb_bot.core.sma_policy import EntryExecutionIntent, PositionSnapshot, StrategyDecisionV2
from bithumb_bot.execution_service import ExecutionSubmitPlan
from bithumb_bot.research.backtest_kernel import (
    _research_execution_plan_bundle,
    execution_submit_plan_to_research_request,
)


def _typed_decision(*, raw_signal: str = "BUY", final_signal: str = "BUY") -> StrategyDecisionV2:
    return StrategyDecisionV2(
        strategy_name="sma_with_filter",
        raw_signal=raw_signal,
        raw_reason="typed_raw",
        entry_signal=final_signal,
        entry_reason="typed_entry",
        exit_signal="HOLD",
        exit_reason="no_exit",
        final_signal=final_signal,
        final_reason="typed_final",
        blocked_filters=(),
        entry_blocked=False,
        entry_block_reason=None,
        exit_rule=None,
        exit_evaluations=(),
        protective_exit_overrode_entry=False,
        exit_filter_suppression_prevented=False,
        position_snapshot=PositionSnapshot(in_position=False, entry_allowed=True, exit_allowed=False),
        execution_intent=EntryExecutionIntent(
            side="BUY",
            intent="enter",
            pair="KRW-BTC",
            requires_execution_sizing=True,
            budget_fraction_of_cash=0.5,
            max_budget_krw=100_000.0,
        ),
        entry_decision=object(),  # type: ignore[arg-type]
        trace={},
        policy_hash="sha256:pure",
        policy_contract_hash="sha256:contract",
        policy_input_hash="sha256:input",
        policy_decision_hash="sha256:decision",
    )


def _plan(
    *,
    side: str,
    qty: float | None,
    notional_krw: float | None,
    submit_expected: bool = True,
    block_reason: str = "none",
) -> ExecutionSubmitPlan:
    return ExecutionSubmitPlan(
        side=side,
        source="research_backtest",
        authority="strategy_execution_intent",
        final_action="ENTER_STRATEGY_POSITION" if side == "BUY" else "EXIT_STRATEGY_POSITION",
        qty=qty,
        notional_krw=notional_krw,
        target_exposure_krw=notional_krw,
        current_effective_exposure_krw=0.0,
        delta_krw=notional_krw,
        submit_expected=submit_expected,
        pre_submit_proof_status="not_required",
        block_reason=block_reason,
        idempotency_key=None,
    )


def _request(plan: ExecutionSubmitPlan):
    return execution_submit_plan_to_research_request(
        submit_plan=plan,
        signal_ts=100,
        decision_ts=200,
        reference_price=10.0,
        fee_rate=0.001,
        timing_fields={"submit_ts_assumption": 201},
        depth_fields={"depth_available": False},
    )


def test_buy_submit_plan_produces_request_from_plan_notional() -> None:
    request = _request(_plan(side="BUY", qty=999.0, notional_krw=12345.0))

    assert request is not None
    assert request.side == "BUY"
    assert request.requested_notional == 12345.0
    assert request.requested_qty == 999.0


def test_sell_submit_plan_produces_request_from_plan_qty() -> None:
    request = _request(_plan(side="SELL", qty=0.25, notional_krw=2500.0))

    assert request is not None
    assert request.side == "SELL"
    assert request.requested_qty == 0.25
    assert request.requested_notional == 2500.0


def test_submit_not_expected_produces_no_research_fill_request() -> None:
    request = _request(
        _plan(
            side="BUY",
            qty=None,
            notional_krw=None,
            submit_expected=False,
            block_reason="research_zero_buy_notional",
        )
    )

    assert request is None


def test_research_backtest_bundle_blocks_zero_size_before_request() -> None:
    bundle = _research_execution_plan_bundle(
        side="BUY",
        cash=0.0,
        buy_fraction=1.0,
        sellable_qty=0.0,
        reference_price=10.0,
        policy_decision=None,
    )

    assert bundle.status == "BLOCKED"
    assert bundle.reason_code == "research_zero_buy_notional"
    assert bundle.submit_plan is not None
    assert bundle.submit_plan.submit_expected is False
    assert execution_submit_plan_to_research_request(
        submit_plan=bundle.submit_plan,
        signal_ts=100,
        decision_ts=200,
        reference_price=10.0,
        fee_rate=0.001,
        timing_fields={},
        depth_fields={},
    ) is None


def test_research_backtest_bundle_blocks_hold_without_submit_plan() -> None:
    bundle = _research_execution_plan_bundle(
        side="HOLD",
        cash=1_000_000.0,
        buy_fraction=1.0,
        sellable_qty=0.0,
        reference_price=10.0,
        policy_decision=None,
        block_reason="strategy_hold",
    )

    assert bundle.status == "BLOCKED"
    assert bundle.reason_code == "strategy_hold"
    assert bundle.submit_plan is None


def test_malformed_submit_plan_fails_closed_before_research_request() -> None:
    with pytest.raises(ValueError, match="research_submit_plan_not_typed"):
        execution_submit_plan_to_research_request(
            submit_plan={"side": "BUY"},  # type: ignore[arg-type]
            signal_ts=100,
            decision_ts=200,
            reference_price=10.0,
            fee_rate=0.001,
            timing_fields={},
            depth_fields={},
        )


def test_direct_cash_fraction_is_not_request_authority_when_plan_exists() -> None:
    cash = 1_000_000.0
    legacy_buy_fraction = 0.5
    request = _request(_plan(side="BUY", qty=None, notional_krw=12_000.0))

    assert request is not None
    assert request.requested_notional == 12_000.0
    assert request.requested_notional != cash * legacy_buy_fraction


def test_research_buy_bundle_uses_typed_execution_planner_summary() -> None:
    bundle = _research_execution_plan_bundle(
        side="BUY",
        cash=500_000.0,
        buy_fraction=0.99,
        sellable_qty=0.0,
        reference_price=10.0,
        policy_decision=_typed_decision(final_signal="BUY"),
    )

    assert bundle.summary is not None
    assert bundle.submit_plan is not None
    assert bundle.submit_plan.submit_expected is True
    assert bundle.submit_plan.notional_krw == 100_000.0
    assert bundle.submit_plan.notional_krw != 500_000.0 * 0.99
    assert bundle.authority == bundle.submit_plan.authority


def test_research_typed_hold_blocks_without_fill_request() -> None:
    bundle = _research_execution_plan_bundle(
        side="BUY",
        cash=500_000.0,
        buy_fraction=1.0,
        sellable_qty=0.0,
        reference_price=10.0,
        policy_decision=_typed_decision(raw_signal="BUY", final_signal="HOLD"),
    )

    assert bundle.summary is not None
    assert bundle.submit_plan is not None
    assert bundle.submit_plan.submit_expected is False
    assert execution_submit_plan_to_research_request(
        submit_plan=bundle.submit_plan,
        signal_ts=100,
        decision_ts=200,
        reference_price=10.0,
        fee_rate=0.001,
        timing_fields={},
        depth_fields={},
    ) is None
