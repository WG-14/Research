from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from bithumb_bot.research.backtest_engine import BacktestRunContext
from bithumb_bot.research.backtest_kernel import run_decision_event_backtest
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.execution_timing import candle_close_ts
from bithumb_bot.research.experiment_manifest import DateRange, ExecutionTimingPolicy, legacy_research_portfolio_policy
from bithumb_bot.research.strategy_registry import resolve_research_strategy_plugin, strategy_runtime_capability_issues
from bithumb_bot.research.parameter_space import iter_parameter_candidates
from bithumb_bot.research.strategy_spec import StrategySpecError, validate_parameter_space_against_strategy_spec
from bithumb_bot.research.strategy_spec import exit_policy_from_parameters, exit_policy_hash
from bithumb_bot.strategy.base import PositionContext
from bithumb_bot.strategy_contract_testing import assert_research_only_contract
from bithumb_bot.strategy_plugin_inventory import build_strategy_plugin_inventory
from bithumb_bot.strategy_plugins.channel_breakout_research import (
    CHANNEL_BREAKOUT_COMPLEXITY_METADATA,
    CHANNEL_BREAKOUT_SPEC,
    CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN,
    SUPPORTED_ENTRY_MODE_VALUES,
    _candle_utc_day_key,
    _kst_hour_from_decision_ts_ms,
    build_channel_breakout_research_events,
    decide_channel_breakout_snapshot,
    estimate_channel_breakout_complexity,
    materialize_channel_breakout_parameters,
    prepare_channel_breakout_context,
)


def _dataset(candles: tuple[Candle, ...] | None = None) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id="channel_breakout_unit",
        source="unit",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=candles
        or (
            Candle(0, 100.0, 101.0, 99.0, 100.0, 100.0),
            Candle(60_000, 101.0, 102.0, 100.0, 101.0, 100.0),
            Candle(120_000, 102.0, 103.0, 101.0, 102.0, 100.0),
            Candle(180_000, 103.0, 104.0, 102.0, 103.0, 100.0),
            Candle(240_000, 104.0, 110.0, 103.0, 109.0, 160.0),
        ),
    )


def _params(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "CHANNEL_BREAKOUT_LOOKBACK": 3,
        "CHANNEL_BREAKOUT_RANGE_WINDOW": 3,
        "CHANNEL_BREAKOUT_VOLUME_WINDOW": 3,
        "CHANNEL_BREAKOUT_REGIME_FILTER_ENABLED": False,
    }
    values.update(overrides)
    return values


def _materialized(**overrides: object) -> dict[str, object]:
    return materialize_channel_breakout_parameters(
        plugin=CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN,
        parameter_values=_params(**overrides),
        fee_rate=0.001,
        slippage_bps=0.0,
    )


def _events(dataset: DatasetSnapshot, params: dict[str, object]):
    return tuple(
        build_channel_breakout_research_events(
            dataset=dataset,
            parameter_values=params,
            fee_rate=0.001,
            slippage_bps=0.0,
            execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
            portfolio_policy=legacy_research_portfolio_policy(),
        )
    )


def _events_with_guard(dataset: DatasetSnapshot, params: dict[str, object], *, decision_guard_ms: int):
    return tuple(
        build_channel_breakout_research_events(
            dataset=dataset,
            parameter_values=params,
            fee_rate=0.001,
            slippage_bps=0.0,
            execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=decision_guard_ms),
            portfolio_policy=legacy_research_portfolio_policy(),
        )
    )


def _utc_ms_for_kst_time(*, hour: int, minute: int = 0, second: int = 0) -> int:
    return int(((24 * 3600) + (hour * 3600) + (minute * 60) + second - (9 * 3600)) * 1000)


def _breakout_dataset_for_decision_ts(decision_ts_ms: int, *, decision_guard_ms: int = 0) -> DatasetSnapshot:
    final_candle_start = int(decision_ts_ms) - int(decision_guard_ms) - 60_000
    starts = tuple(final_candle_start - offset * 60_000 for offset in range(4, -1, -1))
    return _dataset(
        (
            Candle(starts[0], 100.0, 101.0, 99.0, 100.0, 100.0),
            Candle(starts[1], 101.0, 102.0, 100.0, 101.0, 100.0),
            Candle(starts[2], 102.0, 103.0, 101.0, 102.0, 100.0),
            Candle(starts[3], 103.0, 104.0, 102.0, 103.0, 100.0),
            Candle(starts[4], 104.0, 110.0, 103.0, 109.0, 160.0),
        )
    )


def _breakout_confirmation_dataset(
    *,
    confirmation_close: float = 108.0,
    confirmation_low: float = 104.0,
    extra_confirming_candle: bool = False,
) -> DatasetSnapshot:
    candles = (
        Candle(0, 100.0, 101.0, 99.0, 100.0, 100.0),
        Candle(60_000, 101.0, 102.0, 100.0, 101.0, 100.0),
        Candle(120_000, 102.0, 103.0, 101.0, 102.0, 100.0),
        Candle(180_000, 103.0, 104.0, 102.0, 103.0, 100.0),
        Candle(240_000, 104.0, 110.0, 103.0, 109.0, 160.0),
        Candle(300_000, 109.0, 109.5, confirmation_low, confirmation_close, 160.0),
    )
    if extra_confirming_candle:
        candles = (
            *candles,
            Candle(360_000, 108.0, 109.5, 104.0, 108.5, 160.0),
        )
    return _dataset(candles)


def _retest_hold_dataset(
    *,
    retest_low: float = 103.95,
    hold_close: float = 104.4,
    rebound_close: float = 105.2,
    include_rebound: bool = True,
) -> DatasetSnapshot:
    candles = (
        Candle(0, 100.0, 101.0, 99.0, 100.0, 100.0),
        Candle(60_000, 101.0, 102.0, 100.0, 101.0, 100.0),
        Candle(120_000, 102.0, 103.0, 101.0, 102.0, 100.0),
        Candle(180_000, 103.0, 104.0, 102.0, 103.0, 100.0),
        Candle(240_000, 104.0, 110.0, 103.0, 109.0, 180.0),
        Candle(300_000, 109.0, 109.5, retest_low, hold_close, 180.0),
    )
    if include_rebound:
        candles = (*candles, Candle(360_000, hold_close, 106.0, 104.1, rebound_close, 180.0))
    return _dataset(candles)


def _weak_breakout_dataset(*, close: float = 104.05, volume: float = 200.0) -> DatasetSnapshot:
    return _dataset(
        (
            Candle(0, 100.0, 101.0, 99.0, 100.0, 100.0),
            Candle(60_000, 101.0, 102.0, 100.0, 101.0, 100.0),
            Candle(120_000, 102.0, 103.0, 101.0, 102.0, 100.0),
            Candle(180_000, 103.0, 104.0, 102.0, 103.0, 100.0),
            Candle(240_000, 103.0, max(close, 104.2), 102.0, close, volume),
        )
    )


def _repeated_breakout_dataset(*, second_day: bool = False) -> DatasetSnapshot:
    base_second_ts = 86_400_000 if second_day else 300_000
    return _dataset(
        (
            Candle(0, 100.0, 101.0, 99.0, 100.0, 100.0),
            Candle(60_000, 101.0, 102.0, 100.0, 101.0, 100.0),
            Candle(120_000, 102.0, 103.0, 101.0, 102.0, 100.0),
            Candle(180_000, 103.0, 104.0, 102.0, 103.0, 100.0),
            Candle(240_000, 104.0, 109.0, 108.0, 109.0, 200.0),
            Candle(base_second_ts, 109.0, 110.0, 109.0, 110.0, 200.0),
            Candle(base_second_ts + 60_000, 110.0, 111.0, 110.0, 111.0, 200.0),
        )
    )


def test_level_1_research_only_contract() -> None:
    plugin = CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN

    assert_research_only_contract(plugin)
    assert plugin.runtime_replay_builder is None
    assert plugin.runtime_parameter_adapter is None
    assert plugin.runtime_decision_adapter_factory is None
    assert plugin.policy_assembly_factory is None
    assert plugin.runtime_capabilities.live_dry_run_allowed is False
    assert plugin.runtime_capabilities.live_real_order_allowed is False
    issues = strategy_runtime_capability_issues(
        plugin.name,
        live_dry_run=True,
        live_real_order_armed=True,
        require_promotion_runtime=True,
        require_runtime_replay=True,
        require_runtime_decision_adapter=True,
    )
    assert any(item.startswith("promotion_runtime_unsupported_for_strategy:channel_breakout_with_regime_filter") for item in issues)
    assert any(item.startswith("runtime_replay_unsupported_for_strategy:channel_breakout_with_regime_filter") for item in issues)
    assert any(item.startswith("runtime_decision_adapter_unsupported_for_strategy:channel_breakout_with_regime_filter") for item in issues)
    assert any(item.startswith("live_dry_run_not_allowed_for_strategy:channel_breakout_with_regime_filter") for item in issues)
    assert any(item.startswith("live_real_order_not_allowed_for_strategy:channel_breakout_with_regime_filter") for item in issues)


