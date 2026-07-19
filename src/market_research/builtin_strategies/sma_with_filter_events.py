"""Research-native SMA feature and event builder."""

from __future__ import annotations

from typing import Any

from market_research.research.dataset_snapshot import DatasetSnapshot
from market_research.research.decision_event import OrderIntent, ResearchDecisionEvent
from market_research.research.execution_timing import candle_close_ts
from market_research.research.experiment_manifest import ExecutionTimingPolicy
from market_research.research.hashing import sha256_prefixed
from .sma_with_filter import SMA_WITH_FILTER_SPEC
from .sma_with_filter_features import (
    build_sma_feature_context,
    compute_sma_feature_values,
    feature_values_from_sma_snapshot,
)


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
    del portfolio_policy, context, fee_rate, slippage_bps
    short_n, long_n = (
        int(parameter_values["SMA_SHORT"]),
        int(parameter_values["SMA_LONG"]),
    )
    if short_n <= 0 or long_n <= 0 or short_n >= long_n:
        raise ValueError("SMA_SHORT must be smaller than SMA_LONG")
    closes = tuple(float(candle.close) for candle in dataset.candles)
    events: list[ResearchDecisionEvent] = []
    for index in range(long_n, len(closes)):
        feature_values = compute_sma_feature_values(
            context=build_sma_feature_context(
                closes=closes,
                index=index,
                parameter_values=parameter_values,
            )
        )
        prev_s = feature_values["prev_short_sma"]
        prev_l = feature_values["prev_long_sma"]
        curr_s = feature_values["short_sma"]
        curr_l = feature_values["long_sma"]
        raw = (
            "BUY"
            if prev_s <= prev_l and curr_s > curr_l
            else "SELL"
            if prev_s >= prev_l and curr_s < curr_l
            else "HOLD"
        )
        reason = (
            "sma golden cross"
            if raw == "BUY"
            else "sma dead cross"
            if raw == "SELL"
            else "sma no crossover"
        )
        gap = feature_values["gap_ratio"]
        volatility = feature_values["volatility_ratio"]
        overextended = feature_values["overextended_ratio"]
        blocked: list[str] = []
        if raw == "BUY" and gap < float(
            parameter_values.get("SMA_FILTER_GAP_MIN_RATIO") or 0.0
        ):
            blocked.append("gap")
        if raw == "BUY" and volatility < float(
            parameter_values.get("SMA_FILTER_VOL_MIN_RANGE_RATIO") or 0.0
        ):
            blocked.append("volatility")
        if raw == "BUY" and overextended > float(
            parameter_values.get("SMA_FILTER_OVEREXT_MAX_RETURN_RATIO") or 0.0
        ):
            blocked.append("overextended")
        required_edge = max(
            0.0,
            float(parameter_values.get("SMA_COST_EDGE_MIN_RATIO") or 0.0),
            2.0 * float(parameter_values.get("LIVE_FEE_RATE_ESTIMATE") or 0.0)
            + float(parameter_values.get("STRATEGY_ENTRY_SLIPPAGE_BPS") or 0.0)
            / 10_000.0
            + float(parameter_values.get("ENTRY_EDGE_BUFFER_RATIO") or 0.0),
            float(parameter_values.get("STRATEGY_MIN_EXPECTED_EDGE_RATIO") or 0.0),
        )
        if (
            raw == "BUY"
            and bool(parameter_values.get("SMA_COST_EDGE_ENABLED"))
            and gap < required_edge
        ):
            blocked.append("cost_edge")
        entry = "HOLD" if raw == "BUY" and blocked else raw
        candle = dataset.candles[index]
        features: dict[str, object] = {
            "schema_version": 1,
            "candle_index": index,
            **feature_values,
        }
        feature_values_from_sma_snapshot(features)
        features["feature_snapshot_hash"] = sha256_prefixed(features)
        decision_ts = candle_close_ts(candle, interval=dataset.interval) + int(
            execution_timing_policy.decision_guard_ms
        )
        decision_id = sha256_prefixed(
            {
                "strategy_name": "sma_with_filter",
                "strategy_version": SMA_WITH_FILTER_SPEC.strategy_version,
                "candle_ts": int(candle.ts),
                "decision_ts": decision_ts,
                "raw_signal": raw,
                "final_signal": entry,
                "reason": reason,
                "feature_snapshot": features,
            }
        )
        events.append(
            ResearchDecisionEvent(
                candle_ts=int(candle.ts),
                decision_ts=decision_ts,
                strategy_name="sma_with_filter",
                strategy_version=SMA_WITH_FILTER_SPEC.strategy_version,
                raw_signal=raw,
                final_signal=entry,
                reason=reason,
                feature_snapshot=features,
                strategy_diagnostics={
                    "schema_version": 1,
                    "raw_signal": raw,
                    "entry_signal": entry,
                    "blocked_filters": list(blocked),
                },
                entry_signal=entry,
                exit_signal=raw,
                blocked_filters=tuple(blocked),
                order_intent=OrderIntent.from_decision(
                    decision_id=decision_id,
                    side="BUY",
                    sizing="portfolio_policy_fractional_cash",
                    reason=reason,
                )
                if entry == "BUY"
                else None,
            )
        )
    return tuple(events)
