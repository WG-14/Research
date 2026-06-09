from __future__ import annotations

from statistics import fmean
from typing import Any

from bithumb_bot.market_regime import classify_market_regime_from_arrays
from bithumb_bot.research.backtest_types import BacktestRunContext
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.decision_event import ResearchDecisionEvent
from bithumb_bot.research.execution_timing import candle_close_ts
from bithumb_bot.research.experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy
from bithumb_bot.research.prepared_candles import PreparedCandleArrays, prepare_candle_arrays
from bithumb_bot.research.strategy_registry import ResearchStrategyPlugin
from bithumb_bot.research.strategy_spec import (
    StrategyParameterSchema,
    StrategySpec,
    StrategySpecError,
    materialize_strategy_parameters,
)
from bithumb_bot.strategy_authoring import research_plugin_from_event_builder


CHANNEL_BREAKOUT_STRATEGY_NAME = "channel_breakout_with_regime_filter"

CHANNEL_BREAKOUT_COMPLEXITY_METADATA = {
    "schema_version": 1,
    "complexity_class": "linear_precomputed_ohlcv",
    "expected_us_per_candle": 25,
    "precompute_required": True,
    "precompute_path": "prepare_channel_breakout_context",
}

CHANNEL_BREAKOUT_SPEC = StrategySpec(
    strategy_name=CHANNEL_BREAKOUT_STRATEGY_NAME,
    strategy_version="channel_breakout_with_regime_filter.research_contract.v1",
    accepted_parameter_names=(
        "CHANNEL_BREAKOUT_LOOKBACK",
        "CHANNEL_BREAKOUT_RANGE_WINDOW",
        "CHANNEL_BREAKOUT_RANGE_RATIO_MIN",
        "CHANNEL_BREAKOUT_VOLUME_WINDOW",
        "CHANNEL_BREAKOUT_VOLUME_RATIO_MIN",
        "CHANNEL_BREAKOUT_REGIME_FILTER_ENABLED",
        "STRATEGY_EXIT_RULES",
        "STRATEGY_EXIT_STOP_LOSS_RATIO",
        "STRATEGY_EXIT_MAX_HOLDING_MIN",
    ),
    required_parameter_names=(
        "CHANNEL_BREAKOUT_LOOKBACK",
        "CHANNEL_BREAKOUT_RANGE_WINDOW",
        "CHANNEL_BREAKOUT_VOLUME_WINDOW",
    ),
    behavior_affecting_parameter_names=(
        "CHANNEL_BREAKOUT_LOOKBACK",
        "CHANNEL_BREAKOUT_RANGE_WINDOW",
        "CHANNEL_BREAKOUT_RANGE_RATIO_MIN",
        "CHANNEL_BREAKOUT_VOLUME_WINDOW",
        "CHANNEL_BREAKOUT_VOLUME_RATIO_MIN",
        "CHANNEL_BREAKOUT_REGIME_FILTER_ENABLED",
        "STRATEGY_EXIT_RULES",
        "STRATEGY_EXIT_STOP_LOSS_RATIO",
        "STRATEGY_EXIT_MAX_HOLDING_MIN",
    ),
    metadata_only_parameter_names=(),
    research_only_parameter_names=(),
    default_parameters={
        "CHANNEL_BREAKOUT_RANGE_RATIO_MIN": 1.2,
        "CHANNEL_BREAKOUT_VOLUME_RATIO_MIN": 1.1,
        "CHANNEL_BREAKOUT_REGIME_FILTER_ENABLED": True,
        "STRATEGY_EXIT_RULES": "stop_loss,max_holding_time",
        "STRATEGY_EXIT_STOP_LOSS_RATIO": 0.01,
        "STRATEGY_EXIT_MAX_HOLDING_MIN": 30,
    },
    parameter_schema=(
        StrategyParameterSchema("CHANNEL_BREAKOUT_LOOKBACK", "int", required=True, min_value=2, unit="candles"),
        StrategyParameterSchema("CHANNEL_BREAKOUT_RANGE_WINDOW", "int", required=True, min_value=2, unit="candles"),
        StrategyParameterSchema("CHANNEL_BREAKOUT_RANGE_RATIO_MIN", "float", min_value=0.0, unit="range_ratio"),
        StrategyParameterSchema("CHANNEL_BREAKOUT_VOLUME_WINDOW", "int", required=True, min_value=2, unit="candles"),
        StrategyParameterSchema("CHANNEL_BREAKOUT_VOLUME_RATIO_MIN", "float", min_value=0.0, unit="volume_ratio"),
        StrategyParameterSchema("CHANNEL_BREAKOUT_REGIME_FILTER_ENABLED", "bool", unit="enabled_flag"),
        StrategyParameterSchema("STRATEGY_EXIT_RULES", "str", unit="comma_separated_exit_rule_names"),
        StrategyParameterSchema("STRATEGY_EXIT_STOP_LOSS_RATIO", "float", min_value=0.0, unit="unrealized_pnl_ratio"),
        StrategyParameterSchema("STRATEGY_EXIT_MAX_HOLDING_MIN", "int", min_value=0, unit="minutes"),
    ),
    decision_contract_version="research_channel_breakout_decision_contract.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={
        "schema_version": 1,
        "rules": ("stop_loss", "max_holding_time"),
        "stop_loss": {
            "unit": "unrealized_pnl_ratio",
            "disabled_value": 0,
            "evaluation_price_basis": "closed_candle_mark",
            "intrabar_stop_modeled": False,
            "limitation_reasons": (
                "intra_candle_path_unavailable",
                "candle_close_stop_may_exit_later_than_real_stop",
            ),
        },
        "max_holding_time": {"unit": "minutes", "disabled_value": 0},
    },
)