def test_discovery_reports_candle_only_plugin() -> None:
    plugin = resolve_research_strategy_plugin("channel_breakout_with_regime_filter")

    assert plugin is CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN
    assert plugin.required_data == ("candles",)
    assert plugin.optional_data == ()
    assert CHANNEL_BREAKOUT_SPEC.required_data == ("candles",)
    assert CHANNEL_BREAKOUT_SPEC.optional_data == ()


def test_inventory_reports_level_1_research_only_not_runtime_capable() -> None:
    inventory = build_strategy_plugin_inventory()
    item = next(
        record
        for record in inventory["strategies"]
        if record["strategy_name"] == "channel_breakout_with_regime_filter"
    )

    assert item["authoring_level"] == "level_1_research_only"
    assert item["capability_level"] == "research_only"
    assert item["runtime_replay_supported"] is False
    assert item["runtime_decision_supported"] is False
    assert item["live_dry_run_allowed"] is False
    assert item["live_real_order_allowed"] is False
    assert item["approved_profile_required"] is False
    assert item["runtime_data_requirements"]["required_data"] == ["candles"]
    assert item["runtime_data_requirements"]["optional_data"] == []


def test_channel_breakout_research_only_cannot_emit_live_profile() -> None:
    plugin = CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN

    assert plugin.runtime_capabilities.research_only is True
    assert plugin.runtime_capabilities.live_dry_run_allowed is False
    assert plugin.runtime_capabilities.live_real_order_allowed is False
    issues = strategy_runtime_capability_issues(
        plugin.name,
        live_dry_run=False,
        live_real_order_armed=False,
        require_promotion_runtime=True,
        require_runtime_replay=True,
        require_runtime_decision_adapter=True,
    )

    assert any(item.startswith("promotion_runtime_unsupported_for_strategy") for item in issues)
    assert any(item.startswith("runtime_replay_unsupported_for_strategy") for item in issues)
    assert any(item.startswith("runtime_decision_adapter_unsupported_for_strategy") for item in issues)


def test_channel_breakout_live_dry_run_rejected_without_promotion_extension() -> None:
    plugin = CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN

    assert plugin.runtime_replay_builder is None
    assert plugin.runtime_parameter_adapter is None
    assert plugin.runtime_decision_adapter_factory is None
    assert plugin.runtime_capabilities.live_dry_run_allowed is False
    issues = strategy_runtime_capability_issues(plugin.name, live_dry_run=True, live_real_order_armed=False)

    assert any(item.startswith("live_dry_run_not_allowed_for_strategy") for item in issues)


def test_buy_signal_uses_prior_rolling_high_and_order_intent() -> None:
    dataset = _dataset()
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    decision = decide_channel_breakout_snapshot(
        candle=dataset.candles[-1],
        candle_index=len(dataset.candles) - 1,
        dataset=dataset,
        parameter_values=params,
    )

    assert decision["signal"] == "BUY"
    assert decision["reason"] == "channel_breakout_confirmed"
    assert decision["order_intent"]["side"] == "BUY"
    assert decision["feature_snapshot"]["close"] > decision["feature_snapshot"]["rolling_high"]
    assert decision["feature_snapshot"]["blocked_filters"] == ()


def test_channel_breakout_existing_defaults_preserve_immediate_breakout_behavior() -> None:
    dataset = _dataset()
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    decision = decide_channel_breakout_snapshot(
        candle=dataset.candles[-1],
        candle_index=len(dataset.candles) - 1,
        dataset=dataset,
        parameter_values=params,
    )

    assert params["ENTRY_MODE"] == "immediate_breakout"
    assert decision["signal"] == "BUY"
    assert decision["strategy_diagnostics"]["entry_mode"] == "immediate_breakout"


def test_kst_entry_time_filter_parameters_are_in_strategy_spec() -> None:
    names = {
        "ENTRY_TIME_FILTER_KST_ENABLED",
        "ENTRY_TIME_FILTER_KST_START_HOUR",
        "ENTRY_TIME_FILTER_KST_END_HOUR",
    }
    schema_names = {schema.name for schema in CHANNEL_BREAKOUT_SPEC.parameter_schema}

    assert names <= set(CHANNEL_BREAKOUT_SPEC.accepted_parameter_names)
    assert names <= set(CHANNEL_BREAKOUT_SPEC.behavior_affecting_parameter_names)
    assert names <= set(CHANNEL_BREAKOUT_SPEC.default_parameters)
    assert names <= schema_names
    assert CHANNEL_BREAKOUT_SPEC.default_parameters["ENTRY_TIME_FILTER_KST_ENABLED"] is False
    assert CHANNEL_BREAKOUT_SPEC.default_parameters["ENTRY_TIME_FILTER_KST_START_HOUR"] == 0
    assert CHANNEL_BREAKOUT_SPEC.default_parameters["ENTRY_TIME_FILTER_KST_END_HOUR"] == 24


def test_kst_entry_time_filter_defaults_preserve_existing_behavior() -> None:
    dataset = _breakout_dataset_for_decision_ts(_utc_ms_for_kst_time(hour=10, minute=15))
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(dataset, params)[-1]

    assert params["ENTRY_TIME_FILTER_KST_ENABLED"] is False
    assert event.final_signal == "BUY"
    assert event.order_intent is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"ENTRY_TIME_FILTER_KST_START_HOUR": -1},
        {"ENTRY_TIME_FILTER_KST_START_HOUR": 24},
        {"ENTRY_TIME_FILTER_KST_END_HOUR": 0},
        {"ENTRY_TIME_FILTER_KST_END_HOUR": 25},
        {"ENTRY_TIME_FILTER_KST_START_HOUR": 10, "ENTRY_TIME_FILTER_KST_END_HOUR": 10},
    ],
)
def test_kst_entry_time_filter_rejects_invalid_hours(overrides: dict[str, object]) -> None:
    with pytest.raises(StrategySpecError):
        _materialized(**overrides)


def test_new_entry_mode_is_behavior_affecting_parameter() -> None:
    assert "ENTRY_MODE" in CHANNEL_BREAKOUT_SPEC.behavior_affecting_parameter_names


def test_channel_breakout_entry_mode_schema_matches_supported_modes() -> None:
    schema = next(item for item in CHANNEL_BREAKOUT_SPEC.parameter_schema if item.name == "ENTRY_MODE")

    assert schema.enum == SUPPORTED_ENTRY_MODE_VALUES
    assert set(schema.enum) == set(SUPPORTED_ENTRY_MODE_VALUES)


@pytest.mark.parametrize(
    "entry_mode",
    [
        "pullback_after_breakout",
        "contrarian_after_exhaustion",
    ],
)
def test_channel_breakout_unsupported_entry_mode_fails_fast(entry_mode: str) -> None:
    with pytest.raises(StrategySpecError, match="ENTRY_MODE unsupported"):
        _materialized(
            CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
            CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
            ENTRY_MODE=entry_mode,
        )


def test_delayed_confirmation_entry_mode_is_supported() -> None:
    values = _materialized(ENTRY_MODE="delayed_confirmation")

    assert values["ENTRY_MODE"] == "delayed_confirmation"


def test_unknown_entry_mode_fails_fast() -> None:
    with pytest.raises(StrategySpecError):
        _materialized(ENTRY_MODE="unknown_value")


def test_channel_breakout_complexity_reports_unsupported_entry_mode_consistently() -> None:
    with pytest.raises(StrategySpecError, match="ENTRY_MODE unsupported"):
        _materialized(ENTRY_MODE="unknown_value")

    estimate = estimate_channel_breakout_complexity(
        strategy_name=CHANNEL_BREAKOUT_SPEC.strategy_name,
        parameter_space={"ENTRY_MODE": ("unknown_value",)},
    )

    assert estimate["unsupported_parameter_values"] == {"ENTRY_MODE": ("unknown_value",)}
    assert "unsupported_entry_mode:unknown_value" in estimate["complexity_reasons"]


def test_channel_breakout_emits_diagnostic_count_defaults() -> None:
    plugin = CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN
    payload = {
        "raw_signal": "HOLD",
        "final_signal": "HOLD",
        "entry_signal": "HOLD",
        "blocked_filters": ("volume_ratio_below_min",),
        "strategy_diagnostics_namespace": plugin.diagnostics_namespace,
    }

    contract = plugin.diagnostics_count_builder(payload)

    defaults = contract["strategy_diagnostic_count_defaults"]
    assert defaults["raw_signal_count"] == 0
    assert defaults["blocked_filter_distribution.volume_ratio_below_min"] == 0


def test_channel_breakout_counts_blocked_filters() -> None:
    plugin = CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN
    payload = {
        "raw_signal": "HOLD",
        "final_signal": "HOLD",
        "entry_signal": "HOLD",
        "blocked_filters": ("volume_ratio_below_min", "downtrend_regime"),
        "strategy_diagnostics_namespace": plugin.diagnostics_namespace,
    }

    counts = plugin.diagnostics_count_builder(payload)["strategy_diagnostic_counts"]

    assert counts["blocked_filter_distribution.volume_ratio_below_min"] == 1
    assert counts["blocked_filter_distribution.downtrend_regime"] == 1


