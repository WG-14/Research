from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bithumb_bot.research.dataset_snapshot import DatasetSnapshot
from bithumb_bot.research.decision_event import ResearchDecisionEvent
from bithumb_bot.research.execution_timing import candle_close_ts
from bithumb_bot.research.experiment_manifest import ExecutionTimingPolicy
from bithumb_bot.research.strategy_spec import strategy_spec_for_name

@dataclass(frozen=True)
class SmaWithFilterDecisionAdapter:
    parameter_values: dict[str, Any]
    fee_rate: float
    slippage_bps: float
    timing_policy: ExecutionTimingPolicy
    strategy_name: str = "sma_with_filter"

    def build_events(self, dataset: DatasetSnapshot) -> tuple[ResearchDecisionEvent, ...]:
        # Compatibility serialization layer only. The backtest kernel must
        # re-evaluate sma_with_filter through StrategyDecisionV2 with the
        # simulated position before treating final action fields as authority.
        short_n = int(self.parameter_values.get("SMA_SHORT", self.parameter_values.get("short_n", 0)))
        long_n = int(self.parameter_values.get("SMA_LONG", self.parameter_values.get("long_n", 0)))
        if short_n <= 0 or long_n <= 0 or short_n >= long_n:
            raise ValueError("SMA_SHORT must be smaller than SMA_LONG")

        candles = dataset.candles
        if len(candles) < long_n + 2:
            return ()

        strategy_spec = strategy_spec_for_name(self.strategy_name)
        events: list[ResearchDecisionEvent] = []
        for index in range(long_n, len(candles)):
            candle = candles[index]
            decision_ts = candle_close_ts(candle, interval=dataset.interval) + int(self.timing_policy.decision_guard_ms)
            events.append(
                ResearchDecisionEvent(
                    candle_ts=int(candle.ts),
                    decision_ts=int(decision_ts),
                    strategy_name=self.strategy_name,
                    strategy_version=strategy_spec.strategy_version,
                    raw_signal="HOLD",
                    final_signal="HOLD",
                    reason="research_event_adapter_non_authoritative",
                    feature_snapshot={
                        "schema_version": 1,
                        "candle_index": int(index),
                        "authority": "promotion_decision_seed_only",
                    },
                    strategy_diagnostics={
                        "schema_version": 1,
                        "adapter": "SmaWithFilterDecisionAdapter",
                        "candle_index": int(index),
                        "authority": "promotion_decision_seed_only",
                    },
                    entry_signal="HOLD",
                    exit_signal="HOLD",
                    blocked_filters=(),
                    order_intent=None,
                    exit_intent={
                        "mode": "evaluate_exit_policy",
                        "base_signal": "HOLD",
                        "base_reason": "research_event_adapter_non_authoritative",
                    },
                    extra_payload={
                        "adapter": "SmaWithFilterDecisionAdapter",
                        "index": int(index),
                        "processed_count": int(index - long_n + 1),
                        "seed_contract": "PromotionDecisionSeed.v1",
                        "feature_authority": "SmaWithFilterSnapshotProjector.project_features",
                        "non_authoritative_event_adapter": True,
                    },
                )
            )
        return tuple(events)


def build_sma_with_filter_research_events(
    *,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    execution_timing_policy: ExecutionTimingPolicy,
    portfolio_policy: Any | None = None,
    context: Any | None = None,
) -> tuple[ResearchDecisionEvent, ...]:
    del portfolio_policy, context
    return SmaWithFilterDecisionAdapter(
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        timing_policy=execution_timing_policy,
    ).build_events(dataset)