def materialize_channel_breakout_parameters(
    *,
    plugin: ResearchStrategyPlugin,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    context: BacktestRunContext | None = None,
) -> dict[str, Any]:
    del context
    values = materialize_strategy_parameters(
        plugin.name,
        parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )
    for name in (
        "CHANNEL_BREAKOUT_LOOKBACK",
        "CHANNEL_BREAKOUT_RANGE_WINDOW",
        "CHANNEL_BREAKOUT_VOLUME_WINDOW",
    ):
        if int(values[name]) < 2:
            raise StrategySpecError(f"{name} must be >= 2")
    rules = _normalize_exit_rules(values.get("STRATEGY_EXIT_RULES") or "")
    unsupported = sorted(set(rules) - {"stop_loss", "max_holding_time"})
    if unsupported:
        raise StrategySpecError(
            "STRATEGY_EXIT_RULES contains unsupported rule(s): " + ",".join(unsupported)
        )
    return values


def decide_channel_breakout_snapshot(
    *,
    candle: Candle,
    candle_index: int,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    candles: tuple[Candle, ...] | None = None,
    closes: tuple[float, ...] | None = None,
    highs: tuple[float, ...] | None = None,
    lows: tuple[float, ...] | None = None,
    volumes: tuple[float, ...] | None = None,
) -> dict[str, Any]:
    if candles is None:
        candles = tuple(dataset.candles)
    if closes is None:
        closes = tuple(float(item.close) for item in candles)
    if highs is None:
        highs = tuple(float(item.high) for item in candles)
    if lows is None:
        lows = tuple(float(item.low) for item in candles)
    if volumes is None:
        volumes = tuple(float(item.volume) for item in candles)
    lookback = int(parameter_values["CHANNEL_BREAKOUT_LOOKBACK"])
    range_window = int(parameter_values["CHANNEL_BREAKOUT_RANGE_WINDOW"])
    volume_window = int(parameter_values["CHANNEL_BREAKOUT_VOLUME_WINDOW"])
    min_required_prior = max(lookback, range_window, volume_window)
    close = float(candle.close)
    volume = float(candle.volume)

    if candle_index < min_required_prior:
        regime = classify_market_regime_from_arrays(
            closes=closes,
            highs=highs,
            lows=lows,
            volumes=volumes,
            index=int(candle_index),
            volatility_window=range_window,
            volume_window=volume_window,
            liquidity_window=volume_window,
        )
        feature_snapshot = {
            "schema_version": 1,
            "candle_index": int(candle_index),
            "close": close,
            "rolling_high": 0.0,
            "breakout_distance": 0.0,
            "current_range": float(candle.high) - float(candle.low),
            "avg_range": 0.0,
            "range_ratio": 0.0,
            "volume": volume,
            "avg_volume": 0.0,
            "volume_ratio": 0.0,
            "price_regime": regime.price_regime,
            "volatility_bucket": regime.volatility_bucket,
            "volume_bucket": regime.volume_bucket,
            "liquidity_bucket": regime.liquidity_bucket,
            "composite_regime": regime.composite_regime,
            "blocked_filters": (),
            "required_prior_candles": int(min_required_prior),
        }
        return {
            "signal": "HOLD",
            "reason": "not_enough_lookback",
            "feature_snapshot": feature_snapshot,
            "strategy_diagnostics": {
                "schema_version": 1,
                "blocked_filters": (),
                "regime_filter_enabled": bool(parameter_values["CHANNEL_BREAKOUT_REGIME_FILTER_ENABLED"]),
            },
        }

    prior_lookback = candles[candle_index - lookback : candle_index]
    prior_range = candles[candle_index - range_window : candle_index]
    prior_volume = candles[candle_index - volume_window : candle_index]
    rolling_high = max(float(item.high) for item in prior_lookback)
    current_range = float(candle.high) - float(candle.low)
    avg_range = fmean(float(item.high) - float(item.low) for item in prior_range)
    range_ratio = _safe_ratio(current_range, avg_range)
    avg_volume = fmean(float(item.volume) for item in prior_volume)
    volume_ratio = _safe_ratio(volume, avg_volume)
    breakout_distance = _safe_ratio(close - rolling_high, rolling_high)

    regime = classify_market_regime_from_arrays(
        closes=closes,
        highs=highs,
        lows=lows,
        volumes=volumes,
        index=int(candle_index),
        volatility_window=range_window,
        volume_window=volume_window,
        liquidity_window=volume_window,
    )

    blocked_filters: list[str] = []
    if close <= rolling_high:
        blocked_filters.append("close_not_above_rolling_high")
    if range_ratio < float(parameter_values["CHANNEL_BREAKOUT_RANGE_RATIO_MIN"]):
        blocked_filters.append("range_ratio_below_min")
    if volume_ratio < float(parameter_values["CHANNEL_BREAKOUT_VOLUME_RATIO_MIN"]):
        blocked_filters.append("volume_ratio_below_min")
    regime_filter_enabled = bool(parameter_values["CHANNEL_BREAKOUT_REGIME_FILTER_ENABLED"])
    if regime_filter_enabled:
        if regime.price_regime == "downtrend":
            blocked_filters.append("downtrend_regime")
        if regime.legacy_regime == "chop" or regime.price_regime == "sideways":
            blocked_filters.append("chop_regime")

    blocked = tuple(blocked_filters)
    signal = "BUY" if not blocked else "HOLD"
    feature_snapshot = {
        "schema_version": 1,
        "candle_index": int(candle_index),
        "close": close,
        "rolling_high": float(rolling_high),
        "breakout_distance": float(breakout_distance),
        "current_range": float(current_range),
        "avg_range": float(avg_range),
        "range_ratio": float(range_ratio),
        "volume": volume,
        "avg_volume": float(avg_volume),
        "volume_ratio": float(volume_ratio),
        "price_regime": regime.price_regime,
        "volatility_bucket": regime.volatility_bucket,
        "volume_bucket": regime.volume_bucket,
        "liquidity_bucket": regime.liquidity_bucket,
        "composite_regime": regime.composite_regime,
        "blocked_filters": blocked,
    }
    decision = {
        "signal": signal,
        "reason": "channel_breakout_confirmed" if signal == "BUY" else "channel_breakout_blocked",
        "feature_snapshot": feature_snapshot,
        "strategy_diagnostics": {
            "schema_version": 1,
            "blocked_filters": blocked,
            "regime_filter_enabled": regime_filter_enabled,
        },
    }
    if signal == "BUY":
        decision["order_intent"] = {
            "side": "BUY",
            "sizing": "portfolio_policy_fractional_cash",
        }
    return decision