def test_volume_ratio_block_holds() -> None:
    dataset = _dataset()
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=100.0,
    )

    decision = decide_channel_breakout_snapshot(
        candle=dataset.candles[-1],
        candle_index=len(dataset.candles) - 1,
        dataset=dataset,
        parameter_values=params,
    )

    assert decision["signal"] == "HOLD"
    assert decision["reason"] == "channel_breakout_blocked"
    assert "volume_ratio_below_min" in decision["feature_snapshot"]["blocked_filters"]
    assert "order_intent" not in decision


def test_cost_edge_blocks_weak_breakout() -> None:
    dataset = _weak_breakout_dataset(close=104.05)
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
        MIN_BREAKOUT_DISTANCE_RATIO=0.001,
        ENTRY_EDGE_BUFFER_RATIO=0.0,
    )

    decision = decide_channel_breakout_snapshot(
        candle=dataset.candles[-1],
        candle_index=len(dataset.candles) - 1,
        dataset=dataset,
        parameter_values=params,
    )

    assert decision["signal"] == "HOLD"
    assert "breakout_distance_below_min" in decision["feature_snapshot"]["blocked_filters"]
    assert decision["feature_snapshot"]["breakout_distance"] < decision["feature_snapshot"]["required_breakout_distance"]


def test_cost_edge_allows_strong_breakout() -> None:
    dataset = _weak_breakout_dataset(close=105.0)
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
        MIN_BREAKOUT_DISTANCE_RATIO=0.001,
        ENTRY_EDGE_BUFFER_RATIO=0.0,
    )

    decision = decide_channel_breakout_snapshot(
        candle=dataset.candles[-1],
        candle_index=len(dataset.candles) - 1,
        dataset=dataset,
        parameter_values=params,
    )

    assert decision["signal"] == "BUY"
    assert "breakout_distance_below_min" not in decision["feature_snapshot"]["blocked_filters"]


def test_breakout_distance_below_min_is_reported_in_blocked_filters() -> None:
    dataset = _weak_breakout_dataset(close=104.05)
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
        MIN_BREAKOUT_DISTANCE_RATIO=0.001,
    )

    event = _events(dataset, params)[-1]

    assert event.final_signal == "HOLD"
    assert "breakout_distance_below_min" in event.blocked_filters
    assert event.feature_snapshot["required_breakout_distance"] == pytest.approx(0.002)


def test_fee_and_slippage_affect_required_breakout_distance() -> None:
    dataset = _weak_breakout_dataset(close=104.2)
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
        MIN_BREAKOUT_DISTANCE_RATIO=0.0,
        ENTRY_EDGE_BUFFER_RATIO=0.0005,
    )

    low_cost_event = tuple(
        build_channel_breakout_research_events(
            dataset=dataset,
            parameter_values=params,
            fee_rate=0.0001,
            slippage_bps=1.0,
            execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
            portfolio_policy=legacy_research_portfolio_policy(),
        )
    )[-1]
    high_cost_event = tuple(
        build_channel_breakout_research_events(
            dataset=dataset,
            parameter_values=params,
            fee_rate=0.002,
            slippage_bps=10.0,
            execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
            portfolio_policy=legacy_research_portfolio_policy(),
        )
    )[-1]

    assert high_cost_event.feature_snapshot["required_breakout_distance"] > low_cost_event.feature_snapshot[
        "required_breakout_distance"
    ]
    assert high_cost_event.feature_snapshot["fee_rate_used_for_entry_gate"] == pytest.approx(0.002)
    assert high_cost_event.feature_snapshot["slippage_bps_used_for_entry_gate"] == pytest.approx(10.0)


def test_required_breakout_distance_uses_round_trip_fee_by_default() -> None:
    values = materialize_channel_breakout_parameters(
        plugin=CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN,
        parameter_values=_params(
            CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
            CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
            MIN_BREAKOUT_DISTANCE_RATIO=0.0,
            ENTRY_EDGE_BUFFER_RATIO=0.0,
        ),
        fee_rate=0.0004,
        slippage_bps=0.0,
    )

    assert values["REQUIRED_BREAKOUT_DISTANCE_RATIO"] >= 0.0008
    assert values["REQUIRED_BREAKOUT_DISTANCE_RATIO"] == pytest.approx(0.0008)
    assert values["ENTRY_COST_BASIS"] == "round_trip"


def test_required_breakout_distance_diagnostics_records_cost_basis() -> None:
    dataset = _weak_breakout_dataset(close=104.2)
    values = materialize_channel_breakout_parameters(
        plugin=CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN,
        parameter_values=_params(
            CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
            CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
            MIN_BREAKOUT_DISTANCE_RATIO=0.0,
            ENTRY_EDGE_BUFFER_RATIO=0.0,
        ),
        fee_rate=0.0004,
        slippage_bps=2.0,
    )
    event = tuple(
        build_channel_breakout_research_events(
            dataset=dataset,
            parameter_values=values,
            fee_rate=0.0004,
            slippage_bps=2.0,
            execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
            portfolio_policy=legacy_research_portfolio_policy(),
        )
    )[-1]

    assert event.feature_snapshot["required_breakout_distance"] == pytest.approx(0.0012)
    assert event.feature_snapshot["entry_cost_basis"] == "round_trip"
    assert event.strategy_diagnostics["entry_cost_basis"] == "round_trip"
    assert event.strategy_diagnostics["fee_rate_used_for_entry_gate"] == pytest.approx(0.0004)
    assert event.strategy_diagnostics["slippage_bps_used_for_entry_gate"] == pytest.approx(2.0)


def test_min_breakout_distance_is_behavior_affecting_parameter() -> None:
    assert "MIN_BREAKOUT_DISTANCE_RATIO" in CHANNEL_BREAKOUT_SPEC.behavior_affecting_parameter_names


def test_not_enough_lookback_holds() -> None:
    dataset = _dataset()
    params = _materialized()

    decision = decide_channel_breakout_snapshot(
        candle=dataset.candles[1],
        candle_index=1,
        dataset=dataset,
        parameter_values=params,
    )

    assert decision["signal"] == "HOLD"
    assert decision["reason"] == "not_enough_lookback"
    assert decision["strategy_diagnostics"]["blocked_filters"] == ()
    assert _REQUIRED_FEATURE_FIELDS <= set(decision["feature_snapshot"])


def test_current_candle_high_is_not_in_rolling_high() -> None:
    dataset = _dataset(
        (
            Candle(0, 100.0, 101.0, 99.0, 100.0, 100.0),
            Candle(60_000, 100.0, 102.0, 99.0, 101.0, 100.0),
            Candle(120_000, 101.0, 103.0, 100.0, 102.0, 100.0),
            Candle(180_000, 102.0, 104.0, 101.0, 103.0, 100.0),
            Candle(240_000, 103.0, 120.0, 100.0, 103.5, 200.0),
        )
    )
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    decision = decide_channel_breakout_snapshot(
        candle=dataset.candles[-1],
        candle_index=len(dataset.candles) - 1,
        dataset=dataset,
        parameter_values=params,
    )

    assert decision["feature_snapshot"]["rolling_high"] == 104.0
    assert decision["feature_snapshot"]["rolling_high"] != 120.0
    assert decision["signal"] == "HOLD"
    assert "close_not_above_rolling_high" in decision["feature_snapshot"]["blocked_filters"]


def test_delayed_confirmation_candidate_candle_holds() -> None:
    dataset = _breakout_confirmation_dataset()
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    events = _events(dataset, params)
    candidate = events[4]

    assert candidate.final_signal == "HOLD"
    assert candidate.reason == "breakout_pending_confirmation"
    assert candidate.order_intent is None
    assert candidate.feature_snapshot["breakout_candidate"] is True
    assert candidate.feature_snapshot["confirmation_status"] == "candidate"


def test_delayed_confirmation_confirms_within_window_and_buys() -> None:
    dataset = _breakout_confirmation_dataset(extra_confirming_candle=True)
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    events = _events(dataset, params)
    confirmed = events[5]

    assert confirmed.final_signal == "BUY"
    assert confirmed.reason == "delayed_breakout_confirmed"
    assert confirmed.order_intent == {
        "side": "BUY",
        "sizing": "portfolio_policy_fractional_cash",
        "entry_breakout_level": 104.0,
    }
    assert confirmed.feature_snapshot["entry_mode"] == "delayed_confirmation"
    assert confirmed.feature_snapshot["confirmation_status"] == "confirmed"
    assert events[6].final_signal == "HOLD"


def test_delayed_confirmation_expires_without_buy() -> None:
    dataset = _breakout_confirmation_dataset()
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=0,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    events = _events(dataset, params)

    assert events[4].final_signal == "HOLD"
    assert events[5].final_signal == "HOLD"
    assert events[5].reason == "breakout_confirmation_expired"
    assert all(event.final_signal != "BUY" for event in events)


