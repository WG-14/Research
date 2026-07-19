from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, SupportsFloat, SupportsIndex, SupportsInt, cast

from market_research.research.feature_definition import (
    FeatureDefinition,
    FeatureDefinitionError,
    validate_computed_feature_value,
    validate_feature_definition_set,
)


@dataclass(frozen=True, slots=True)
class SmaFeatureContext:
    closes: tuple[float, ...]
    index: int
    short_window: int
    long_window: int
    volatility_window: int
    overextension_lookback: int


def _sma(values: tuple[float, ...], *, end: int, window: int) -> float:
    return sum(values[end - window : end]) / float(window)


def _close(*, context: SmaFeatureContext) -> float:
    return float(context.closes[context.index])


def _short_sma(*, context: SmaFeatureContext) -> float:
    return _sma(context.closes, end=context.index + 1, window=context.short_window)


def _long_sma(*, context: SmaFeatureContext) -> float:
    return _sma(context.closes, end=context.index + 1, window=context.long_window)


def _previous_short_sma(*, context: SmaFeatureContext) -> float:
    return _sma(context.closes, end=context.index, window=context.short_window)


def _previous_long_sma(*, context: SmaFeatureContext) -> float:
    return _sma(context.closes, end=context.index, window=context.long_window)


def _gap_ratio(*, context: SmaFeatureContext) -> float:
    short_sma = _short_sma(context=context)
    long_sma = _long_sma(context=context)
    return abs((short_sma - long_sma) / long_sma) if long_sma else 0.0


def _volatility_ratio(*, context: SmaFeatureContext) -> float:
    start = max(0, context.index + 1 - context.volatility_window)
    values = context.closes[start : context.index + 1]
    mean = sum(values) / len(values)
    return ((max(values) - min(values)) / mean) if mean else 0.0


def _overextended_ratio(*, context: SmaFeatureContext) -> float:
    base = context.closes[max(0, context.index - context.overextension_lookback)]
    close = context.closes[context.index]
    return abs((close - base) / base) if base else 0.0


_SIGNAL_CONSUMERS = (
    "sma_with_filter.signal",
    "sma_with_filter.runtime_evidence",
)
_FILTER_CONSUMERS = (
    "sma_with_filter.entry_filter",
    "sma_with_filter.runtime_evidence",
)


