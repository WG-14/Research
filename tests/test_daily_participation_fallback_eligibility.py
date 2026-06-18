from __future__ import annotations

from tests.test_daily_participation_sizing import _count_snapshot, _exit_policy, _market, _position, _state
from bithumb_bot.core.sma_policy import ExecutionConstraintSnapshot, SmaPolicyConfig
from bithumb_bot.sma_decision import SmaEntryDecision
from bithumb_bot.strategy.daily_participation_policy import (
    DOCUMENT_FALLBACK_MODE_ALIASES,
    DailyParticipationPolicyConfig,
)
from bithumb_bot.strategy_plugins.daily_participation_sma import evaluate_daily_participation_sma_decision


def _config() -> SmaPolicyConfig:
    return SmaPolicyConfig(
        strategy_name="daily_participation_sma",
        short_n=2,
        long_n=4,
        min_gap_ratio=0.02,
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


def _config_with(**overrides: object) -> SmaPolicyConfig:
    payload = _config().__dict__.copy()
    payload.update(overrides)
    return SmaPolicyConfig(**payload)


def _daily(mode: str) -> DailyParticipationPolicyConfig:
    return DailyParticipationPolicyConfig(
        enabled=True,
        timezone="Asia/Seoul",
        count_basis="filled",
        window_start_hour=0,
        window_end_hour=24,
        buy_fraction=0.05,
        max_order_krw=10000.0,
        fallback_mode=mode,  # type: ignore[arg-type]
    )


def test_fallback_mode_change_changes_policy_hash() -> None:
    assert _daily("unconditional_participation").policy_hash() != _daily("requires_base_safety_filter").policy_hash()


def test_requires_base_safety_filter_blocks_when_base_filter_blocks() -> None:
    decision = evaluate_daily_participation_sma_decision(
        market=_market(prev_s=100.0, prev_l=99.0, curr_s=101.0, curr_l=100.0),
        position=_position(),
        config=_config(),
        execution_context=ExecutionConstraintSnapshot(fee_rate_for_decision=0.001),
        exit_policy_config=_exit_policy(),
        participation_config=_daily("requires_base_safety_filter"),
        participation_state=_state(),
        count_snapshot=_count_snapshot(),
    )

    assert decision.final_signal == "HOLD"
    assert decision.trace["fallback_mode"] == "requires_base_safety_filter"
    assert decision.trace["base_blocked_filters"]


def test_safety_filtered_mode_blocks_when_market_regime_blocks() -> None:
    market = _market(prev_s=99.0, prev_l=100.0, curr_s=102.0, curr_l=101.0)
    market = market.__class__(
        **{
            **market.__dict__,
            "entry_decision": SmaEntryDecision(
                base_signal="BUY",
                base_reason="sma golden cross",
                entry_signal="HOLD",
                entry_reason="market regime blocked: chop_market",
                prev_s=99.0,
                prev_l=100.0,
                curr_s=102.0,
                curr_l=101.0,
                gap_ratio=0.01,
                volatility_ratio=0.01,
                overextended_ratio=0.0,
                blocked_filters=("market_regime.chop_market",),
                gap_filter_enabled=True,
                volatility_filter_enabled=True,
                overextended_filter_enabled=True,
                gap_triggered=False,
                volatility_triggered=False,
                overextended_triggered=False,
                edge_filter_triggered=False,
                edge_filter_details={},
                market_regime={"regime": "chop", "allows_entry": False, "block_reason": "chop_market"},
                market_regime_triggered=True,
                candidate_regime_decision={},
                candidate_regime_triggered=False,
                filter_blocked=True,
                raw_filter_would_block=True,
                entry_blocked=True,
            ),
        }
    )

    decision = evaluate_daily_participation_sma_decision(
        market=market,
        position=_position(),
        config=_config_with(market_regime_enabled=True, min_gap_ratio=0.0),
        execution_context=ExecutionConstraintSnapshot(fee_rate_for_decision=0.001),
        exit_policy_config=_exit_policy(),
        participation_config=_daily("requires_base_safety_filter"),
        participation_state=_state(),
        count_snapshot=_count_snapshot(),
    )

    assert decision.final_signal == "HOLD"
    assert decision.trace["fallback_block_reason"] == "daily_participation_base_safety_filter_blocked"
    assert "market_regime" in ",".join(str(item) for item in decision.trace["base_blocked_filters"])


def test_safety_filtered_mode_blocks_when_cost_edge_blocks() -> None:
    decision = evaluate_daily_participation_sma_decision(
        market=_market(prev_s=99.0, prev_l=100.0, curr_s=102.0, curr_l=101.0),
        position=_position(),
        config=_config_with(cost_edge_enabled=True, cost_edge_min_ratio=0.50),
        execution_context=ExecutionConstraintSnapshot(fee_rate_for_decision=0.001),
        exit_policy_config=_exit_policy(),
        participation_config=_daily("requires_base_safety_filter"),
        participation_state=_state(),
        count_snapshot=_count_snapshot(),
    )

    assert decision.final_signal == "HOLD"
    assert decision.trace["fallback_block_reason"] == "daily_participation_base_safety_filter_blocked"
    assert "cost_edge" in decision.trace["base_blocked_filters"]


def test_fallback_mode_names_match_document_contract_or_declared_aliases() -> None:
    payload = _daily("requires_base_safety_filter").policy_payload()

    assert DOCUMENT_FALLBACK_MODE_ALIASES["safety_filtered_participation"] == "requires_base_safety_filter"
    assert DOCUMENT_FALLBACK_MODE_ALIASES["unconditional_time_participation"] == "unconditional_participation"
    assert payload["fallback_mode_document_name"] == "safety_filtered_participation"
    assert payload["fallback_mode_alias_contract"]["document_to_code"] == DOCUMENT_FALLBACK_MODE_ALIASES


def test_unconditional_mode_trace_declares_sma_filter_bypass() -> None:
    decision = evaluate_daily_participation_sma_decision(
        market=_market(prev_s=100.0, prev_l=99.0, curr_s=101.0, curr_l=100.0),
        position=_position(),
        config=_config(),
        execution_context=ExecutionConstraintSnapshot(fee_rate_for_decision=0.001),
        exit_policy_config=_exit_policy(),
        participation_config=_daily("unconditional_participation"),
        participation_state=_state(),
        count_snapshot=_count_snapshot(),
    )

    assert decision.final_signal == "BUY"
    assert decision.trace["fallback_mode"] == "unconditional_participation"
    assert decision.trace["entry_signal_source"] == "daily_participation_fallback"