def test_delayed_confirmation_deep_retest_fails_without_buy() -> None:
    dataset = _breakout_confirmation_dataset(confirmation_close=108.0, confirmation_low=100.0)
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    events = _events(dataset, params)

    assert events[5].final_signal == "HOLD"
    assert events[5].reason == "breakout_confirmation_failed_deep_retest"
    assert events[5].feature_snapshot["confirmation_status"] == "failed_deep_retest"
    assert all(event.final_signal != "BUY" for event in events)


def test_delayed_confirmation_pending_snapshot_contains_breakout_metadata() -> None:
    dataset = _breakout_confirmation_dataset()
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=2,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(dataset, params)[4]

    assert event.feature_snapshot["entry_mode"] == "delayed_confirmation"
    assert event.feature_snapshot["breakout_level"] == 104.0
    assert event.feature_snapshot["breakout_index"] == 4
    assert event.feature_snapshot["confirmation_window_min"] == 2
    assert event.feature_snapshot["pending_expires_at_index"] == 6
    assert event.strategy_diagnostics["confirmation_status"] == "candidate"


def test_delayed_confirmation_confirmed_reason_is_emitted() -> None:
    dataset = _breakout_confirmation_dataset()
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    assert _events(dataset, params)[5].reason == "delayed_breakout_confirmed"


def test_delayed_confirmation_expired_reason_is_emitted() -> None:
    dataset = _breakout_confirmation_dataset()
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=0,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    assert _events(dataset, params)[5].reason == "breakout_confirmation_expired"


def test_delayed_confirmation_deep_retest_failure_reason_is_emitted() -> None:
    dataset = _breakout_confirmation_dataset(confirmation_close=108.0, confirmation_low=100.0)
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    assert _events(dataset, params)[5].reason == "breakout_confirmation_failed_deep_retest"


def test_delayed_confirmation_regime_failure_reason_is_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    import bithumb_bot.strategy_plugins.channel_breakout_research as module

    dataset = _breakout_confirmation_dataset()
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_REGIME_FILTER_ENABLED=True,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    def fake_regime(*, index: int, **_: object):
        price_regime = "downtrend" if index == 5 else "uptrend"
        return SimpleNamespace(
            price_regime=price_regime,
            volatility_bucket="normal",
            volume_bucket="normal",
            liquidity_bucket="normal",
            composite_regime=price_regime,
            legacy_regime="trend",
        )

    monkeypatch.setattr(module, "classify_market_regime_from_arrays", fake_regime)

    event = tuple(
        module.build_channel_breakout_research_events(
            dataset=dataset,
            parameter_values=params,
            fee_rate=0.001,
            slippage_bps=0.0,
            execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
            portfolio_policy=legacy_research_portfolio_policy(),
        )
    )[5]

    assert event.final_signal == "HOLD"
    assert event.reason == "breakout_confirmation_failed_regime"
    assert event.feature_snapshot["confirmation_status"] == "failed_regime"


def test_delayed_confirmation_rejects_low_close_location() -> None:
    dataset = _breakout_confirmation_dataset(confirmation_close=104.2, confirmation_low=104.0)
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
        CONFIRMATION_CLOSE_LOCATION_MIN=0.8,
    )

    event = _events(dataset, params)[5]

    assert event.final_signal == "HOLD"
    assert event.reason == "breakout_confirmation_failed_close_location"
    assert event.feature_snapshot["close_location"] < 0.8


def test_delayed_confirmation_rejects_large_upper_wick() -> None:
    dataset = _breakout_confirmation_dataset(confirmation_close=108.0, confirmation_low=104.0)
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
        MAX_UPPER_WICK_RATIO=0.05,
    )

    event = _events(dataset, params)[5]

    assert event.final_signal == "HOLD"
    assert event.reason == "breakout_confirmation_failed_upper_wick"
    assert event.feature_snapshot["upper_wick_ratio"] > 0.05


def test_delayed_confirmation_rejects_small_body() -> None:
    dataset = _breakout_confirmation_dataset(confirmation_close=108.1, confirmation_low=104.0)
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
        MIN_BODY_RATIO=0.2,
    )

    event = _events(dataset, params)[5]

    assert event.final_signal == "HOLD"
    assert event.reason == "breakout_confirmation_failed_body"
    assert event.feature_snapshot["body_ratio"] < 0.2


def test_delayed_confirmation_rejects_insufficient_confirmation_distance() -> None:
    dataset = _breakout_confirmation_dataset(confirmation_close=104.2, confirmation_low=104.0)
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
        CONFIRMATION_MIN_BREAKOUT_DISTANCE_RATIO=0.01,
    )

    event = _events(dataset, params)[5]

    assert event.final_signal == "HOLD"
    assert event.reason == "breakout_confirmation_failed_distance"


def test_delayed_confirmation_quality_fields_are_in_feature_snapshot() -> None:
    dataset = _breakout_confirmation_dataset()
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    snapshot = _events(dataset, params)[5].feature_snapshot

    assert {
        "confirmation_breakout_distance",
        "close_location",
        "upper_wick_ratio",
        "body_ratio",
        "confirmation_volume_ratio",
    } <= set(snapshot)


def test_delayed_confirmation_quality_filter_reason_is_emitted() -> None:
    dataset = _breakout_confirmation_dataset(confirmation_close=108.0, confirmation_low=104.0)
    params = _materialized(
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=1,
        PULLBACK_RATIO=0.02,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
        CONFIRMATION_VOLUME_RATIO_MIN=2.0,
    )

    event = _events(dataset, params)[5]

    assert event.final_signal == "HOLD"
    assert event.reason == "breakout_confirmation_failed_volume"
    assert event.strategy_diagnostics["confirmation_failure_reason"] == "breakout_confirmation_failed_volume"


