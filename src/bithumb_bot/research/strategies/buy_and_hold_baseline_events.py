"""Research-native decision events for the buy-and-hold baseline."""

from __future__ import annotations

from typing import Any

from ..dataset_snapshot import DatasetSnapshot
from ..decision_event import ResearchDecisionEvent
from ..execution_timing import candle_close_ts
from ..experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy
from ..strategy_spec import BUY_AND_HOLD_BASELINE_SPEC


def build_buy_and_hold_baseline_events(
    *,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    execution_timing_policy: ExecutionTimingPolicy,
    portfolio_policy: PortfolioPolicy,
    context: Any | None = None,
) -> tuple[ResearchDecisionEvent, ...]:
    """Emit one BUY at the configured candle and HOLD everywhere else."""
    del fee_rate, slippage_bps, context
    buy_index = max(0, int(parameter_values["BUY_HOLD_BUY_INDEX"]))
    decision_reason = str(
        parameter_values.get("BUY_HOLD_DECISION_REASON")
        or "buy_and_hold_architecture_canary"
    )
    events: list[ResearchDecisionEvent] = []
    for index, candle in enumerate(dataset.candles):
        is_buy = index == buy_index
        action = "BUY" if is_buy else "HOLD"
        events.append(
            ResearchDecisionEvent(
                candle_ts=int(candle.ts),
                decision_ts=candle_close_ts(candle, interval=dataset.interval)
                + int(execution_timing_policy.decision_guard_ms),
                strategy_name=BUY_AND_HOLD_BASELINE_SPEC.strategy_name,
                strategy_version=BUY_AND_HOLD_BASELINE_SPEC.strategy_version,
                raw_signal=action,
                entry_signal=action,
                exit_signal="HOLD",
                final_signal=action,
                reason=(
                    decision_reason if is_buy else "buy_and_hold_after_entry_hold"
                ),
                feature_snapshot={
                    "candle_index": int(index),
                    "buy_index": int(buy_index),
                    "close": float(candle.close),
                },
                strategy_diagnostics={
                    "schema_version": 1,
                    "buy_index": int(buy_index),
                    "candle_index": int(index),
                    "emitted_buy_intent": is_buy,
                },
                order_intent=(
                    {
                        "side": "BUY",
                        "sizing": "portfolio_policy_fractional_cash",
                        "buy_fraction": float(
                            portfolio_policy.position_sizing.buy_fraction
                        ),
                    }
                    if is_buy
                    else None
                ),
                extra_payload={
                    "exit_policy": "no_explicit_exit",
                    "final_position_marked_to_market": True,
                },
            )
        )
    return tuple(events)
