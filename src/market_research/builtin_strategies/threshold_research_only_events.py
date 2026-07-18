"""Research-native decision events for ``threshold_research_only``."""

from __future__ import annotations

from typing import Any

from market_research.research.dataset_snapshot import DatasetSnapshot
from market_research.research.decision_event import OrderIntent, ResearchDecisionEvent
from market_research.research.hashing import sha256_prefixed
from market_research.research.execution_timing import candle_close_ts
from market_research.research.experiment_manifest import (
    ExecutionTimingPolicy,
    PortfolioPolicy,
)
from .threshold_research_only import THRESHOLD_RESEARCH_ONLY_SPEC


def build_threshold_research_only_events(
    *,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    execution_timing_policy: ExecutionTimingPolicy,
    portfolio_policy: PortfolioPolicy,
    context: Any | None = None,
    candle_index_offset: int = 0,
) -> tuple[ResearchDecisionEvent, ...]:
    """Evaluate the strict close-above threshold on every candle."""
    del fee_rate, slippage_bps, portfolio_policy, context
    threshold = float(parameter_values["THRESHOLD_CLOSE_ABOVE"])
    events: list[ResearchDecisionEvent] = []
    for local_index, candle in enumerate(dataset.candles):
        index = int(candle_index_offset) + local_index
        close = float(candle.close)
        is_buy = close > threshold
        signal = "BUY" if is_buy else "HOLD"
        decision_ts = candle_close_ts(candle, interval=dataset.interval) + int(
            execution_timing_policy.decision_guard_ms
        )
        reason = "threshold_close_above" if is_buy else "threshold_not_met"
        features: dict[str, object] = {
            "candle_index": int(index),
            "close": close,
            "threshold_close_above": threshold,
        }
        decision_id = sha256_prefixed(
            {
                "strategy_name": THRESHOLD_RESEARCH_ONLY_SPEC.strategy_name,
                "strategy_version": THRESHOLD_RESEARCH_ONLY_SPEC.strategy_version,
                "candle_ts": int(candle.ts),
                "decision_ts": decision_ts,
                "raw_signal": signal,
                "final_signal": signal,
                "reason": reason,
                "feature_snapshot": features,
            }
        )
        events.append(
            ResearchDecisionEvent(
                candle_ts=int(candle.ts),
                decision_ts=decision_ts,
                strategy_name=THRESHOLD_RESEARCH_ONLY_SPEC.strategy_name,
                strategy_version=THRESHOLD_RESEARCH_ONLY_SPEC.strategy_version,
                raw_signal=signal,
                entry_signal=signal,
                exit_signal="HOLD",
                final_signal=signal,
                reason=reason,
                feature_snapshot=features,
                strategy_diagnostics={
                    "schema_version": 1,
                    "threshold_close_above": threshold,
                    "close_above_threshold": is_buy,
                },
                order_intent=(
                    OrderIntent.from_decision(
                        decision_id=decision_id,
                        side="BUY",
                        sizing="portfolio_policy_fractional_cash",
                        reason=reason,
                    )
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