def test_retest_hold_mode_does_not_buy_on_initial_breakout() -> None:
    params = _materialized(
        ENTRY_MODE="retest_hold_after_breakout",
        RETEST_WINDOW_MIN=2,
        RETEST_MAX_DEPTH_RATIO=0.02,
        RETEST_HOLD_CANDLES=1,
        RETEST_REBOUND_RATIO=0.0,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(_retest_hold_dataset(), params)[4]

    assert event.final_signal == "HOLD"
    assert event.reason == "breakout_waiting_for_retest"
    assert event.order_intent is None
    assert event.feature_snapshot["retest_state"] == "waiting_for_retest"


def test_retest_hold_mode_buys_after_retest_hold_and_rebound() -> None:
    params = _materialized(
        ENTRY_MODE="retest_hold_after_breakout",
        RETEST_WINDOW_MIN=2,
        RETEST_MAX_DEPTH_RATIO=0.02,
        RETEST_HOLD_CANDLES=2,
        RETEST_REBOUND_RATIO=0.005,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    events = _events(_retest_hold_dataset(), params)

    assert events[4].final_signal == "HOLD"
    assert events[5].final_signal == "HOLD"
    assert events[5].feature_snapshot["retest_state"] == "retest_observed"
    assert events[6].final_signal == "BUY"
    assert events[6].reason == "retest_hold_after_breakout_confirmed"
    assert events[6].feature_snapshot["retest_state"] == "rebound_confirmed"


def test_retest_hold_mode_expires_without_retest() -> None:
    params = _materialized(
        ENTRY_MODE="retest_hold_after_breakout",
        RETEST_WINDOW_MIN=1,
        RETEST_MAX_DEPTH_RATIO=0.02,
        RETEST_HOLD_CANDLES=1,
        RETEST_REBOUND_RATIO=0.0,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )
    dataset = _dataset(
        (
            *_retest_hold_dataset(retest_low=104.5, hold_close=105.0, include_rebound=False).candles,
            Candle(360_000, 105.0, 106.0, 104.5, 105.0, 180.0),
        )
    )

    events = _events(dataset, params)

    assert events[6].final_signal == "HOLD"
    assert events[6].reason == "retest_hold_after_breakout_expired"
    assert events[6].feature_snapshot["retest_failure_reason"] == "expired"
    assert all(event.final_signal != "BUY" for event in events)


def test_retest_hold_mode_fails_on_deep_retest() -> None:
    params = _materialized(
        ENTRY_MODE="retest_hold_after_breakout",
        RETEST_WINDOW_MIN=2,
        RETEST_MAX_DEPTH_RATIO=0.01,
        RETEST_HOLD_CANDLES=1,
        RETEST_REBOUND_RATIO=0.0,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(_retest_hold_dataset(retest_low=100.0), params)[5]

    assert event.final_signal == "HOLD"
    assert event.reason == "retest_hold_after_breakout_failed_deep_retest"
    assert event.feature_snapshot["retest_failure_reason"] == "failed_deep_retest"
    assert event.strategy_diagnostics["retest_failure_reason"] == "failed_deep_retest"


def test_retest_hold_mode_records_hold_failure_reason_in_diagnostics() -> None:
    params = _materialized(
        ENTRY_MODE="retest_hold_after_breakout",
        RETEST_WINDOW_MIN=2,
        RETEST_MAX_DEPTH_RATIO=0.02,
        RETEST_HOLD_CANDLES=1,
        RETEST_REBOUND_RATIO=0.0,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(_retest_hold_dataset(retest_low=103.8, hold_close=103.9), params)[5]

    assert event.final_signal == "HOLD"
    assert event.reason == "retest_hold_after_breakout_failed_hold_above_level"
    assert event.feature_snapshot["retest_failure_reason"] == "failed_hold_above_level"
    assert event.strategy_diagnostics["retest_failure_reason"] == "failed_hold_above_level"


def test_retest_hold_mode_preserves_immediate_breakout_behavior() -> None:
    params = _materialized(
        ENTRY_MODE="immediate_breakout",
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(_retest_hold_dataset(), params)[4]

    assert event.final_signal == "BUY"
    assert event.reason == "channel_breakout_confirmed"


@pytest.mark.parametrize(
    "bad_key",
    [
        "CHANNEL_BREAKOUT_LOOKBACK",
        "CHANNEL_BREAKOUT_RANGE_WINDOW",
        "CHANNEL_BREAKOUT_VOLUME_WINDOW",
    ],
)
def test_parameter_materializer_rejects_windows_below_two(bad_key: str) -> None:
    with pytest.raises(StrategySpecError, match=bad_key):
        _materialized(**{bad_key: 1})


def test_parameter_materializer_accepts_valid_values() -> None:
    values = _materialized()

    assert values["CHANNEL_BREAKOUT_LOOKBACK"] == 3
    assert values["CHANNEL_BREAKOUT_RANGE_RATIO_MIN"] == 1.2
    assert values["CHANNEL_BREAKOUT_VOLUME_RATIO_MIN"] == 1.1
    assert values["CHANNEL_BREAKOUT_REGIME_FILTER_ENABLED"] is False
    assert values["STRATEGY_EXIT_RULES"] == "stop_loss,max_holding_time"


@pytest.mark.parametrize("raw_rules", ["trailing_stop", "custom_exit", "opposite_cross"])
def test_exit_rule_regression_rejects_unsupported_rules(raw_rules: str) -> None:
    with pytest.raises(StrategySpecError, match="unsupported rule"):
        _materialized(STRATEGY_EXIT_RULES=raw_rules, STRATEGY_EXIT_STOP_LOSS_RATIO=0.0)


def test_common_exit_rules_are_accepted() -> None:
    values = _materialized(STRATEGY_EXIT_RULES="stop_loss,max_holding_time")

    assert values["STRATEGY_EXIT_RULES"] == "stop_loss,max_holding_time"


def test_exit_policy_hash_changes_when_take_profit_changes() -> None:
    base = _materialized(STRATEGY_EXIT_RULES="stop_loss,take_profit,max_holding_time", TAKE_PROFIT_RATIO=0.01)
    changed = _materialized(STRATEGY_EXIT_RULES="stop_loss,take_profit,max_holding_time", TAKE_PROFIT_RATIO=0.02)

    assert "TAKE_PROFIT_RATIO" in CHANNEL_BREAKOUT_SPEC.accepted_parameter_names
    assert "TAKE_PROFIT_RATIO" in CHANNEL_BREAKOUT_SPEC.behavior_affecting_parameter_names
    assert exit_policy_hash(exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, base)) != exit_policy_hash(
        exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, changed)
    )


def test_channel_breakout_advanced_exit_params_are_not_behavior_affecting_when_diagnostic_only() -> None:
    diagnostic_only = {
        "TRAILING_STOP_RATIO",
        "BREAK_EVEN_STOP_ENABLED",
        "OPPOSITE_SIGNAL_EXIT_ENABLED",
        "REGIME_CHANGE_EXIT_ENABLED",
    }

    assert diagnostic_only <= set(CHANNEL_BREAKOUT_SPEC.accepted_parameter_names)
    assert diagnostic_only.isdisjoint(CHANNEL_BREAKOUT_SPEC.behavior_affecting_parameter_names)
    assert diagnostic_only <= set(CHANNEL_BREAKOUT_SPEC.research_only_parameter_names)


def test_production_bound_manifest_rejects_diagnostic_only_exit_params() -> None:
    parameter_space = {
        "CHANNEL_BREAKOUT_LOOKBACK": (3,),
        "CHANNEL_BREAKOUT_RANGE_WINDOW": (3,),
        "CHANNEL_BREAKOUT_VOLUME_WINDOW": (3,),
        "TRAILING_STOP_RATIO": (0.02,),
    }

    with pytest.raises(StrategySpecError, match="research-only strategy parameter"):
        validate_parameter_space_against_strategy_spec(
            strategy_name=CHANNEL_BREAKOUT_SPEC.strategy_name,
            parameter_space=parameter_space,
            deployment_tier="paper_candidate",
        )


def test_existing_stop_loss_max_holding_behavior_is_unchanged() -> None:
    values = _materialized()
    policy = exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, values)

    assert policy["common_rules"] == ["stop_loss", "max_holding_time"]
    assert policy["stop_loss"]["stop_loss_ratio"] == 0.01
    assert policy["max_holding_time"]["max_holding_min"] == 30


def test_channel_breakout_exit_rule_factory_emits_breakout_reclaim_rule() -> None:
    rules = CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN.exit_rule_factory({}, {}, 0.0)

    assert [rule.name for rule in rules] == ["breakout_level_reclaim_failed"]


def test_breakout_reclaim_exit_triggers_when_close_below_breakout_level() -> None:
    rule = CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN.exit_rule_factory({}, {}, 0.0)[0]
    decision = rule.evaluate(
        position=PositionContext(
            in_position=True,
            entry_ts=0,
            entry_price=105.0,
            qty_open=1.0,
            holding_time_sec=60.0,
            unrealized_pnl=-1.0,
            unrealized_pnl_ratio=-0.01,
        ),
        candle_ts=60_000,
        market_price=103.5,
        signal_context={"entry_breakout_level": 104.0},
    )

    assert decision.should_exit is True
    assert decision.context["entry_breakout_level"] == 104.0


def test_reclaim_exit_factory_passes_parameter_values() -> None:
    rule = CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN.exit_rule_factory(
        {},
        {
            "BREAKOUT_RECLAIM_TOLERANCE_RATIO": 0.001,
            "BREAKOUT_RECLAIM_CONFIRMATION_CANDLES": 2,
            "BREAKOUT_RECLAIM_GRACE_MIN": 5,
        },
        0.0,
    )[0]

    assert rule.tolerance_ratio == pytest.approx(0.001)
    assert rule.confirmation_candles == 2
    assert rule.grace_min == 5


def test_reclaim_exit_uses_tolerance_ratio() -> None:
    rule = CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN.exit_rule_factory(
        {},
        {"BREAKOUT_RECLAIM_TOLERANCE_RATIO": 0.001},
        0.0,
    )[0]
    position = PositionContext(
        in_position=True,
        entry_ts=0,
        entry_price=105.0,
        qty_open=1.0,
        holding_time_sec=60.0,
        unrealized_pnl=-1.0,
        unrealized_pnl_ratio=-0.01,
    )

    decision = rule.evaluate(
        position=position,
        candle_ts=60_000,
        market_price=103.95,
        signal_context={"entry_breakout_level": 104.0},
    )

    assert decision.should_exit is False
    assert decision.context["threshold_price"] == pytest.approx(103.896)


def test_reclaim_exit_respects_grace_minutes() -> None:
    rule = CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN.exit_rule_factory(
        {},
        {"BREAKOUT_RECLAIM_GRACE_MIN": 5},
        0.0,
    )[0]
    position = PositionContext(
        in_position=True,
        entry_ts=0,
        entry_price=105.0,
        qty_open=1.0,
        holding_time_sec=60.0,
        unrealized_pnl=-1.0,
        unrealized_pnl_ratio=-0.01,
    )

    decision = rule.evaluate(
        position=position,
        candle_ts=60_000,
        market_price=103.0,
        signal_context={"entry_breakout_level": 104.0},
    )

    assert decision.should_exit is False
    assert decision.context["in_grace_period"] is True


def test_reclaim_exit_requires_confirmation_candles() -> None:
    rule = CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN.exit_rule_factory(
        {},
        {"BREAKOUT_RECLAIM_CONFIRMATION_CANDLES": 2},
        0.0,
    )[0]
    position = PositionContext(
        in_position=True,
        entry_ts=0,
        entry_price=105.0,
        qty_open=1.0,
        holding_time_sec=60.0,
        unrealized_pnl=-1.0,
        unrealized_pnl_ratio=-0.01,
    )

    first = rule.evaluate(
        position=position,
        candle_ts=60_000,
        market_price=103.0,
        signal_context={"entry_breakout_level": 104.0},
    )
    second = rule.evaluate(
        position=position,
        candle_ts=120_000,
        market_price=103.0,
        signal_context={"entry_breakout_level": 104.0},
    )

    assert first.should_exit is False
    assert first.context["breach_count"] == 1
    assert second.should_exit is True
    assert second.context["breach_count"] == 2


def test_strategy_exit_rule_source_is_reported_as_strategy() -> None:
    dataset = _dataset(
        (
            Candle(0, 100.0, 101.0, 99.0, 100.0, 100.0),
            Candle(60_000, 101.0, 102.0, 100.0, 101.0, 100.0),
            Candle(120_000, 102.0, 103.0, 101.0, 102.0, 100.0),
            Candle(180_000, 103.0, 104.0, 102.0, 103.0, 100.0),
            Candle(240_000, 104.0, 110.0, 103.0, 109.0, 200.0),
            Candle(300_000, 109.0, 109.5, 103.0, 103.5, 200.0),
        )
    )
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
        STRATEGY_EXIT_STOP_LOSS_RATIO=0.50,
    )
    events = _events(dataset, params)

    result = run_decision_event_backtest(
        dataset=dataset,
        strategy_name=CHANNEL_BREAKOUT_SPEC.strategy_name,
        parameter_values=params,
        fee_rate=0.0,
        slippage_bps=0.0,
        decision_events=events,
        context=BacktestRunContext(report_detail="full"),
    )

    exit_decision = result.decisions[-1]
    assert exit_decision["final_signal"] == "SELL"
    assert exit_decision["exit_rule"] == "breakout_level_reclaim_failed"
    assert exit_decision["exit_evaluations"][0]["rule"] == "breakout_level_reclaim_failed"
    assert exit_decision["exit_evaluations"][0]["rule_source"] == "strategy"


def test_exit_reason_distribution_records_take_profit() -> None:
    from bithumb_bot.research.backtest_support import BacktestAccumulator
    from bithumb_bot.research.backtest_types import BacktestRunContext

    accumulator = BacktestAccumulator(
        context=BacktestRunContext(report_detail="summary"),
        total_candles=1,
        diagnostics_namespace=CHANNEL_BREAKOUT_SPEC.strategy_name,
    )

    diagnostics = accumulator.strategy_diagnostics(
        trades=[
            {
                "side": "SELL",
                "is_portfolio_applied_trade": True,
                "exit_rule": "take_profit",
                "net_pnl": 10.0,
                "holding_minutes": 5.0,
            }
        ]
    )

    assert diagnostics["exit_reason_distribution"]["take_profit"] == 1
    assert diagnostics["exit_rule_distribution"]["take_profit"] == 1


def test_channel_breakout_runtime_exit_diagnostics_include_exit_rule_distribution() -> None:
    from bithumb_bot.research.backtest_support import BacktestAccumulator
    from bithumb_bot.research.backtest_types import BacktestRunContext

    accumulator = BacktestAccumulator(
        context=BacktestRunContext(report_detail="summary"),
        total_candles=1,
        diagnostics_namespace=CHANNEL_BREAKOUT_SPEC.strategy_name,
    )

    diagnostics = accumulator.strategy_diagnostics(
        trades=[
            {
                "side": "SELL",
                "is_portfolio_applied_trade": True,
                "exit_rule": "take_profit",
                "exit_reason": "price_target_reached",
                "net_pnl": 10.0,
                "return_pct": 0.02,
                "holding_minutes": 5.0,
                "mae_pct": -0.003,
                "mfe_pct": 0.025,
            }
        ]
    )

    assert diagnostics["exit_rule_distribution"]["take_profit"] == 1
    assert diagnostics["exit_reason_distribution"]["take_profit"] == 1
    assert diagnostics["avg_holding_minutes_by_exit_reason"]["take_profit"] == 5.0
    assert diagnostics["mae_mfe_by_exit_reason"]["take_profit"]["avg_mae_pct"] == -0.003
    assert diagnostics["mae_mfe_by_exit_reason"]["take_profit"]["avg_mfe_pct"] == 0.025


def test_channel_breakout_runtime_exit_diagnostics_include_return_by_exit_reason() -> None:
    from bithumb_bot.research.backtest_support import BacktestAccumulator
    from bithumb_bot.research.backtest_types import BacktestRunContext

    accumulator = BacktestAccumulator(
        context=BacktestRunContext(report_detail="summary"),
        total_candles=1,
        diagnostics_namespace=CHANNEL_BREAKOUT_SPEC.strategy_name,
    )

    diagnostics = accumulator.strategy_diagnostics(
        trades=[
            {
                "side": "SELL",
                "is_portfolio_applied_trade": True,
                "exit_rule": "take_profit",
                "return_pct": 0.02,
                "net_pnl": 10.0,
            },
            {
                "side": "SELL",
                "is_portfolio_applied_trade": True,
                "exit_rule": "take_profit",
                "return_pct": 0.01,
                "closed_trade_pnl": 4.0,
            },
        ]
    )

    take_profit_returns = diagnostics["return_by_exit_reason"]["take_profit"]
    assert take_profit_returns["count"] == 2
    assert take_profit_returns["avg_return_pct"] == pytest.approx(0.015)
    assert take_profit_returns["total_return_pct"] == pytest.approx(0.03)
    assert take_profit_returns["avg_pnl"] == 7.0
    assert take_profit_returns["total_pnl"] == 14.0


def test_channel_breakout_advanced_exit_rules_are_marked_diagnostic_only() -> None:
    values = _materialized(
        TRAILING_STOP_RATIO=0.02,
        BREAK_EVEN_STOP_ENABLED=True,
        OPPOSITE_SIGNAL_EXIT_ENABLED=True,
        REGIME_CHANGE_EXIT_ENABLED=True,
    )
    policy = exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, values)

    for key in (
        "trailing_stop",
        "break_even_stop",
        "opposite_signal_exit",
        "regime_change_exit",
    ):
        assert policy[key]["enabled"] is True
        assert policy[key]["evaluation_status"] == "diagnostic_policy_bound_not_runtime_evaluated"


