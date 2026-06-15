from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean
from time import gmtime, strftime
from collections.abc import Iterator
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
from bithumb_bot.strategy.base import PositionContext
from bithumb_bot.strategy.exit_rules import ExitRuleDecision
from bithumb_bot.strategy_authoring import research_plugin_from_event_builder


CHANNEL_BREAKOUT_STRATEGY_NAME = "channel_breakout_with_regime_filter"
SUPPORTED_ENTRY_MODE_VALUES = (
    "immediate_breakout",
    "delayed_confirmation",
    "retest_hold_after_breakout",
)
_SUPPORTED_ENTRY_MODES = frozenset(SUPPORTED_ENTRY_MODE_VALUES)
ENTRY_COST_BASIS = "round_trip"

CHANNEL_BREAKOUT_COMPLEXITY_METADATA = {
    "schema_version": 1,
    "complexity_class": "linear_precomputed_ohlcv",
    "expected_us_per_candle": 25,
    "precompute_required": True,
    "precompute_path": "prepare_channel_breakout_context",
}


def estimate_channel_breakout_complexity(
    *,
    strategy_name: str,
    parameter_space: dict[str, Any] | None = None,
    report_detail: str = "summary",
    diagnostic_mode: str = "exploratory",
    audit_trail: Any | None = None,
    expected_candle_count: int | None = None,
) -> dict[str, Any]:
    modes = _parameter_values_for_key(parameter_space or {}, "ENTRY_MODE")
    unsupported_modes = sorted(str(mode) for mode in modes if str(mode) not in _SUPPORTED_ENTRY_MODES)
    mode_set = {str(mode) for mode in modes}
    includes_delayed = "delayed_confirmation" in mode_set
    includes_retest_hold = "retest_hold_after_breakout" in mode_set
    full_observability = str(report_detail or "").lower() == "full" or bool(
        getattr(audit_trail, "complete_external", False)
    )
    expected_us = int(CHANNEL_BREAKOUT_COMPLEXITY_METADATA["expected_us_per_candle"])
    decision_payload_bytes = 384
    feature_snapshot_bytes = 512
    reasons = ["linear_precomputed_ohlcv"]
    if includes_delayed:
        expected_us += 15
        decision_payload_bytes += 256
        feature_snapshot_bytes += 256
        reasons.append("delayed_confirmation_pending_state")
    if includes_retest_hold:
        expected_us += 20
        decision_payload_bytes += 320
        feature_snapshot_bytes += 320
        reasons.append("retest_hold_after_breakout_pending_state")
    if full_observability:
        decision_payload_bytes *= 3
        feature_snapshot_bytes *= 2
        reasons.append("full_observability_payloads")
    if unsupported_modes:
        reasons.append("unsupported_entry_mode:" + ",".join(unsupported_modes))
    return {
        "schema_version": 1,
        "strategy_name": strategy_name,
        "expected_candle_count": expected_candle_count,
        "expected_us_per_candle": expected_us,
        "expected_feature_snapshot_bytes_per_event": feature_snapshot_bytes,
        "expected_decision_payload_bytes_per_event": decision_payload_bytes,
        "complexity_reasons": tuple(reasons),
        "unsupported_parameter_values": {"ENTRY_MODE": tuple(unsupported_modes)} if unsupported_modes else {},
    }


