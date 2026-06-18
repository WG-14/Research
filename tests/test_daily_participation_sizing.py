from __future__ import annotations

from bithumb_bot.core.sma_policy import ExecutionConstraintSnapshot, MarketWindow, PositionSnapshot, SmaPolicyConfig
from bithumb_bot.strategy.daily_participation_policy import (
    DailyParticipationCountSnapshot,
    DailyParticipationPolicyConfig,
    DailyParticipationStateSnapshot,
)
from bithumb_bot.strategy.exit_rules import ExitPolicyConfig
from bithumb_bot.strategy_plugins.daily_participation_sma import evaluate_daily_participation_sma_decision


def _market(prev_s: float, prev_l: float, curr_s: float, curr_l: float) -> MarketWindow:
    return MarketWindow(
        pair="KRW-BTC",
        interval="1m",
        candle_ts=1_704_046_800_000,
        closes=(100.0, 101.0, 102.0, 103.0),
        prev_s=prev_s,
        prev_l=prev_l,
        curr_s=curr_s,
        curr_l=curr_l,
        gap_ratio=0.01,
        volatility_ratio=0.01,
        overextended_ratio=0.0,
        market_regime_snapshot={"regime": "unknown"},
    )


def _position() -> PositionSnapshot:
    return PositionSnapshot(in_position=False, entry_allowed=True, exit_allowed=True)


def _config() -> SmaPolicyConfig:
    return SmaPolicyConfig(
        strategy_name="daily_participation_sma",
        short_n=2,
        long_n=4,
        min_gap_ratio=0.0,
        volatility_window=2,
        min_volatility_ratio=0.0,
        overextended_lookback=1,
        overextended_max_return_ratio=1.0,
        slippage_bps=0.0,
        live_fee_rate_estimate=0.001,
        entry_edge_buffer_ratio=0.0,
        cost_edge_enabled=False,
        cost_edge_min_ratio=0.0,
        market_regime_enabled=False,
        buy_fraction=0.99,
        max_order_krw=50000.0,
    )


def _participation() -> DailyParticipationPolicyConfig:
    return DailyParticipationPolicyConfig(
        enabled=True,
        timezone="Asia/Seoul",
        count_basis="filled",
        window_start_hour=0,
        window_end_hour=24,
        buy_fraction=0.05,
        max_order_krw=10000.0,
    )


def _state() -> DailyParticipationStateSnapshot:
    return DailyParticipationStateSnapshot(
        decision_ts=1_704_046_800_000,
        count_for_kst_day=0,
        position_open=False,
        daily_count_snapshot_hash="sha256:" + "2" * 64,
    )


def _count_snapshot() -> DailyParticipationCountSnapshot:
    return DailyParticipationCountSnapshot(
        count_basis="filled",
        timezone="Asia/Seoul",
        kst_day="2024-01-01",
        count_for_kst_day=0,
        timestamp_field="fill_ts",
        source="unit",
        rows=(),
        pair="KRW-BTC",
        strategy_instance_id="daily:test",
        event_set_hash="sha256:" + "3" * 64,
        source_contract_hash="sha256:" + "4" * 64,
        query_contract_hash="sha256:" + "5" * 64,
    )


def _exit_policy() -> ExitPolicyConfig:
    return ExitPolicyConfig(
        rule_names=(),
        stop_loss_ratio=0.0,
        max_holding_sec=0.0,
        min_take_profit_ratio=0.0,
        small_loss_tolerance_ratio=0.0,
        live_fee_rate_estimate=0.001,
    )


def _decision(market: MarketWindow):
    return evaluate_daily_participation_sma_decision(
        market=market,
        position=_position(),
        config=_config(),
        execution_context=ExecutionConstraintSnapshot(fee_rate_for_decision=0.001),
        exit_policy_config=_exit_policy(),
        participation_config=_participation(),
        participation_state=_state(),
        count_snapshot=_count_snapshot(),
    )


def test_sma_cross_buy_uses_base_sizing() -> None:
    decision = _decision(_market(prev_s=99.0, prev_l=100.0, curr_s=102.0, curr_l=101.0))

    assert decision.execution_intent is not None
    assert decision.execution_intent.budget_fraction_of_cash == 0.99
    assert decision.trace["entry_signal_source"] == "sma_cross"


def test_daily_fallback_buy_uses_participation_sizing() -> None:
    decision = _decision(_market(prev_s=100.0, prev_l=99.0, curr_s=101.0, curr_l=100.0))

    assert decision.execution_intent is not None
    assert decision.execution_intent.budget_fraction_of_cash == 0.05
    assert decision.execution_intent.max_budget_krw == 10000.0
    assert decision.trace["entry_signal_source"] == "daily_participation_fallback"
    assert decision.trace["entry_sizing_source"] == "daily_participation_policy"


def test_daily_fallback_sizing_is_in_policy_hash_material() -> None:
    first = _decision(_market(prev_s=100.0, prev_l=99.0, curr_s=101.0, curr_l=100.0))
    altered = evaluate_daily_participation_sma_decision(
        market=_market(prev_s=100.0, prev_l=99.0, curr_s=101.0, curr_l=100.0),
        position=_position(),
        config=_config(),
        execution_context=ExecutionConstraintSnapshot(fee_rate_for_decision=0.001),
        exit_policy_config=_exit_policy(),
        participation_config=DailyParticipationPolicyConfig(
            enabled=True,
            timezone="Asia/Seoul",
            count_basis="filled",
            window_start_hour=0,
            window_end_hour=24,
            buy_fraction=0.10,
            max_order_krw=10000.0,
        ),
        participation_state=_state(),
        count_snapshot=_count_snapshot(),
    )

    assert first.policy_input_hash != altered.policy_input_hash


def test_daily_fallback_sizing_reaches_execution_planner() -> None:
    from bithumb_bot.research.execution_planning import _research_execution_plan_bundle

    decision = _decision(_market(prev_s=100.0, prev_l=99.0, curr_s=101.0, curr_l=100.0))
    bundle = _research_execution_plan_bundle(
        side="BUY",
        cash=1_000_000.0,
        buy_fraction=0.99,
        sellable_qty=0.0,
        reference_price=100.0,
        policy_decision=decision,
        candle_ts=1_704_046_800_000,
    )

    assert bundle.submit_plan is not None
    assert bundle.submit_plan.notional_krw == 10000.0
    assert bundle.submit_plan.extra_payload["entry_signal_source"] == "daily_participation_fallback"


def test_final_reason_string_is_not_used_as_source_authority() -> None:
    decision = _decision(_market(prev_s=100.0, prev_l=99.0, curr_s=101.0, curr_l=100.0))

    assert decision.trace["entry_signal_source"] == "daily_participation_fallback"
    assert "daily" in decision.final_reason