def test_event_builder_emits_required_event_fields() -> None:
    dataset = _dataset()
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )
    timing_policy = ExecutionTimingPolicy(decision_guard_ms=250)

    events = tuple(
        build_channel_breakout_research_events(
            dataset=dataset,
            parameter_values=params,
            fee_rate=0.001,
            slippage_bps=0.0,
            execution_timing_policy=timing_policy,
            portfolio_policy=legacy_research_portfolio_policy(),
        )
    )
    event = events[-1]

    assert len(events) == len(dataset.candles)
    assert event.decision_ts == candle_close_ts(dataset.candles[-1], interval=dataset.interval) + 250
    assert event.strategy_name == "channel_breakout_with_regime_filter"
    assert event.strategy_version == CHANNEL_BREAKOUT_SPEC.strategy_version
    assert event.raw_signal == "BUY"
    assert event.final_signal == "BUY"
    assert event.entry_signal == "BUY"
    assert event.exit_signal == "HOLD"
    assert event.blocked_filters == ()
    assert event.order_intent == {
        "side": "BUY",
        "sizing": "portfolio_policy_fractional_cash",
        "entry_breakout_level": 104.0,
    }
    assert event.exit_intent == {
        "mode": "evaluate_exit_policy",
        "base_signal": "HOLD",
        "base_reason": "common_exit_policy_only",
    }
    assert event.extra_payload == {"strategy_family": "channel_breakout", "research_only": True}
    assert _REQUIRED_FEATURE_FIELDS <= set(event.feature_snapshot)
    assert event.strategy_diagnostics["schema_version"] == 1
    assert event.strategy_diagnostics["blocked_filters"] == ()
    assert event.strategy_diagnostics["regime_filter_enabled"] is False


@pytest.mark.unit
@pytest.mark.contract
@pytest.mark.resource_guard
def test_channel_breakout_event_builder_returns_one_shot_iterable_without_materializing_all_events() -> None:
    dataset = _dataset()
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    events = build_channel_breakout_research_events(
        dataset=dataset,
        parameter_values=params,
        fee_rate=0.001,
        slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
        portfolio_policy=legacy_research_portfolio_policy(),
    )

    assert not isinstance(events, (tuple, list))
    first_pass = tuple(events)
    second_pass = tuple(events)
    assert len(first_pass) == len(dataset.candles)
    assert second_pass == ()


@pytest.mark.unit
@pytest.mark.contract
@pytest.mark.resource_guard
def test_channel_breakout_streaming_delayed_confirmation_preserves_state() -> None:
    dataset = _breakout_confirmation_dataset(extra_confirming_candle=True)
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
        ENTRY_MODE="delayed_confirmation",
        CONFIRMATION_WINDOW_MIN=2,
        PULLBACK_RATIO=0.05,
    )
    kwargs = {
        "dataset": dataset,
        "strategy_name": "channel_breakout_with_regime_filter",
        "parameter_values": params,
        "fee_rate": 0.001,
        "slippage_bps": 0.0,
        "context": BacktestRunContext(report_detail="summary"),
    }
    streaming_events = build_channel_breakout_research_events(
        dataset=dataset,
        parameter_values=params,
        fee_rate=0.001,
        slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
        portfolio_policy=legacy_research_portfolio_policy(),
    )
    tuple_events = tuple(
        build_channel_breakout_research_events(
            dataset=dataset,
            parameter_values=params,
            fee_rate=0.001,
            slippage_bps=0.0,
            execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
            portfolio_policy=legacy_research_portfolio_policy(),
        )
    )

    assert any(event.reason == "delayed_breakout_confirmed" for event in tuple_events)
    tuple_result = run_decision_event_backtest(decision_events=tuple_events, **kwargs)
    streaming_result = run_decision_event_backtest(decision_events=streaming_events, **kwargs)

    assert tuple_result.resource_usage["decision_behavior_hash"] == streaming_result.resource_usage[
        "decision_behavior_hash"
    ]


