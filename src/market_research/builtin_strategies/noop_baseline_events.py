"""Research-native decision events for the noop baseline."""

from __future__ import annotations

from typing import Any

from market_research.research.dataset_snapshot import DatasetSnapshot
from market_research.research.decision_event import ResearchDecisionEvent
from market_research.research.execution_timing import candle_close_ts
from market_research.research.experiment_manifest import (
    ExecutionTimingPolicy,
    PortfolioPolicy,
)

_STRATEGY_NAME = "noop_baseline"
_STRATEGY_VERSION = "noop_baseline.research_contract.v1"


def build_noop_baseline_events(
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
    """Emit HOLD-only decisions from the configured candle index onward."""
    del fee_rate, slippage_bps, portfolio_policy, context
    start_index = max(0, int(parameter_values.get("NOOP_DECISION_START_INDEX", 0)))
    decision_reason = str(
        parameter_values.get("NOOP_DECISION_REASON") or "noop_baseline_hold"
    )
    events: list[ResearchDecisionEvent] = []
    for local_index, candle in enumerate(dataset.candles):
        index = int(candle_index_offset) + local_index
        if index < start_index:
            continue
        events.append(
            ResearchDecisionEvent(
                candle_ts=int(candle.ts),
                decision_ts=(
                    candle_close_ts(candle, interval=dataset.interval)
                    + int(execution_timing_policy.decision_guard_ms)
                ),
                strategy_name=_STRATEGY_NAME,
                strategy_version=_STRATEGY_VERSION,
                raw_signal="HOLD",
                entry_signal="HOLD",
                exit_signal="HOLD",
                final_signal="HOLD",
                reason=decision_reason,
                feature_snapshot={
                    "candle_index": int(index),
                    "close": float(candle.close),
                    "start_index": int(start_index),
                },
                strategy_diagnostics={
                    "schema_version": 1,
                    "hold_decision_count": int(index - start_index + 1),
                    "start_index": int(start_index),
                },
                extra_payload={
                    "execution_intent": "none",
                    "exit_policy": "no_entry_no_exit",
                    "position_unchanged": True,
                    "cash_unchanged": True,
                },
            )
        )
    return tuple(events)
