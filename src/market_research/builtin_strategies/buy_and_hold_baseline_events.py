"""Research-native decision events for the buy-and-hold baseline."""

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

_STRATEGY_NAME = "buy_and_hold_baseline"
_STRATEGY_VERSION = "buy_and_hold_baseline.research_contract.v1"


def build_buy_and_hold_baseline_events(
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
    """Emit one BUY at the configured candle and HOLD everywhere else."""
    del fee_rate, slippage_bps, portfolio_policy, context
    buy_index = max(0, int(parameter_values["BUY_HOLD_BUY_INDEX"]))
    decision_reason = str(
        parameter_values.get("BUY_HOLD_DECISION_REASON")
        or "buy_and_hold_architecture_canary"
    )
    events: list[ResearchDecisionEvent] = []
    for local_index, candle in enumerate(dataset.candles):
        index = int(candle_index_offset) + local_index
        is_buy = index == buy_index
        action = "BUY" if is_buy else "HOLD"
        decision_ts = candle_close_ts(candle, interval=dataset.interval) + int(
            execution_timing_policy.decision_guard_ms
        )
        features = {
            "candle_index": int(index),
            "buy_index": int(buy_index),
            "close": float(candle.close),
        }
        decision_id = sha256_prefixed(
            {
                "strategy_name": _STRATEGY_NAME,
                "strategy_version": _STRATEGY_VERSION,
                "candle_ts": int(candle.ts),
                "decision_ts": decision_ts,
                "raw_signal": action,
                "final_signal": action,
                "reason": decision_reason
                if is_buy
                else "buy_and_hold_after_entry_hold",
                "feature_snapshot": features,
            }
        )
        events.append(
            ResearchDecisionEvent(
                candle_ts=int(candle.ts),
                decision_ts=decision_ts,
                strategy_name=_STRATEGY_NAME,
                strategy_version=_STRATEGY_VERSION,
                raw_signal=action,
                entry_signal=action,
                exit_signal="HOLD",
                final_signal=action,
                reason=(decision_reason if is_buy else "buy_and_hold_after_entry_hold"),
                feature_snapshot=features,
                strategy_diagnostics={
                    "schema_version": 1,
                    "buy_index": int(buy_index),
                    "candle_index": int(index),
                    "emitted_buy_intent": is_buy,
                },
                order_intent=(
                    OrderIntent.from_decision(
                        decision_id=decision_id,
                        side="BUY",
                        sizing="portfolio_policy_fractional_cash",
                        reason=decision_reason,
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