def test_cooldown_blocks_buy_until_window_elapses() -> None:
    dataset = _repeated_breakout_dataset()
    params = _materialized(
        COOLDOWN_MIN=2,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
    )

    events = _events(dataset, params)

    assert events[4].final_signal == "BUY"
    assert events[5].final_signal == "HOLD"
    assert "cooldown_active" in events[5].blocked_filters
    assert events[6].final_signal == "BUY"


def test_zero_cooldown_preserves_existing_behavior() -> None:
    dataset = _repeated_breakout_dataset()
    params = _materialized(
        COOLDOWN_MIN=0,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
    )

    events = _events(dataset, params)

    assert [event.final_signal for event in events[4:7]] == ["BUY", "BUY", "BUY"]


def test_max_trades_per_day_blocks_after_limit() -> None:
    dataset = _repeated_breakout_dataset()
    params = _materialized(
        MAX_TRADES_PER_DAY=1,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
    )

    events = _events(dataset, params)

    assert events[4].final_signal == "BUY"
    assert events[5].final_signal == "HOLD"
    assert "max_trades_per_day_reached" in events[5].blocked_filters
    assert events[6].final_signal == "HOLD"
    assert "max_trades_per_day_reached" in events[6].blocked_filters


def test_zero_max_trades_per_day_preserves_existing_behavior() -> None:
    dataset = _repeated_breakout_dataset()
    params = _materialized(
        MAX_TRADES_PER_DAY=0,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
    )

    events = _events(dataset, params)

    assert [event.final_signal for event in events[4:7]] == ["BUY", "BUY", "BUY"]


def test_cooldown_state_resets_between_backtest_event_builder_calls() -> None:
    dataset = _repeated_breakout_dataset()
    params = _materialized(
        COOLDOWN_MIN=10,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=0.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=0.0,
    )

    first = _events(dataset, params)
    second = _events(dataset, params)

    assert first[4].final_signal == "BUY"
    assert second[4].final_signal == "BUY"