def _parameter_values_for_key(parameter_space: dict[str, Any], key: str) -> tuple[Any, ...]:
    if key not in parameter_space:
        return (CHANNEL_BREAKOUT_SPEC.default_parameters.get(key),)
    raw = parameter_space.get(key)
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(raw)
    return (raw,)

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
        "MIN_BREAKOUT_DISTANCE_RATIO",
        "ENTRY_EDGE_BUFFER_RATIO",
        "ENTRY_MODE",
        "CONFIRMATION_WINDOW_MIN",
        "CONFIRMATION_MIN_BREAKOUT_DISTANCE_RATIO",
        "CONFIRMATION_CLOSE_LOCATION_MIN",
        "CONFIRMATION_VOLUME_RATIO_MIN",
        "MAX_UPPER_WICK_RATIO",
        "MIN_BODY_RATIO",
        "PULLBACK_RATIO",
        "RETEST_WINDOW_MIN",
        "RETEST_MAX_DEPTH_RATIO",
        "RETEST_HOLD_CANDLES",
        "RETEST_REBOUND_RATIO",
        "ENTRY_TIME_FILTER_KST_ENABLED",
        "ENTRY_TIME_FILTER_KST_START_HOUR",
        "ENTRY_TIME_FILTER_KST_END_HOUR",
        "COOLDOWN_MIN",
        "MAX_TRADES_PER_DAY",
        "STRATEGY_EXIT_RULES",
        "STRATEGY_EXIT_STOP_LOSS_RATIO",
        "STRATEGY_EXIT_MAX_HOLDING_MIN",
        "TAKE_PROFIT_RATIO",
        "TRAILING_STOP_RATIO",
        "BREAK_EVEN_STOP_ENABLED",
        "OPPOSITE_SIGNAL_EXIT_ENABLED",
        "REGIME_CHANGE_EXIT_ENABLED",
        "BREAKOUT_RECLAIM_TOLERANCE_RATIO",
        "BREAKOUT_RECLAIM_CONFIRMATION_CANDLES",
        "BREAKOUT_RECLAIM_GRACE_MIN",
        "FEE_RATE_USED_FOR_ENTRY_GATE",
        "SLIPPAGE_BPS_USED_FOR_ENTRY_GATE",
        "ENTRY_COST_BASIS",
        "REQUIRED_BREAKOUT_DISTANCE_RATIO",
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
        "MIN_BREAKOUT_DISTANCE_RATIO",
        "ENTRY_EDGE_BUFFER_RATIO",
        "ENTRY_MODE",
        "CONFIRMATION_WINDOW_MIN",
        "CONFIRMATION_MIN_BREAKOUT_DISTANCE_RATIO",
        "CONFIRMATION_CLOSE_LOCATION_MIN",
        "CONFIRMATION_VOLUME_RATIO_MIN",
        "MAX_UPPER_WICK_RATIO",
        "MIN_BODY_RATIO",
        "PULLBACK_RATIO",
        "RETEST_WINDOW_MIN",
        "RETEST_MAX_DEPTH_RATIO",
        "RETEST_HOLD_CANDLES",
        "RETEST_REBOUND_RATIO",
        "ENTRY_TIME_FILTER_KST_ENABLED",
        "ENTRY_TIME_FILTER_KST_START_HOUR",
        "ENTRY_TIME_FILTER_KST_END_HOUR",
        "COOLDOWN_MIN",
        "MAX_TRADES_PER_DAY",
        "STRATEGY_EXIT_RULES",
        "STRATEGY_EXIT_STOP_LOSS_RATIO",
        "STRATEGY_EXIT_MAX_HOLDING_MIN",
        "TAKE_PROFIT_RATIO",
        "BREAKOUT_RECLAIM_TOLERANCE_RATIO",
        "BREAKOUT_RECLAIM_CONFIRMATION_CANDLES",
        "BREAKOUT_RECLAIM_GRACE_MIN",
    ),
    metadata_only_parameter_names=(
        "FEE_RATE_USED_FOR_ENTRY_GATE",
        "SLIPPAGE_BPS_USED_FOR_ENTRY_GATE",
        "ENTRY_COST_BASIS",
        "REQUIRED_BREAKOUT_DISTANCE_RATIO",
    ),
    research_only_parameter_names=(
        "TRAILING_STOP_RATIO",
        "BREAK_EVEN_STOP_ENABLED",
        "OPPOSITE_SIGNAL_EXIT_ENABLED",
        "REGIME_CHANGE_EXIT_ENABLED",
    ),
    default_parameters={
        "CHANNEL_BREAKOUT_RANGE_RATIO_MIN": 1.2,
        "CHANNEL_BREAKOUT_VOLUME_RATIO_MIN": 1.1,
        "CHANNEL_BREAKOUT_REGIME_FILTER_ENABLED": True,
        "MIN_BREAKOUT_DISTANCE_RATIO": 0.0,
        "ENTRY_EDGE_BUFFER_RATIO": 0.0,
        "ENTRY_MODE": "immediate_breakout",
        "CONFIRMATION_WINDOW_MIN": 0,
        "CONFIRMATION_MIN_BREAKOUT_DISTANCE_RATIO": 0.0,
        "CONFIRMATION_CLOSE_LOCATION_MIN": 0.0,
        "CONFIRMATION_VOLUME_RATIO_MIN": 0.0,
        "MAX_UPPER_WICK_RATIO": 1.0,
        "MIN_BODY_RATIO": 0.0,
        "PULLBACK_RATIO": 0.0,
        "RETEST_WINDOW_MIN": 0,
        "RETEST_MAX_DEPTH_RATIO": 0.0,
        "RETEST_HOLD_CANDLES": 1,
        "RETEST_REBOUND_RATIO": 0.0,
        "ENTRY_TIME_FILTER_KST_ENABLED": False,
        "ENTRY_TIME_FILTER_KST_START_HOUR": 0,
        "ENTRY_TIME_FILTER_KST_END_HOUR": 24,
        "COOLDOWN_MIN": 0,
        "MAX_TRADES_PER_DAY": 0,
        "STRATEGY_EXIT_RULES": "stop_loss,max_holding_time",
        "STRATEGY_EXIT_STOP_LOSS_RATIO": 0.01,
        "STRATEGY_EXIT_MAX_HOLDING_MIN": 30,
        "TAKE_PROFIT_RATIO": 0.0,
        "TRAILING_STOP_RATIO": 0.0,
        "BREAK_EVEN_STOP_ENABLED": False,
        "OPPOSITE_SIGNAL_EXIT_ENABLED": False,
        "REGIME_CHANGE_EXIT_ENABLED": False,
        "BREAKOUT_RECLAIM_TOLERANCE_RATIO": 0.0,
        "BREAKOUT_RECLAIM_CONFIRMATION_CANDLES": 1,
        "BREAKOUT_RECLAIM_GRACE_MIN": 0,
    },
    parameter_schema=(
        StrategyParameterSchema("CHANNEL_BREAKOUT_LOOKBACK", "int", required=True, min_value=2, unit="candles"),
        StrategyParameterSchema("CHANNEL_BREAKOUT_RANGE_WINDOW", "int", required=True, min_value=2, unit="candles"),
        StrategyParameterSchema("CHANNEL_BREAKOUT_RANGE_RATIO_MIN", "float", min_value=0.0, unit="range_ratio"),
        StrategyParameterSchema("CHANNEL_BREAKOUT_VOLUME_WINDOW", "int", required=True, min_value=2, unit="candles"),
        StrategyParameterSchema("CHANNEL_BREAKOUT_VOLUME_RATIO_MIN", "float", min_value=0.0, unit="volume_ratio"),
        StrategyParameterSchema("CHANNEL_BREAKOUT_REGIME_FILTER_ENABLED", "bool", unit="enabled_flag"),
        StrategyParameterSchema("MIN_BREAKOUT_DISTANCE_RATIO", "float", min_value=0.0, unit="price_ratio"),
        StrategyParameterSchema("ENTRY_EDGE_BUFFER_RATIO", "float", min_value=0.0, unit="edge_ratio"),
        StrategyParameterSchema(
            "ENTRY_MODE",
            "str",
            enum=SUPPORTED_ENTRY_MODE_VALUES,
            unit="entry_hypothesis",
        ),
        StrategyParameterSchema("CONFIRMATION_WINDOW_MIN", "int", min_value=0, unit="minutes"),
        StrategyParameterSchema(
            "CONFIRMATION_MIN_BREAKOUT_DISTANCE_RATIO",
            "float",
            min_value=0.0,
            unit="price_ratio",
        ),
        StrategyParameterSchema("CONFIRMATION_CLOSE_LOCATION_MIN", "float", min_value=0.0, unit="ratio"),
        StrategyParameterSchema("CONFIRMATION_VOLUME_RATIO_MIN", "float", min_value=0.0, unit="volume_ratio"),
        StrategyParameterSchema("MAX_UPPER_WICK_RATIO", "float", min_value=0.0, unit="ratio"),
        StrategyParameterSchema("MIN_BODY_RATIO", "float", min_value=0.0, unit="ratio"),
        StrategyParameterSchema("PULLBACK_RATIO", "float", min_value=0.0, unit="price_ratio"),
        StrategyParameterSchema("RETEST_WINDOW_MIN", "int", min_value=0, unit="minutes"),
        StrategyParameterSchema("RETEST_MAX_DEPTH_RATIO", "float", min_value=0.0, unit="price_ratio"),
        StrategyParameterSchema("RETEST_HOLD_CANDLES", "int", min_value=1, unit="candles"),
        StrategyParameterSchema("RETEST_REBOUND_RATIO", "float", min_value=0.0, unit="price_ratio"),
        StrategyParameterSchema("ENTRY_TIME_FILTER_KST_ENABLED", "bool", unit="enabled_flag"),
        StrategyParameterSchema("ENTRY_TIME_FILTER_KST_START_HOUR", "int", min_value=0, max_value=23, unit="kst_hour"),
        StrategyParameterSchema("ENTRY_TIME_FILTER_KST_END_HOUR", "int", min_value=1, max_value=24, unit="kst_hour"),
        StrategyParameterSchema("COOLDOWN_MIN", "int", min_value=0, unit="minutes"),
        StrategyParameterSchema("MAX_TRADES_PER_DAY", "int", min_value=0, unit="count"),
        StrategyParameterSchema("STRATEGY_EXIT_RULES", "str", unit="comma_separated_exit_rule_names"),
        StrategyParameterSchema("STRATEGY_EXIT_STOP_LOSS_RATIO", "float", min_value=0.0, unit="unrealized_pnl_ratio"),
        StrategyParameterSchema("STRATEGY_EXIT_MAX_HOLDING_MIN", "int", min_value=0, unit="minutes"),
        StrategyParameterSchema("TAKE_PROFIT_RATIO", "float", min_value=0.0, unit="unrealized_pnl_ratio"),
        StrategyParameterSchema(
            "TRAILING_STOP_RATIO",
            "float",
            min_value=0.0,
            unit="unrealized_pnl_ratio",
            runtime_bound=False,
            behavior_affecting=False,
        ),
        StrategyParameterSchema(
            "BREAK_EVEN_STOP_ENABLED",
            "bool",
            unit="enabled_flag",
            runtime_bound=False,
            behavior_affecting=False,
        ),
        StrategyParameterSchema(
            "OPPOSITE_SIGNAL_EXIT_ENABLED",
            "bool",
            unit="enabled_flag",
            runtime_bound=False,
            behavior_affecting=False,
        ),
        StrategyParameterSchema(
            "REGIME_CHANGE_EXIT_ENABLED",
            "bool",
            unit="enabled_flag",
            runtime_bound=False,
            behavior_affecting=False,
        ),
        StrategyParameterSchema("BREAKOUT_RECLAIM_TOLERANCE_RATIO", "float", min_value=0.0, unit="price_ratio"),
        StrategyParameterSchema("BREAKOUT_RECLAIM_CONFIRMATION_CANDLES", "int", min_value=1, unit="candles"),
        StrategyParameterSchema("BREAKOUT_RECLAIM_GRACE_MIN", "int", min_value=0, unit="minutes"),
    ),
    decision_contract_version="research_channel_breakout_decision_contract.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={
        "schema_version": 1,
        "rules": ("stop_loss", "take_profit", "max_holding_time"),
        "strategy_owned_rules": ("breakout_level_reclaim_failed",),
        "breakout_level_reclaim_failed": {
            "unit": "price_ratio",
            "default_tolerance_ratio": 0.0,
            "default_confirmation_candles": 1,
            "default_grace_min": 0,
            "evaluation_price_basis": "closed_candle_mark",
        },
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


