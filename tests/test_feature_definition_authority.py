from __future__ import annotations

from dataclasses import replace

import pytest

from market_research.builtin_strategies.buy_and_hold_baseline import (
    BUY_AND_HOLD_BASELINE_SPEC,
)
from market_research.builtin_strategies.noop_baseline import NOOP_BASELINE_SPEC
from market_research.builtin_strategies.sma_with_filter import SMA_WITH_FILTER_SPEC
from market_research.builtin_strategies.sma_with_filter_events import (
    build_sma_with_filter_research_events,
)
from market_research.builtin_strategies.sma_with_filter_features import (
    build_sma_feature_context,
    compute_sma_feature_values,
    feature_values_from_sma_snapshot,
)
from market_research.builtin_strategies.threshold_research_only import (
    THRESHOLD_RESEARCH_ONLY_SPEC,
)
from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.experiment_manifest import DateRange, ExecutionTimingPolicy
from market_research.research.feature_definition import (
    FeatureDefinition,
    FeatureDefinitionError,
)
from market_research.research.feature_diagnostic_features import AsOfCandleView
from market_research.research.feature_provider_registry import (
    feature_provider_spec_for_name,
    list_feature_provider_specs,
)
from market_research.research.forward_diagnostics import (
    run_forward_diagnostics_on_snapshot,
)
from market_research.research.strategy_spec import StrategyFeatureDefinition
from market_research.strategy_sdk import FeatureDefinition as SdkFeatureDefinition


def _dataset() -> DatasetSnapshot:
    closes = (100.0, 101.0, 99.0, 103.0, 98.0, 105.0)
    return DatasetSnapshot(
        "feature-authority",
        "fixture",
        "KRW-BTC",
        "1m",
        "validation",
        DateRange("2026-01-01", "2026-01-01"),
        tuple(
            Candle(index * 60_000, close, close + 1, close - 1, close, 1.0)
            for index, close in enumerate(closes)
        ),
    )


def _parameters() -> dict[str, object]:
    values = dict(SMA_WITH_FILTER_SPEC.default_parameters)
    values.update(
        {
            "SMA_SHORT": 2,
            "SMA_LONG": 3,
            "SMA_FILTER_VOL_WINDOW": 3,
            "SMA_FILTER_OVEREXT_LOOKBACK": 2,
        }
    )
    return values


def _legacy_sma_feature_values(
    closes: tuple[float, ...], index: int, parameters: dict[str, object]
) -> dict[str, float]:
    short_window = int(parameters["SMA_SHORT"])
    long_window = int(parameters["SMA_LONG"])

    def sma(end: int, window: int) -> float:
        return sum(closes[end - window : end]) / float(window)

    previous_short = sma(index, short_window)
    previous_long = sma(index, long_window)
    short = sma(index + 1, short_window)
    long = sma(index + 1, long_window)
    volatility_window = max(
        1, int(parameters.get("SMA_FILTER_VOL_WINDOW") or 10)
    )
    window_values = closes[
        max(0, index + 1 - volatility_window) : index + 1
    ]
    mean = sum(window_values) / len(window_values)
    overextension_lookback = max(
        1, int(parameters.get("SMA_FILTER_OVEREXT_LOOKBACK") or 3)
    )
    base = closes[max(0, index - overextension_lookback)]
    return {
        "close": closes[index],
        "short_sma": short,
        "long_sma": long,
        "prev_short_sma": previous_short,
        "prev_long_sma": previous_long,
        "gap_ratio": abs((short - long) / long) if long else 0.0,
        "volatility_ratio": (
            (max(window_values) - min(window_values)) / mean if mean else 0.0
        ),
        "overextended_ratio": (
            abs((closes[index] - base) / base) if base else 0.0
        ),
    }


def test_strategy_and_diagnostic_features_share_versioned_authority() -> None:
    assert StrategyFeatureDefinition is FeatureDefinition
    assert SdkFeatureDefinition is FeatureDefinition
    definitions = (
        *BUY_AND_HOLD_BASELINE_SPEC.feature_definitions,
        *NOOP_BASELINE_SPEC.feature_definitions,
        *SMA_WITH_FILTER_SPEC.feature_definitions,
        *THRESHOLD_RESEARCH_ONLY_SPEC.feature_definitions,
        *(spec.definition for spec in list_feature_provider_specs()),
    )

    for definition in definitions:
        payload = definition.as_dict()
        assert payload["schema_version"] == 1
        assert payload["feature_id"]
        assert payload["version"]
        assert payload["inputs"]
        assert payload["formula"]
        assert str(payload["implementation_code_hash"]).startswith("sha256:")
        assert str(payload["definition_hash"]).startswith("sha256:")
        assert payload["warm_up"]
        assert payload["current_bar_rule"]
        assert payload["availability_lag_ms"] == 0
        assert payload["missing_policy"]
        assert payload["outlier_policy"]
        assert payload["unit"] != "unspecified"
        assert payload["leakage_risk"]
        assert payload["consumers"]