def test_kst_entry_time_filter_allows_09_kst_buy() -> None:
    dataset = _breakout_dataset_for_decision_ts(_utc_ms_for_kst_time(hour=9, minute=30))
    params = _materialized(
        ENTRY_TIME_FILTER_KST_ENABLED=True,
        ENTRY_TIME_FILTER_KST_START_HOUR=9,
        ENTRY_TIME_FILTER_KST_END_HOUR=10,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(dataset, params)[-1]

    assert event.final_signal == "BUY"
    assert event.order_intent is not None
    assert event.feature_snapshot["entry_hour_kst"] == 9


def test_kst_entry_time_filter_blocks_10_kst_buy_for_09_only_window() -> None:
    dataset = _breakout_dataset_for_decision_ts(_utc_ms_for_kst_time(hour=10, minute=1))
    params = _materialized(
        ENTRY_TIME_FILTER_KST_ENABLED=True,
        ENTRY_TIME_FILTER_KST_START_HOUR=9,
        ENTRY_TIME_FILTER_KST_END_HOUR=10,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(dataset, params)[-1]

    assert event.final_signal == "HOLD"
    assert event.order_intent is None
    assert "entry_time_filter_kst_blocked" in event.blocked_filters


def test_kst_entry_time_filter_does_not_remove_exit_intent() -> None:
    dataset = _breakout_dataset_for_decision_ts(_utc_ms_for_kst_time(hour=10, minute=1))
    params = _materialized(
        ENTRY_TIME_FILTER_KST_ENABLED=True,
        ENTRY_TIME_FILTER_KST_START_HOUR=9,
        ENTRY_TIME_FILTER_KST_END_HOUR=10,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(dataset, params)[-1]

    assert event.final_signal == "HOLD"
    assert event.exit_intent == {
        "mode": "evaluate_exit_policy",
        "base_signal": "HOLD",
        "base_reason": "common_exit_policy_only",
    }


def test_kst_entry_time_filter_disabled_preserves_buy() -> None:
    dataset = _breakout_dataset_for_decision_ts(_utc_ms_for_kst_time(hour=10, minute=1))
    params = _materialized(
        ENTRY_TIME_FILTER_KST_ENABLED=False,
        ENTRY_TIME_FILTER_KST_START_HOUR=9,
        ENTRY_TIME_FILTER_KST_END_HOUR=10,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(dataset, params)[-1]

    assert event.final_signal == "BUY"
    assert event.order_intent is not None
    assert "entry_time_filter_kst_blocked" not in event.blocked_filters


def test_kst_entry_time_filter_uses_decision_ts_not_candle_ts() -> None:
    decision_ts = _utc_ms_for_kst_time(hour=9, minute=0)
    dataset = _breakout_dataset_for_decision_ts(decision_ts, decision_guard_ms=1_000)
    params = _materialized(
        ENTRY_TIME_FILTER_KST_ENABLED=True,
        ENTRY_TIME_FILTER_KST_START_HOUR=9,
        ENTRY_TIME_FILTER_KST_END_HOUR=10,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events_with_guard(dataset, params, decision_guard_ms=1_000)[-1]

    assert _kst_hour_from_decision_ts_ms(candle_close_ts(dataset.candles[-1], interval=dataset.interval)) == 8
    assert event.decision_ts == decision_ts
    assert event.feature_snapshot["entry_hour_kst"] == 9
    assert event.final_signal == "BUY"


def test_kst_entry_time_filter_uses_start_inclusive_end_exclusive_window() -> None:
    params = _materialized(
        ENTRY_TIME_FILTER_KST_ENABLED=True,
        ENTRY_TIME_FILTER_KST_START_HOUR=9,
        ENTRY_TIME_FILTER_KST_END_HOUR=10,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    at_start = _events(_breakout_dataset_for_decision_ts(_utc_ms_for_kst_time(hour=9, minute=0)), params)[-1]
    before_end = _events(_breakout_dataset_for_decision_ts(_utc_ms_for_kst_time(hour=9, minute=59)), params)[-1]
    at_end = _events(_breakout_dataset_for_decision_ts(_utc_ms_for_kst_time(hour=10, minute=0)), params)[-1]

    assert at_start.final_signal == "BUY"
    assert before_end.final_signal == "BUY"
    assert at_end.final_signal == "HOLD"
    assert "entry_time_filter_kst_blocked" in at_end.blocked_filters


def test_kst_entry_time_filter_blocked_reason_is_reported() -> None:
    dataset = _breakout_dataset_for_decision_ts(_utc_ms_for_kst_time(hour=10, minute=1))
    params = _materialized(
        ENTRY_TIME_FILTER_KST_ENABLED=True,
        ENTRY_TIME_FILTER_KST_START_HOUR=9,
        ENTRY_TIME_FILTER_KST_END_HOUR=10,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(dataset, params)[-1]

    assert "entry_time_filter_kst_blocked" in event.blocked_filters
    assert "entry_time_filter_kst_blocked" in event.strategy_diagnostics["blocked_filters"]
    assert "entry_time_filter_kst_blocked" in event.feature_snapshot["blocked_filters"]


def test_kst_entry_time_filter_records_entry_hour_kst() -> None:
    dataset = _breakout_dataset_for_decision_ts(_utc_ms_for_kst_time(hour=10, minute=1))
    params = _materialized(
        ENTRY_TIME_FILTER_KST_ENABLED=True,
        ENTRY_TIME_FILTER_KST_START_HOUR=9,
        ENTRY_TIME_FILTER_KST_END_HOUR=10,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(dataset, params)[-1]

    assert event.feature_snapshot["entry_hour_kst"] == 10
    assert event.strategy_diagnostics["entry_hour_kst"] == 10


def test_kst_entry_time_filter_diagnostics_include_filter_window() -> None:
    dataset = _breakout_dataset_for_decision_ts(_utc_ms_for_kst_time(hour=9, minute=30))
    params = _materialized(
        ENTRY_TIME_FILTER_KST_ENABLED=True,
        ENTRY_TIME_FILTER_KST_START_HOUR=9,
        ENTRY_TIME_FILTER_KST_END_HOUR=11,
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    event = _events(dataset, params)[-1]

    assert event.strategy_diagnostics["entry_time_filter_kst_enabled"] is True
    assert event.strategy_diagnostics["entry_time_filter_kst_start_hour"] == 9
    assert event.strategy_diagnostics["entry_time_filter_kst_end_hour"] == 11
    assert event.feature_snapshot["entry_time_filter_kst_start_hour"] == 9
    assert event.feature_snapshot["entry_time_filter_kst_end_hour"] == 11


def test_kst_time_filter_parameter_space_validates() -> None:
    validate_parameter_space_against_strategy_spec(
        strategy_name=CHANNEL_BREAKOUT_SPEC.strategy_name,
        parameter_space={
            "CHANNEL_BREAKOUT_LOOKBACK": (3,),
            "CHANNEL_BREAKOUT_RANGE_WINDOW": (3,),
            "CHANNEL_BREAKOUT_VOLUME_WINDOW": (3,),
            "ENTRY_TIME_FILTER_KST_ENABLED": (False, True),
            "ENTRY_TIME_FILTER_KST_START_HOUR": (0, 9),
            "ENTRY_TIME_FILTER_KST_END_HOUR": (24, 10, 11),
        },
        deployment_tier="research_only",
    )


def test_three_candidate_kst_time_filter_requires_separate_manifests_because_parameter_space_is_cartesian() -> None:
    aligned_parameter_space = {
        "ENTRY_TIME_FILTER_KST_ENABLED": (False, True, True),
        "ENTRY_TIME_FILTER_KST_START_HOUR": (0, 9, 9),
        "ENTRY_TIME_FILTER_KST_END_HOUR": (24, 10, 11),
    }
    one_candidate_spaces = (
        {
            "CHANNEL_BREAKOUT_LOOKBACK": (3,),
            "CHANNEL_BREAKOUT_RANGE_WINDOW": (3,),
            "CHANNEL_BREAKOUT_VOLUME_WINDOW": (3,),
            "ENTRY_TIME_FILTER_KST_ENABLED": (False,),
            "ENTRY_TIME_FILTER_KST_START_HOUR": (0,),
            "ENTRY_TIME_FILTER_KST_END_HOUR": (24,),
        },
        {
            "CHANNEL_BREAKOUT_LOOKBACK": (3,),
            "CHANNEL_BREAKOUT_RANGE_WINDOW": (3,),
            "CHANNEL_BREAKOUT_VOLUME_WINDOW": (3,),
            "ENTRY_TIME_FILTER_KST_ENABLED": (True,),
            "ENTRY_TIME_FILTER_KST_START_HOUR": (9,),
            "ENTRY_TIME_FILTER_KST_END_HOUR": (10,),
        },
        {
            "CHANNEL_BREAKOUT_LOOKBACK": (3,),
            "CHANNEL_BREAKOUT_RANGE_WINDOW": (3,),
            "CHANNEL_BREAKOUT_VOLUME_WINDOW": (3,),
            "ENTRY_TIME_FILTER_KST_ENABLED": (True,),
            "ENTRY_TIME_FILTER_KST_START_HOUR": (9,),
            "ENTRY_TIME_FILTER_KST_END_HOUR": (11,),
        },
    )

    assert len(iter_parameter_candidates(aligned_parameter_space)) == 27
    for parameter_space in one_candidate_spaces:
        validate_parameter_space_against_strategy_spec(
            strategy_name=CHANNEL_BREAKOUT_SPEC.strategy_name,
            parameter_space=parameter_space,
            deployment_tier="research_only",
        )
        assert len(iter_parameter_candidates(parameter_space)) == 1


def test_candle_utc_day_key_uses_candle_timestamp_only() -> None:
    assert _candle_utc_day_key(Candle(86_399_999, 1.0, 1.0, 1.0, 1.0, 1.0)) == "1970-01-01"
    assert _candle_utc_day_key(Candle(86_400_000, 1.0, 1.0, 1.0, 1.0, 1.0)) == "1970-01-02"


def test_decide_snapshot_accepts_precomputed_arrays() -> None:
    dataset = _dataset()
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )
    prepared = prepare_channel_breakout_context(dataset)

    decision = decide_channel_breakout_snapshot(
        candle=prepared.candles[-1],
        candle_index=len(prepared.candles) - 1,
        dataset=dataset,
        parameter_values=params,
        candles=prepared.candles,
        closes=prepared.closes,
        highs=prepared.highs,
        lows=prepared.lows,
        volumes=prepared.volumes,
    )

    assert decision["signal"] == "BUY"
    assert decision["feature_snapshot"]["rolling_high"] == 104.0


def test_decide_snapshot_legacy_dataset_path_still_works() -> None:
    dataset = _dataset()
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )

    decision = decide_channel_breakout_snapshot(
        candle=dataset.candles[-1],
        candle_index=len(dataset.candles) - 1,
        dataset=dataset,
        parameter_values=params,
    )

    assert decision["signal"] == "BUY"
    assert decision["feature_snapshot"]["rolling_high"] == 104.0


def test_event_builder_precomputes_ohlcv_arrays_once() -> None:
    candles = _CountingCandles(_synthetic_candles(25))
    dataset = _dataset(candles=candles)  # type: ignore[arg-type]
    params = _materialized()

    events = tuple(
        build_channel_breakout_research_events(
            dataset=dataset,
            parameter_values=params,
            fee_rate=0.001,
            slippage_bps=0.0,
            execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
            portfolio_policy=legacy_research_portfolio_policy(),
        )
    )

    assert len(events) == len(candles)
    assert candles.iteration_count == 1


def test_event_builder_does_not_materialize_dataset_per_candle() -> None:
    candles = _CountingCandles(_synthetic_candles(10_000))
    dataset = _dataset(candles=candles)  # type: ignore[arg-type]
    params = _materialized()

    events = tuple(
        build_channel_breakout_research_events(
            dataset=dataset,
            parameter_values=params,
            fee_rate=0.001,
            slippage_bps=0.0,
            execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
            portfolio_policy=legacy_research_portfolio_policy(),
        )
    )

    assert len(events) == len(candles)
    assert candles.iteration_count == 1


def test_channel_breakout_context_builder_used_once_per_backtest(monkeypatch: pytest.MonkeyPatch) -> None:
    import bithumb_bot.strategy_plugins.channel_breakout_research as module

    dataset = _dataset(candles=_synthetic_candles(12))
    calls = 0
    original = module.prepare_channel_breakout_context

    def spy_prepare_context(dataset: DatasetSnapshot):
        nonlocal calls
        calls += 1
        return original(dataset)

    monkeypatch.setattr(module, "prepare_channel_breakout_context", spy_prepare_context)

    events = tuple(
        module.build_channel_breakout_research_events(
            dataset=dataset,
            parameter_values=_materialized(),
            fee_rate=0.001,
            slippage_bps=0.0,
            execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
            portfolio_policy=legacy_research_portfolio_policy(),
        )
    )

    assert len(events) == len(dataset.candles)
    assert calls == 1


def test_channel_breakout_declares_linear_complexity() -> None:
    assert CHANNEL_BREAKOUT_COMPLEXITY_METADATA["complexity_class"] == "linear_precomputed_ohlcv"
    assert CHANNEL_BREAKOUT_COMPLEXITY_METADATA["precompute_required"] is True
    assert (
        getattr(CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN, "complexity_metadata")
        == CHANNEL_BREAKOUT_COMPLEXITY_METADATA
    )


def test_channel_breakout_complexity_metadata_declares_precomputed_ohlcv() -> None:
    assert CHANNEL_BREAKOUT_COMPLEXITY_METADATA["complexity_class"] == "linear_precomputed_ohlcv"
    assert CHANNEL_BREAKOUT_COMPLEXITY_METADATA["precompute_path"] == "prepare_channel_breakout_context"


def test_delayed_confirmation_uses_causal_pending_state_without_future_candle_access() -> None:
    import bithumb_bot.strategy_plugins.channel_breakout_research as module

    source = inspect.getsource(module.build_channel_breakout_research_events)

    assert "prepare_channel_breakout_context(dataset)" in source
    assert "BreakoutPendingState()" in source
    assert "tuple(dataset.candles)" not in source
    assert "list(dataset.candles)" not in source
    assert "_apply_delayed_confirmation_state" in source


_REQUIRED_FEATURE_FIELDS = {
    "schema_version",
    "candle_index",
    "close",
    "rolling_high",
    "breakout_distance",
    "required_breakout_distance",
    "entry_edge_buffer_ratio",
    "fee_rate_used_for_entry_gate",
    "slippage_bps_used_for_entry_gate",
    "current_range",
    "avg_range",
    "range_ratio",
    "volume",
    "avg_volume",
    "volume_ratio",
    "price_regime",
    "volatility_bucket",
    "volume_bucket",
    "liquidity_bucket",
    "composite_regime",
    "blocked_filters",
}


class _CountingCandles:
    def __init__(self, candles: tuple[Candle, ...]) -> None:
        self._candles = candles
        self.iteration_count = 0

    def __iter__(self):
        self.iteration_count += 1
        return iter(self._candles)

    def __len__(self) -> int:
        return len(self._candles)

    def __getitem__(self, index):
        return self._candles[index]


def _synthetic_candles(count: int) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            ts=index * 60_000,
            open=100.0 + index * 0.01,
            high=101.0 + index * 0.01,
            low=99.0 + index * 0.01,
            close=100.5 + index * 0.01,
            volume=100.0 + (index % 10),
        )
        for index in range(count)
    )