SMA_WITH_FILTER_FEATURE_DEFINITIONS = (
    FeatureDefinition(
        name="close",
        description="Current completed candle close used by SMA evidence and filters.",
        source_data=("candles.close",),
        calculation="close[index]",
        feature_id="sma_with_filter.close",
        version="1.0.0",
        value_type="float",
        warm_up_bars=1,
        current_bar_rule="completed_current_bar_inclusive",
        availability_lag_ms=0,
        missing_policy="reject_missing_or_non_finite_candle_before_runtime",
        outlier_policy="preserve_quality_gated_value",
        unit="price",
        leakage_risk="low_completed_bar_only",
        consumers=("sma_with_filter.runtime_evidence",),
        calculator=_close,
    ),
    FeatureDefinition(
        name="short_sma",
        description="Arithmetic mean of completed closes over the short window.",
        source_data=("candles.close",),
        calculation="mean(close[index + 1 - SMA_SHORT : index + 1])",
        lookback_parameter_names=("SMA_SHORT",),
        feature_id="sma_with_filter.short_sma",
        version="1.0.0",
        value_type="float",
        warm_up_bars=0,
        warm_up_parameter_names=("SMA_SHORT",),
        current_bar_rule="completed_current_bar_inclusive",
        availability_lag_ms=0,
        missing_policy="not_emitted_before_long_window_runtime_start",
        outlier_policy="preserve_quality_gated_value",
        unit="price",
        leakage_risk="low_completed_bar_only",
        consumers=_SIGNAL_CONSUMERS,
        calculator=_short_sma,
        implementation_dependencies=(_sma,),
    ),
    FeatureDefinition(
        name="long_sma",
        description="Arithmetic mean of completed closes over the long window.",
        source_data=("candles.close",),
        calculation="mean(close[index + 1 - SMA_LONG : index + 1])",
        lookback_parameter_names=("SMA_LONG",),
        feature_id="sma_with_filter.long_sma",
        version="1.0.0",
        value_type="float",
        warm_up_bars=0,
        warm_up_parameter_names=("SMA_LONG",),
        current_bar_rule="completed_current_bar_inclusive",
        availability_lag_ms=0,
        missing_policy="not_emitted_before_long_window_runtime_start",
        outlier_policy="preserve_quality_gated_value",
        unit="price",
        leakage_risk="low_completed_bar_only",
        consumers=_SIGNAL_CONSUMERS,
        calculator=_long_sma,
        implementation_dependencies=(_sma,),
    ),
    FeatureDefinition(
        name="prev_short_sma",
        description="Short-window mean ending at the prior completed candle.",
        source_data=("candles.close",),
        calculation="mean(close[index - SMA_SHORT : index])",
        lookback_parameter_names=("SMA_SHORT",),
        feature_id="sma_with_filter.prev_short_sma",
        version="1.0.0",
        value_type="float",
        warm_up_bars=1,
        warm_up_parameter_names=("SMA_SHORT",),
        current_bar_rule="prior_completed_bars_only",
        availability_lag_ms=0,
        missing_policy="not_emitted_before_long_window_runtime_start",
        outlier_policy="preserve_quality_gated_value",
        unit="price",
        leakage_risk="low_prior_bar_only",
        consumers=_SIGNAL_CONSUMERS,
        calculator=_previous_short_sma,
        implementation_dependencies=(_sma,),
    ),
    FeatureDefinition(
        name="prev_long_sma",
        description="Long-window mean ending at the prior completed candle.",
        source_data=("candles.close",),
        calculation="mean(close[index - SMA_LONG : index])",
        lookback_parameter_names=("SMA_LONG",),
        feature_id="sma_with_filter.prev_long_sma",
        version="1.0.0",
        value_type="float",
        warm_up_bars=1,
        warm_up_parameter_names=("SMA_LONG",),
        current_bar_rule="prior_completed_bars_only",
        availability_lag_ms=0,
        missing_policy="not_emitted_before_long_window_runtime_start",
        outlier_policy="preserve_quality_gated_value",
        unit="price",
        leakage_risk="low_prior_bar_only",
        consumers=_SIGNAL_CONSUMERS,
        calculator=_previous_long_sma,
        implementation_dependencies=(_sma,),
    ),
    FeatureDefinition(
        name="gap_ratio",
        description="Absolute relative distance between current short and long SMAs.",
        source_data=("feature.short_sma", "feature.long_sma"),
        calculation="abs((short_sma - long_sma) / long_sma) if long_sma else 0",
        lookback_parameter_names=("SMA_SHORT", "SMA_LONG"),
        feature_id="sma_with_filter.gap_ratio",
        version="1.0.0",
        value_type="float",
        warm_up_bars=0,
        warm_up_parameter_names=("SMA_SHORT", "SMA_LONG"),
        current_bar_rule="completed_current_bar_inclusive",
        availability_lag_ms=0,
        missing_policy="zero_only_when_long_sma_is_zero",
        outlier_policy="preserve_non_negative_ratio",
        unit="ratio",
        leakage_risk="low_completed_bar_only",
        consumers=(
            "sma_with_filter.entry_filter",
            "sma_with_filter.cost_edge_filter",
            "sma_with_filter.runtime_evidence",
        ),
        calculator=_gap_ratio,
        implementation_dependencies=(_short_sma, _long_sma, _sma),
    ),
    FeatureDefinition(
        name="volatility_ratio",
        description="Completed-close window range divided by its arithmetic mean.",
        source_data=("candles.close",),
        calculation=(
            "(max(close[max(0,index+1-window):index+1]) - "
            "min(close[max(0,index+1-window):index+1])) / "
            "mean(close[max(0,index+1-window):index+1])"
        ),
        lookback_parameter_names=("SMA_FILTER_VOL_WINDOW",),
        feature_id="sma_with_filter.volatility_ratio",
        version="1.0.0",
        value_type="float",
        warm_up_bars=1,
        warm_up_parameter_names=("SMA_FILTER_VOL_WINDOW",),
        current_bar_rule="completed_current_bar_inclusive",
        availability_lag_ms=0,
        missing_policy="window_is_clamped_to_available_completed_history",
        outlier_policy="preserve_non_negative_ratio",
        unit="ratio",
        leakage_risk="low_completed_bar_only",
        consumers=_FILTER_CONSUMERS,
        calculator=_volatility_ratio,
    ),
    FeatureDefinition(
        name="overextended_ratio",
        description="Absolute close return over the configured completed-bar lookback.",
        source_data=("candles.close",),
        calculation=(
            "abs((close[index] - close[max(0,index-lookback)]) / "
            "close[max(0,index-lookback)])"
        ),
        lookback_parameter_names=("SMA_FILTER_OVEREXT_LOOKBACK",),
        feature_id="sma_with_filter.overextended_ratio",
        version="1.0.0",
        value_type="float",
        warm_up_bars=1,
        warm_up_parameter_names=("SMA_FILTER_OVEREXT_LOOKBACK",),
        current_bar_rule="completed_current_bar_inclusive",
        availability_lag_ms=0,
        missing_policy="lookback_is_clamped_to_available_completed_history",
        outlier_policy="preserve_non_negative_ratio",
        unit="ratio",
        leakage_risk="low_completed_bar_only",
        consumers=_FILTER_CONSUMERS,
        calculator=_overextended_ratio,
    ),
)