@dataclass
class BreakoutPendingState:
    active: bool = False
    breakout_index: int = -1
    breakout_level: float = 0.0
    breakout_close: float = 0.0
    expires_at_index: int = -1
    state: str = "idle"
    hold_count: int = 0
    retest_low: float = 0.0


def materialize_channel_breakout_parameters(
    *,
    plugin: ResearchStrategyPlugin,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    context: BacktestRunContext | None = None,
) -> dict[str, Any]:
    del context
    _validate_supported_entry_mode(
        parameter_values.get("ENTRY_MODE", CHANNEL_BREAKOUT_SPEC.default_parameters.get("ENTRY_MODE"))
    )
    values = materialize_strategy_parameters(
        plugin.name,
        parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )
    values["FEE_RATE_USED_FOR_ENTRY_GATE"] = max(0.0, float(fee_rate))
    values["SLIPPAGE_BPS_USED_FOR_ENTRY_GATE"] = max(0.0, float(slippage_bps))
    values["ENTRY_COST_BASIS"] = ENTRY_COST_BASIS
    values["REQUIRED_BREAKOUT_DISTANCE_RATIO"] = _required_breakout_distance(
        parameter_values=values,
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
    _validate_entry_time_filter_kst_window(values)
    rules = _normalize_exit_rules(values.get("STRATEGY_EXIT_RULES") or "")
    unsupported = sorted(set(rules) - {"stop_loss", "take_profit", "max_holding_time"})
    if unsupported:
        raise StrategySpecError(
            "STRATEGY_EXIT_RULES contains unsupported rule(s): " + ",".join(unsupported)
        )
    _validate_supported_entry_mode(values.get("ENTRY_MODE"))
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
    if candles is None or closes is None or highs is None or lows is None or volumes is None:
        prepared = prepare_channel_breakout_context(dataset)
        candles = prepared.candles
        closes = prepared.closes
        highs = prepared.highs
        lows = prepared.lows
        volumes = prepared.volumes
    lookback = int(parameter_values["CHANNEL_BREAKOUT_LOOKBACK"])
    range_window = int(parameter_values["CHANNEL_BREAKOUT_RANGE_WINDOW"])
    volume_window = int(parameter_values["CHANNEL_BREAKOUT_VOLUME_WINDOW"])
    required_breakout_distance = _required_breakout_distance_from_materialized(parameter_values)
    entry_edge_buffer_ratio = float(parameter_values.get("ENTRY_EDGE_BUFFER_RATIO", 0.0))
    fee_rate_used_for_entry_gate = float(parameter_values.get("FEE_RATE_USED_FOR_ENTRY_GATE", 0.0))
    slippage_bps_used_for_entry_gate = float(parameter_values.get("SLIPPAGE_BPS_USED_FOR_ENTRY_GATE", 0.0))
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
            "required_breakout_distance": float(required_breakout_distance),
            "entry_edge_buffer_ratio": float(entry_edge_buffer_ratio),
            "fee_rate_used_for_entry_gate": float(fee_rate_used_for_entry_gate),
            "slippage_bps_used_for_entry_gate": float(slippage_bps_used_for_entry_gate),
            "entry_cost_basis": ENTRY_COST_BASIS,
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
                "entry_mode": str(parameter_values.get("ENTRY_MODE") or "immediate_breakout"),
                "required_breakout_distance": float(required_breakout_distance),
                "entry_cost_basis": ENTRY_COST_BASIS,
                "fee_rate_used_for_entry_gate": float(fee_rate_used_for_entry_gate),
                "slippage_bps_used_for_entry_gate": float(slippage_bps_used_for_entry_gate),
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
    if breakout_distance < required_breakout_distance:
        blocked_filters.append("breakout_distance_below_min")
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

    entry_mode = _validate_supported_entry_mode(parameter_values.get("ENTRY_MODE"))
    blocked = tuple(blocked_filters)
    signal = "BUY" if entry_mode == "immediate_breakout" and not blocked else "HOLD"
    confirmation_status = "not_applicable"
    reason = "channel_breakout_confirmed" if signal == "BUY" else "channel_breakout_blocked"
    if entry_mode == "delayed_confirmation":
        confirmation_status = "candidate" if not blocked else "blocked"
        reason = "breakout_pending_confirmation" if not blocked else "channel_breakout_blocked"
    feature_snapshot = {
        "schema_version": 1,
        "candle_index": int(candle_index),
        "close": close,
        "rolling_high": float(rolling_high),
        "breakout_distance": float(breakout_distance),
        "required_breakout_distance": float(required_breakout_distance),
        "entry_edge_buffer_ratio": float(entry_edge_buffer_ratio),
        "fee_rate_used_for_entry_gate": float(fee_rate_used_for_entry_gate),
        "slippage_bps_used_for_entry_gate": float(slippage_bps_used_for_entry_gate),
        "entry_cost_basis": ENTRY_COST_BASIS,
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
    if entry_mode in {"delayed_confirmation", "retest_hold_after_breakout"}:
        feature_snapshot.update(
            {
                "entry_mode": entry_mode,
                "breakout_candidate": not blocked,
                "breakout_pending": not blocked,
                "breakout_level": float(rolling_high) if not blocked else 0.0,
                "breakout_index": int(candle_index) if not blocked else -1,
                "confirmation_window_min": int(parameter_values["CONFIRMATION_WINDOW_MIN"]),
                "pending_expires_at_index": (
                    int(candle_index) + int(parameter_values["CONFIRMATION_WINDOW_MIN"])
                    if not blocked
                    else -1
                ),
                "confirmation_status": confirmation_status,
            }
        )
    if entry_mode == "retest_hold_after_breakout":
        feature_snapshot.update(
            {
                "retest_state": "breakout_candidate" if not blocked else "idle",
                "retest_window_min": int(parameter_values["RETEST_WINDOW_MIN"]),
                "retest_max_depth_ratio": float(parameter_values["RETEST_MAX_DEPTH_RATIO"]),
                "retest_hold_candles": int(parameter_values["RETEST_HOLD_CANDLES"]),
                "retest_rebound_ratio": float(parameter_values["RETEST_REBOUND_RATIO"]),
                "retest_failure_reason": "",
            }
        )
    decision = {
        "signal": signal,
        "reason": reason,
        "feature_snapshot": feature_snapshot,
        "strategy_diagnostics": {
            "schema_version": 1,
            "blocked_filters": blocked,
            "regime_filter_enabled": regime_filter_enabled,
            "entry_mode": entry_mode,
            "confirmation_status": confirmation_status,
            "breakout_distance": float(breakout_distance),
            "required_breakout_distance": float(required_breakout_distance),
            "entry_cost_basis": ENTRY_COST_BASIS,
            "entry_edge_buffer_ratio": float(entry_edge_buffer_ratio),
            "fee_rate_used_for_entry_gate": float(fee_rate_used_for_entry_gate),
            "slippage_bps_used_for_entry_gate": float(slippage_bps_used_for_entry_gate),
        },
    }
    if signal == "BUY":
        decision["order_intent"] = {
            "side": "BUY",
            "sizing": "portfolio_policy_fractional_cash",
            "entry_breakout_level": float(rolling_high),
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
) -> Iterator[ResearchDecisionEvent]:
    del portfolio_policy, context
    parameter_values = {
        **dict(parameter_values),
        "FEE_RATE_USED_FOR_ENTRY_GATE": max(0.0, float(fee_rate)),
        "SLIPPAGE_BPS_USED_FOR_ENTRY_GATE": max(0.0, float(slippage_bps)),
        "ENTRY_COST_BASIS": ENTRY_COST_BASIS,
    }
    parameter_values["REQUIRED_BREAKOUT_DISTANCE_RATIO"] = _required_breakout_distance(
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )
    prepared = prepare_channel_breakout_context(dataset)
    candles = prepared.candles
    entry_mode = _validate_supported_entry_mode(parameter_values.get("ENTRY_MODE"))
    pending = BreakoutPendingState()
    last_buy_index: int | None = None
    active_entry_breakout_level: float = 0.0
    trade_count_by_day: dict[str, int] = {}
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
        if entry_mode == "delayed_confirmation":
            decision = _apply_delayed_confirmation_state(
                decision=decision,
                candle=candle,
                candle_index=candle_index,
                parameter_values=parameter_values,
                pending=pending,
            )
        elif entry_mode == "retest_hold_after_breakout":
            decision = _apply_retest_hold_state(
                decision=decision,
                candle=candle,
                candle_index=candle_index,
                parameter_values=parameter_values,
                pending=pending,
            )
        decision_ts = candle_close_ts(candle, interval=dataset.interval) + int(
            execution_timing_policy.decision_guard_ms
        )
        decision = _apply_buy_limits(
            decision=decision,
            candle=candle,
            candle_index=candle_index,
            decision_ts_ms=int(decision_ts),
            parameter_values=parameter_values,
            last_buy_index=last_buy_index,
            trade_count_by_day=trade_count_by_day,
        )
        signal = str(decision.get("signal") or "HOLD").upper()
        feature_snapshot = dict(decision.get("feature_snapshot") or {})
        if signal == "BUY":
            active_entry_breakout_level = float(
                (decision.get("order_intent") or {}).get("entry_breakout_level")
                or feature_snapshot.get("breakout_level")
                or feature_snapshot.get("rolling_high")
                or 0.0
            )
        if active_entry_breakout_level > 0.0:
            feature_snapshot["entry_breakout_level"] = float(active_entry_breakout_level)
        blocked_filters = tuple(str(item) for item in feature_snapshot.get("blocked_filters") or ())
        yield ResearchDecisionEvent(
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
        if signal == "BUY":
            last_buy_index = int(candle_index)
            day_key = _candle_utc_day_key(candle)
            trade_count_by_day[day_key] = trade_count_by_day.get(day_key, 0) + 1


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0.0:
        return 0.0
    return float(numerator) / float(denominator)


@dataclass(frozen=True)
class BreakoutLevelReclaimExitRule:
    tolerance_ratio: float = 0.0
    confirmation_candles: int = 1
    grace_min: int = 0
    name: str = "breakout_level_reclaim_failed"
    _breach_count: int = 0

    def evaluate(
        self,
        *,
        position: PositionContext,
        candle_ts: int,
        market_price: float,
        signal_context: dict[str, object],
    ) -> ExitRuleDecision:
        raw_level = signal_context.get("entry_breakout_level")
        try:
            entry_breakout_level = float(raw_level)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            entry_breakout_level = 0.0
        tolerance = max(0.0, float(self.tolerance_ratio))
        confirmation_candles = max(1, int(self.confirmation_candles))
        grace_ms = max(0, int(self.grace_min)) * 60_000
        entry_ts = int(position.entry_ts) if position.entry_ts is not None else None
        in_grace = bool(entry_ts is not None and int(candle_ts) - entry_ts < grace_ms)
        threshold = entry_breakout_level * (1.0 - tolerance)
        breached = bool(position.in_position and entry_breakout_level > 0.0 and float(market_price) < threshold)
        next_breach_count = 0 if in_grace or not breached else int(self._breach_count) + 1
        object.__setattr__(self, "_breach_count", next_breach_count)
        should_exit = bool(not in_grace and breached and next_breach_count >= confirmation_candles)
        return ExitRuleDecision(
            should_exit=should_exit,
            reason=(
                "exit by breakout level reclaim failure"
                if should_exit
                else "breakout level reclaim not failed"
            ),
            context={
                "rule": self.name,
                "entry_breakout_level": entry_breakout_level,
                "tolerance_ratio": tolerance,
                "confirmation_candles": confirmation_candles,
                "breach_count": next_breach_count,
                "grace_min": max(0, int(self.grace_min)),
                "in_grace_period": in_grace,
                "threshold_price": threshold,
                "market_price": float(market_price),
                "candle_ts": int(candle_ts),
            },
        )


def channel_breakout_exit_rule_factory(
    _active_exit_policy: dict[str, Any],
    _parameter_values: dict[str, Any],
    _fee_rate: float,
) -> list[BreakoutLevelReclaimExitRule]:
    return [
        BreakoutLevelReclaimExitRule(
            tolerance_ratio=max(0.0, float(_parameter_values.get("BREAKOUT_RECLAIM_TOLERANCE_RATIO", 0.0))),
            confirmation_candles=max(1, int(_parameter_values.get("BREAKOUT_RECLAIM_CONFIRMATION_CANDLES", 1))),
            grace_min=max(0, int(_parameter_values.get("BREAKOUT_RECLAIM_GRACE_MIN", 0))),
        )
    ]


def channel_breakout_exit_signal_context(event: ResearchDecisionEvent) -> dict[str, object]:
    feature_snapshot = dict(getattr(event, "feature_snapshot", None) or {})
    order_intent = dict(getattr(event, "order_intent", None) or {})
    level = order_intent.get("entry_breakout_level") or feature_snapshot.get("entry_breakout_level")
    if not level:
        level = feature_snapshot.get("breakout_level") or feature_snapshot.get("rolling_high")
    return {"entry_breakout_level": float(level or 0.0)}


def _required_breakout_distance(
    *,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
) -> float:
    min_breakout = max(0.0, float(parameter_values.get("MIN_BREAKOUT_DISTANCE_RATIO", 0.0)))
    buffer = max(0.0, float(parameter_values.get("ENTRY_EDGE_BUFFER_RATIO", 0.0)))
    entry_cost_estimate = (2.0 * max(0.0, float(fee_rate))) + (
        2.0 * max(0.0, float(slippage_bps)) / 10_000.0
    )
    return max(min_breakout, entry_cost_estimate + buffer)


def _required_breakout_distance_from_materialized(parameter_values: dict[str, Any]) -> float:
    if "REQUIRED_BREAKOUT_DISTANCE_RATIO" in parameter_values:
        return max(0.0, float(parameter_values["REQUIRED_BREAKOUT_DISTANCE_RATIO"]))
    return _required_breakout_distance(
        parameter_values=parameter_values,
        fee_rate=float(parameter_values.get("FEE_RATE_USED_FOR_ENTRY_GATE", 0.0)),
        slippage_bps=float(parameter_values.get("SLIPPAGE_BPS_USED_FOR_ENTRY_GATE", 0.0)),
    )


def _normalize_exit_rules(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, str):
        raise StrategySpecError("STRATEGY_EXIT_RULES must be str")
    return tuple(token.strip().lower() for token in raw.split(",") if token.strip())


def _validate_supported_entry_mode(raw: object) -> str:
    entry_mode = str(raw or "immediate_breakout").strip()
    if entry_mode not in _SUPPORTED_ENTRY_MODES:
        raise StrategySpecError(
            "ENTRY_MODE unsupported for channel_breakout_with_regime_filter: "
            f"{entry_mode}; supported entry modes: {','.join(sorted(_SUPPORTED_ENTRY_MODES))}"
        )
    return entry_mode


def _validate_entry_time_filter_kst_window(parameter_values: dict[str, Any]) -> None:
    start_hour = int(parameter_values.get("ENTRY_TIME_FILTER_KST_START_HOUR", 0))
    end_hour = int(parameter_values.get("ENTRY_TIME_FILTER_KST_END_HOUR", 24))
    if end_hour <= start_hour:
        raise StrategySpecError("ENTRY_TIME_FILTER_KST_END_HOUR must be greater than start hour")


def _kst_hour_from_decision_ts_ms(decision_ts_ms: int) -> int:
    return int(gmtime((int(decision_ts_ms) / 1000.0) + (9 * 3600)).tm_hour)


def _apply_delayed_confirmation_state(
    *,
    decision: dict[str, Any],
    candle: Candle,
    candle_index: int,
    parameter_values: dict[str, Any],
    pending: BreakoutPendingState,
) -> dict[str, Any]:
    if pending.active and int(candle_index) > pending.breakout_index:
        return _evaluate_pending_confirmation(
            decision=decision,
            candle=candle,
            candle_index=candle_index,
            parameter_values=parameter_values,
            pending=pending,
        )
    feature_snapshot = dict(decision.get("feature_snapshot") or {})
    if bool(feature_snapshot.get("breakout_candidate")):
        pending.active = True
        pending.breakout_index = int(candle_index)
        pending.breakout_level = float(feature_snapshot["breakout_level"])
        pending.breakout_close = float(feature_snapshot["close"])
        pending.expires_at_index = int(feature_snapshot["pending_expires_at_index"])
    return decision


def _evaluate_pending_confirmation(
    *,
    decision: dict[str, Any],
    candle: Candle,
    candle_index: int,
    parameter_values: dict[str, Any],
    pending: BreakoutPendingState,
) -> dict[str, Any]:
    if int(candle_index) > int(pending.expires_at_index):
        return _delayed_confirmation_decision(
            base_decision=decision,
            pending=pending,
            signal="HOLD",
            reason="breakout_confirmation_expired",
            confirmation_status="expired",
            clear_pending=True,
        )
    close = float(candle.close)
    low = float(candle.low)
    pullback_ratio = float(parameter_values["PULLBACK_RATIO"])
    quality = compute_confirmation_quality_features(
        candle=candle,
        breakout_level=float(pending.breakout_level),
        avg_volume=float((decision.get("feature_snapshot") or {}).get("avg_volume") or 0.0),
    )
    if close <= pending.breakout_level:
        return _delayed_confirmation_decision(
            base_decision=decision,
            pending=pending,
            signal="HOLD",
            reason="breakout_confirmation_failed_close_below_level",
            confirmation_status="failed_close_below_level",
            clear_pending=True,
            quality_features=quality,
        )
    if low < pending.breakout_level * (1.0 - pullback_ratio):
        return _delayed_confirmation_decision(
            base_decision=decision,
            pending=pending,
            signal="HOLD",
            reason="breakout_confirmation_failed_deep_retest",
            confirmation_status="failed_deep_retest",
            clear_pending=True,
            quality_features=quality,
        )
    blocked_filters = tuple(str(item) for item in (decision.get("feature_snapshot") or {}).get("blocked_filters") or ())
    if "downtrend_regime" in blocked_filters or "chop_regime" in blocked_filters:
        return _delayed_confirmation_decision(
            base_decision=decision,
            pending=pending,
            signal="HOLD",
            reason="breakout_confirmation_failed_regime",
            confirmation_status="failed_regime",
            clear_pending=True,
            quality_features=quality,
        )
    quality_failure = _confirmation_quality_failure(parameter_values=parameter_values, quality=quality)
    if quality_failure is not None:
        return _delayed_confirmation_decision(
            base_decision=decision,
            pending=pending,
            signal="HOLD",
            reason=quality_failure,
            confirmation_status=quality_failure.removeprefix("breakout_confirmation_"),
            clear_pending=True,
            quality_features=quality,
        )
    return _delayed_confirmation_decision(
        base_decision=decision,
        pending=pending,
        signal="BUY",
        reason="delayed_breakout_confirmed",
        confirmation_status="confirmed",
        clear_pending=True,
        quality_features=quality,
    )


def _apply_retest_hold_state(
    *,
    decision: dict[str, Any],
    candle: Candle,
    candle_index: int,
    parameter_values: dict[str, Any],
    pending: BreakoutPendingState,
) -> dict[str, Any]:
    if pending.active and int(candle_index) > pending.breakout_index:
        return _evaluate_retest_hold(
            decision=decision,
            candle=candle,
            candle_index=candle_index,
            parameter_values=parameter_values,
            pending=pending,
        )
    feature_snapshot = dict(decision.get("feature_snapshot") or {})
    if bool(feature_snapshot.get("breakout_candidate")):
        pending.active = True
        pending.breakout_index = int(candle_index)
        pending.breakout_level = float(feature_snapshot["breakout_level"])
        pending.breakout_close = float(feature_snapshot["close"])
        pending.expires_at_index = int(candle_index) + int(parameter_values["RETEST_WINDOW_MIN"])
        pending.state = "waiting_for_retest"
        pending.hold_count = 0
        pending.retest_low = 0.0
        return _retest_hold_decision(
            base_decision=decision,
            pending=pending,
            signal="HOLD",
            reason="breakout_waiting_for_retest",
            retest_state="waiting_for_retest",
            clear_pending=False,
        )
    return decision


def _evaluate_retest_hold(
    *,
    decision: dict[str, Any],
    candle: Candle,
    candle_index: int,
    parameter_values: dict[str, Any],
    pending: BreakoutPendingState,
) -> dict[str, Any]:
    if int(candle_index) > int(pending.expires_at_index):
        return _retest_hold_decision(
            base_decision=decision,
            pending=pending,
            signal="HOLD",
            reason="retest_hold_after_breakout_expired",
            retest_state="expired",
            clear_pending=True,
            failure_reason="expired",
        )
    low = float(candle.low)
    close = float(candle.close)
    level = float(pending.breakout_level)
    max_depth_ratio = float(parameter_values["RETEST_MAX_DEPTH_RATIO"])
    if low < level * (1.0 - max_depth_ratio):
        return _retest_hold_decision(
            base_decision=decision,
            pending=pending,
            signal="HOLD",
            reason="retest_hold_after_breakout_failed_deep_retest",
            retest_state="failed_deep_retest",
            clear_pending=True,
            failure_reason="failed_deep_retest",
        )
    if close < level:
        return _retest_hold_decision(
            base_decision=decision,
            pending=pending,
            signal="HOLD",
            reason="retest_hold_after_breakout_failed_hold_above_level",
            retest_state="failed_hold_above_level",
            clear_pending=True,
            failure_reason="failed_hold_above_level",
        )
    retest_observed = low <= level and close >= level
    if retest_observed or pending.state in {"retest_observed", "hold_above_level_confirmed"}:
        pending.state = "retest_observed"
        pending.retest_low = low if pending.retest_low <= 0.0 else min(float(pending.retest_low), low)
        pending.hold_count += 1
        required_holds = max(1, int(parameter_values["RETEST_HOLD_CANDLES"]))
        if pending.hold_count < required_holds:
            return _retest_hold_decision(
                base_decision=decision,
                pending=pending,
                signal="HOLD",
                reason="retest_hold_after_breakout_holding_above_level",
                retest_state="retest_observed",
                clear_pending=False,
            )
        pending.state = "hold_above_level_confirmed"
        rebound_ratio = _safe_ratio(close - level, level)
        if rebound_ratio < float(parameter_values["RETEST_REBOUND_RATIO"]):
            return _retest_hold_decision(
                base_decision=decision,
                pending=pending,
                signal="HOLD",
                reason="retest_hold_after_breakout_waiting_for_rebound",
                retest_state="hold_above_level_confirmed",
                clear_pending=False,
            )
        return _retest_hold_decision(
            base_decision=decision,
            pending=pending,
            signal="BUY",
            reason="retest_hold_after_breakout_confirmed",
            retest_state="rebound_confirmed",
            clear_pending=True,
        )
    return _retest_hold_decision(
        base_decision=decision,
        pending=pending,
        signal="HOLD",
        reason="breakout_waiting_for_retest",
        retest_state="waiting_for_retest",
        clear_pending=False,
    )


def _retest_hold_decision(
    *,
    base_decision: dict[str, Any],
    pending: BreakoutPendingState,
    signal: str,
    reason: str,
    retest_state: str,
    clear_pending: bool,
    failure_reason: str = "",
) -> dict[str, Any]:
    decision = dict(base_decision)
    feature_snapshot = dict(decision.get("feature_snapshot") or {})
    feature_snapshot.update(
        {
            "entry_mode": "retest_hold_after_breakout",
            "breakout_candidate": False,
            "breakout_pending": pending.active and not clear_pending,
            "breakout_level": float(pending.breakout_level),
            "breakout_index": int(pending.breakout_index),
            "pending_expires_at_index": int(pending.expires_at_index),
            "retest_state": retest_state,
            "retest_hold_count": int(pending.hold_count),
            "retest_low": float(pending.retest_low),
            "retest_failure_reason": str(failure_reason),
            "confirmation_status": retest_state,
        }
    )
    diagnostics = dict(decision.get("strategy_diagnostics") or {})
    diagnostics.update(
        {
            "entry_mode": "retest_hold_after_breakout",
            "retest_state": retest_state,
            "confirmation_status": retest_state,
        }
    )
    if failure_reason:
        diagnostics["retest_failure_reason"] = str(failure_reason)
    decision["signal"] = signal
    decision["reason"] = reason
    decision["feature_snapshot"] = feature_snapshot
    decision["strategy_diagnostics"] = diagnostics
    if signal == "BUY":
        decision["order_intent"] = {
            "side": "BUY",
            "sizing": "portfolio_policy_fractional_cash",
            "entry_breakout_level": float(pending.breakout_level),
        }
    else:
        decision.pop("order_intent", None)
    if clear_pending:
        pending.active = False
        pending.breakout_index = -1
        pending.breakout_level = 0.0
        pending.breakout_close = 0.0
        pending.expires_at_index = -1
        pending.state = "idle"
        pending.hold_count = 0
        pending.retest_low = 0.0
    return decision


def _delayed_confirmation_decision(
    *,
    base_decision: dict[str, Any],
    pending: BreakoutPendingState,
    signal: str,
    reason: str,
    confirmation_status: str,
    clear_pending: bool,
    quality_features: dict[str, float] | None = None,
) -> dict[str, Any]:
    decision = dict(base_decision)
    feature_snapshot = dict(decision.get("feature_snapshot") or {})
    feature_snapshot.update(
        {
            "entry_mode": "delayed_confirmation",
            "breakout_candidate": False,
            "breakout_pending": pending.active and not clear_pending,
            "breakout_level": float(pending.breakout_level),
            "breakout_index": int(pending.breakout_index),
            "confirmation_window_min": int(
                feature_snapshot.get("confirmation_window_min")
                if feature_snapshot.get("confirmation_window_min") is not None
                else max(0, int(pending.expires_at_index) - int(pending.breakout_index))
            ),
            "pending_expires_at_index": int(pending.expires_at_index),
            "confirmation_status": confirmation_status,
        }
    )
    if quality_features:
        feature_snapshot.update(quality_features)
    diagnostics = dict(decision.get("strategy_diagnostics") or {})
    diagnostics["entry_mode"] = "delayed_confirmation"
    diagnostics["confirmation_status"] = confirmation_status
    if quality_features:
        diagnostics.update(quality_features)
    if reason.startswith("breakout_confirmation_failed_"):
        diagnostics["confirmation_failure_reason"] = reason
    decision["signal"] = signal
    decision["reason"] = reason
    decision["feature_snapshot"] = feature_snapshot
    decision["strategy_diagnostics"] = diagnostics
    if signal == "BUY":
        decision["order_intent"] = {
            "side": "BUY",
            "sizing": "portfolio_policy_fractional_cash",
            "entry_breakout_level": float(pending.breakout_level),
        }
    else:
        decision.pop("order_intent", None)
    if clear_pending:
        pending.active = False
        pending.breakout_index = -1
        pending.breakout_level = 0.0
        pending.breakout_close = 0.0
        pending.expires_at_index = -1
        pending.state = "idle"
        pending.hold_count = 0
        pending.retest_low = 0.0
    return decision


def compute_confirmation_quality_features(
    *,
    candle: Candle,
    breakout_level: float,
    avg_volume: float,
) -> dict[str, float]:
    close = float(candle.close)
    high = float(candle.high)
    low = float(candle.low)
    open_ = float(candle.open)
    candle_range = max(0.0, high - low)
    return {
        "confirmation_breakout_distance": _safe_ratio(close - float(breakout_level), float(breakout_level)),
        "close_location": _safe_ratio(close - low, candle_range),
        "upper_wick_ratio": _safe_ratio(high - max(open_, close), candle_range),
        "body_ratio": _safe_ratio(abs(close - open_), candle_range),
        "confirmation_volume_ratio": _safe_ratio(float(candle.volume), float(avg_volume)),
    }


def _confirmation_quality_failure(
    *,
    parameter_values: dict[str, Any],
    quality: dict[str, float],
) -> str | None:
    if quality["confirmation_breakout_distance"] < float(
        parameter_values["CONFIRMATION_MIN_BREAKOUT_DISTANCE_RATIO"]
    ):
        return "breakout_confirmation_failed_distance"
    if quality["close_location"] < float(parameter_values["CONFIRMATION_CLOSE_LOCATION_MIN"]):
        return "breakout_confirmation_failed_close_location"
    max_upper_wick = float(parameter_values["MAX_UPPER_WICK_RATIO"])
    if max_upper_wick >= 0.0 and quality["upper_wick_ratio"] > max_upper_wick:
        return "breakout_confirmation_failed_upper_wick"
    if quality["body_ratio"] < float(parameter_values["MIN_BODY_RATIO"]):
        return "breakout_confirmation_failed_body"
    if quality["confirmation_volume_ratio"] < float(parameter_values["CONFIRMATION_VOLUME_RATIO_MIN"]):
        return "breakout_confirmation_failed_volume"
    return None


def _apply_buy_limits(
    *,
    decision: dict[str, Any],
    candle: Candle,
    candle_index: int,
    decision_ts_ms: int,
    parameter_values: dict[str, Any],
    last_buy_index: int | None,
    trade_count_by_day: dict[str, int],
) -> dict[str, Any]:
    decision = _with_entry_time_filter_kst_diagnostics(
        decision=decision,
        parameter_values=parameter_values,
        decision_ts_ms=decision_ts_ms,
    )
    if str(decision.get("signal") or "HOLD").upper() != "BUY":
        return decision
    if _entry_time_filter_kst_blocks(parameter_values=parameter_values, decision_ts_ms=decision_ts_ms):
        return _blocked_buy_limit_decision(decision=decision, reason="entry_time_filter_kst_blocked")
    cooldown_min = int(parameter_values["COOLDOWN_MIN"])
    if last_buy_index is not None and cooldown_min > 0 and int(candle_index) - int(last_buy_index) < cooldown_min:
        return _blocked_buy_limit_decision(decision=decision, reason="cooldown_active")
    max_trades_per_day = int(parameter_values["MAX_TRADES_PER_DAY"])
    day_key = _candle_utc_day_key(candle)
    if max_trades_per_day > 0 and trade_count_by_day.get(day_key, 0) >= max_trades_per_day:
        return _blocked_buy_limit_decision(decision=decision, reason="max_trades_per_day_reached")
    return decision


def _with_entry_time_filter_kst_diagnostics(
    *,
    decision: dict[str, Any],
    parameter_values: dict[str, Any],
    decision_ts_ms: int,
) -> dict[str, Any]:
    enabled = bool(parameter_values.get("ENTRY_TIME_FILTER_KST_ENABLED", False))
    start_hour = int(parameter_values.get("ENTRY_TIME_FILTER_KST_START_HOUR", 0))
    end_hour = int(parameter_values.get("ENTRY_TIME_FILTER_KST_END_HOUR", 24))
    entry_hour_kst = _kst_hour_from_decision_ts_ms(decision_ts_ms)
    updated = dict(decision)
    feature_snapshot = dict(updated.get("feature_snapshot") or {})
    diagnostics = dict(updated.get("strategy_diagnostics") or {})
    evidence = {
        "entry_time_filter_kst_enabled": enabled,
        "entry_time_filter_kst_start_hour": start_hour,
        "entry_time_filter_kst_end_hour": end_hour,
        "entry_hour_kst": entry_hour_kst,
    }
    feature_snapshot.update(evidence)
    diagnostics.update(evidence)
    updated["feature_snapshot"] = feature_snapshot
    updated["strategy_diagnostics"] = diagnostics
    return updated


def _entry_time_filter_kst_blocks(*, parameter_values: dict[str, Any], decision_ts_ms: int) -> bool:
    if not bool(parameter_values.get("ENTRY_TIME_FILTER_KST_ENABLED", False)):
        return False
    start_hour = int(parameter_values.get("ENTRY_TIME_FILTER_KST_START_HOUR", 0))
    end_hour = int(parameter_values.get("ENTRY_TIME_FILTER_KST_END_HOUR", 24))
    entry_hour_kst = _kst_hour_from_decision_ts_ms(decision_ts_ms)
    return not (start_hour <= entry_hour_kst < end_hour)


def _blocked_buy_limit_decision(*, decision: dict[str, Any], reason: str) -> dict[str, Any]:
    blocked = tuple(str(item) for item in (decision.get("feature_snapshot") or {}).get("blocked_filters") or ())
    blocked = (*blocked, reason)
    limited = dict(decision)
    feature_snapshot = dict(limited.get("feature_snapshot") or {})
    feature_snapshot["blocked_filters"] = blocked
    diagnostics = dict(limited.get("strategy_diagnostics") or {})
    diagnostics["blocked_filters"] = blocked
    limited["signal"] = "HOLD"
    limited["reason"] = "channel_breakout_blocked"
    limited["feature_snapshot"] = feature_snapshot
    limited["strategy_diagnostics"] = diagnostics
    limited.pop("order_intent", None)
    return limited


def _candle_utc_day_key(candle: Candle) -> str:
    return strftime("%Y-%m-%d", gmtime(int(candle.ts) // 1000))


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
object.__setattr__(
    CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN,
    "estimate_complexity",
    estimate_channel_breakout_complexity,
)
object.__setattr__(
    CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN,
    "exit_rule_factory",
    channel_breakout_exit_rule_factory,
)
object.__setattr__(
    CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN,
    "exit_signal_context_builder",
    channel_breakout_exit_signal_context,
)