def prepare_channel_breakout_context(dataset: DatasetSnapshot) -> PreparedCandleArrays:
    return prepare_candle_arrays(dataset)


def build_channel_breakout_research_events(
    *,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    execution_timing_policy: ExecutionTimingPolicy,
    portfolio_policy: PortfolioPolicy,
    context: Any | None = None,
) -> tuple[ResearchDecisionEvent, ...]:
    del fee_rate, slippage_bps, portfolio_policy, context
    prepared = prepare_channel_breakout_context(dataset)
    candles = prepared.candles
    events: list[ResearchDecisionEvent] = []
    for candle_index, candle in enumerate(candles):
        decision = decide_channel_breakout_snapshot(
            candle=candle,
            candle_index=candle_index,
            dataset=dataset,
            parameter_values=parameter_values,
            candles=candles,
            closes=prepared.closes,
            highs=prepared.highs,
            lows=prepared.lows,
            volumes=prepared.volumes,
        )
        signal = str(decision.get("signal") or "HOLD").upper()
        feature_snapshot = dict(decision.get("feature_snapshot") or {})
        blocked_filters = tuple(str(item) for item in feature_snapshot.get("blocked_filters") or ())
        decision_ts = candle_close_ts(candle, interval=dataset.interval) + int(
            execution_timing_policy.decision_guard_ms
        )
        events.append(
            ResearchDecisionEvent(
                candle_ts=int(candle.ts),
                decision_ts=int(decision_ts),
                strategy_name=CHANNEL_BREAKOUT_SPEC.strategy_name,
                strategy_version=CHANNEL_BREAKOUT_SPEC.strategy_version,
                raw_signal=signal,
                final_signal=signal,
                reason=str(decision.get("reason") or "channel_breakout_research_decision"),
                feature_snapshot=feature_snapshot,
                strategy_diagnostics=dict(decision.get("strategy_diagnostics") or {}),
                entry_signal=signal if signal == "BUY" else "HOLD",
                exit_signal="HOLD",
                blocked_filters=blocked_filters,
                order_intent=(
                    dict(decision["order_intent"])
                    if isinstance(decision.get("order_intent"), dict)
                    else None
                ),
                exit_intent={
                    "mode": "evaluate_exit_policy",
                    "base_signal": "HOLD",
                    "base_reason": "common_exit_policy_only",
                },
                extra_payload={"strategy_family": "channel_breakout", "research_only": True},
            )
        )
    return tuple(events)


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0.0:
        return 0.0
    return float(numerator) / float(denominator)


def _normalize_exit_rules(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str):
        raise StrategySpecError("STRATEGY_EXIT_RULES must be str")
    return tuple(token.strip().lower() for token in raw.split(",") if token.strip())


CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN = research_plugin_from_event_builder(
    strategy_name=CHANNEL_BREAKOUT_SPEC.strategy_name,
    version=CHANNEL_BREAKOUT_SPEC.strategy_version,
    spec=CHANNEL_BREAKOUT_SPEC,
    required_data=CHANNEL_BREAKOUT_SPEC.required_data,
    optional_data=CHANNEL_BREAKOUT_SPEC.optional_data,
    build_research_events=build_channel_breakout_research_events,
    diagnostics_namespace=CHANNEL_BREAKOUT_SPEC.strategy_name,
    research_parameter_materializer=materialize_channel_breakout_parameters,
).to_research_strategy_plugin()

object.__setattr__(
    CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN,
    "complexity_metadata",
    CHANNEL_BREAKOUT_COMPLEXITY_METADATA,
)