def test_code_change_rejects_stale_hash_and_changes_versioned_definition_hash() -> None:
    def original(*, value: float) -> float:
        return value

    def changed(*, value: float) -> float:
        return value + 1.0

    definition = FeatureDefinition(
        name="probe",
        description="Code-binding probe.",
        source_data=("probe.value",),
        calculation="value",
        feature_id="test.probe",
        version="1.0.0",
        value_type="float",
        unit="ratio",
        consumers=("test",),
        calculator=original,
    )

    with pytest.raises(
        FeatureDefinitionError, match="feature_definition_code_hash_mismatch"
    ):
        replace(definition, calculator=changed)

    changed_definition = replace(
        definition,
        version="1.0.1",
        calculator=changed,
        implementation_code_hash="",
    )
    assert changed_definition.implementation_code_hash != (
        definition.implementation_code_hash
    )
    assert changed_definition.definition_hash != definition.definition_hash


def test_sma_runtime_consumes_declared_feature_calculators_without_semantic_change() -> (
    None
):
    dataset = _dataset()
    parameters = _parameters()
    closes = tuple(float(candle.close) for candle in dataset.candles)
    events = build_sma_with_filter_research_events(
        dataset=dataset,
        parameter_values=parameters,
        fee_rate=0.0,
        slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="next_candle_open",
            allow_same_candle_close_fill=False,
        ),
    )

    declared_names = {item.name for item in SMA_WITH_FILTER_SPEC.feature_definitions}
    assert declared_names == {
        "close",
        "short_sma",
        "long_sma",
        "prev_short_sma",
        "prev_long_sma",
        "gap_ratio",
        "volatility_ratio",
        "overextended_ratio",
    }
    assert "range_ratio" not in declared_names

    for event in events:
        index = int(event.feature_snapshot["candle_index"])
        emitted = feature_values_from_sma_snapshot(event.feature_snapshot)
        authority_values = compute_sma_feature_values(
            context=build_sma_feature_context(
                closes=closes,
                index=index,
                parameter_values=parameters,
            )
        )
        assert emitted == authority_values
        assert emitted == _legacy_sma_feature_values(closes, index, parameters)


def test_sma_snapshot_rejects_undeclared_or_missing_feature_keys() -> None:
    valid = _legacy_sma_feature_values(
        tuple(float(candle.close) for candle in _dataset().candles),
        3,
        _parameters(),
    )
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "candle_index": 3,
        **valid,
    }
    feature_values_from_sma_snapshot(snapshot)

    with pytest.raises(
        FeatureDefinitionError, match="sma_feature_snapshot_definition_mismatch"
    ):
        feature_values_from_sma_snapshot({**snapshot, "range_ratio": 0.1})
    missing = dict(snapshot)
    missing.pop("volatility_ratio")
    with pytest.raises(
        FeatureDefinitionError, match="sma_feature_snapshot_definition_mismatch"
    ):
        feature_values_from_sma_snapshot(missing)


def test_diagnostic_runtime_computes_through_its_feature_definition() -> None:
    spec = feature_provider_spec_for_name("range_ratio")
    candles = _dataset().candles
    value = spec.compute(view=AsOfCandleView(candles=candles, index=2))

    assert value is not None
    assert value.name == spec.definition.name
    assert value.value == pytest.approx(
        (candles[2].high - candles[2].low) / candles[2].close
    )
    report = spec.as_report_dict()
    assert report["feature_id"] == "diagnostic.range_ratio"
    assert report["definition_hash"] == spec.definition.definition_hash
    assert report["implementation_kind"] == "callable_code"


def test_forward_diagnostics_persists_the_consumed_feature_authority() -> None:
    result = run_forward_diagnostics_on_snapshot(
        snapshot=_dataset(),
        feature_names=("range_ratio",),
        horizon_steps=(1,),
        bucket_method="quantile:2",
        min_bucket_count=1,
    )

    persisted = result.as_dict()["feature_provider_specs"][0]
    authority = feature_provider_spec_for_name("range_ratio").definition
    assert result.sample_count == 5
    assert persisted["feature_id"] == authority.feature_id
    assert persisted["version"] == authority.version
    assert persisted["definition_hash"] == authority.definition_hash
    assert persisted["implementation_code_hash"] == (
        authority.implementation_code_hash
    )