validate_feature_definition_set(SMA_WITH_FILTER_FEATURE_DEFINITIONS)

_SNAPSHOT_METADATA_KEYS = frozenset(
    {"schema_version", "candle_index", "feature_snapshot_hash"}
)


def _as_int(value: object) -> int:
    return int(cast(str | bytes | bytearray | SupportsInt | SupportsIndex, value))


def _as_float(value: object) -> float:
    return float(cast(str | bytes | bytearray | SupportsFloat | SupportsIndex, value))


def build_sma_feature_context(
    *,
    closes: tuple[float, ...],
    index: int,
    parameter_values: Mapping[str, object],
) -> SmaFeatureContext:
    return SmaFeatureContext(
        closes=tuple(closes),
        index=int(index),
        short_window=_as_int(parameter_values["SMA_SHORT"]),
        long_window=_as_int(parameter_values["SMA_LONG"]),
        volatility_window=max(
            1, _as_int(parameter_values.get("SMA_FILTER_VOL_WINDOW") or 10)
        ),
        overextension_lookback=max(
            1, _as_int(parameter_values.get("SMA_FILTER_OVEREXT_LOOKBACK") or 3)
        ),
    )


def compute_sma_feature_values(
    *,
    context: SmaFeatureContext,
    definitions: tuple[FeatureDefinition, ...] = SMA_WITH_FILTER_FEATURE_DEFINITIONS,
) -> dict[str, float]:
    validate_feature_definition_set(definitions)
    values: dict[str, float] = {}
    for definition in definitions:
        value = definition.compute(context=context)
        validate_computed_feature_value(definition, value)
        values[definition.name] = _as_float(value)
    return values


def feature_values_from_sma_snapshot(
    snapshot: Mapping[str, object],
    *,
    definitions: tuple[FeatureDefinition, ...] = SMA_WITH_FILTER_FEATURE_DEFINITIONS,
) -> dict[str, float]:
    expected = {item.name for item in definitions}
    actual = set(snapshot) - _SNAPSHOT_METADATA_KEYS
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise FeatureDefinitionError(
            "sma_feature_snapshot_definition_mismatch:"
            f"missing={','.join(missing)}:unexpected={','.join(unexpected)}"
        )
    values = {name: snapshot[name] for name in expected}
    for definition in definitions:
        validate_computed_feature_value(definition, values[definition.name])
    return {name: _as_float(value) for name, value in values.items()}


__all__ = [
    "SMA_WITH_FILTER_FEATURE_DEFINITIONS",
    "SmaFeatureContext",
    "build_sma_feature_context",
    "compute_sma_feature_values",
    "feature_values_from_sma_snapshot",
]
