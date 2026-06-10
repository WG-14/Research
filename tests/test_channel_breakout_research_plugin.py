from __future__ import annotations

import pytest

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.execution_timing import candle_close_ts
from bithumb_bot.research.experiment_manifest import DateRange, ExecutionTimingPolicy, legacy_research_portfolio_policy
from bithumb_bot.research.strategy_registry import resolve_research_strategy_plugin, strategy_runtime_capability_issues
from bithumb_bot.research.strategy_spec import StrategySpecError
from bithumb_bot.research.strategy_spec import exit_policy_from_parameters, exit_policy_hash
from bithumb_bot.strategy_contract_testing import assert_research_only_contract
from bithumb_bot.strategy_plugin_inventory import build_strategy_plugin_inventory
from bithumb_bot.strategy_plugins.channel_breakout_research import (
    CHANNEL_BREAKOUT_COMPLEXITY_METADATA,
    CHANNEL_BREAKOUT_SPEC,
    CHANNEL_BREAKOUT_WITH_REGIME_FILTER_PLUGIN,
    build_channel_breakout_research_events,
    decide_channel_breakout_snapshot,
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


def test_new_entry_mode_is_behavior_affecting_parameter() -> None:
    assert "ENTRY_MODE" in CHANNEL_BREAKOUT_SPEC.behavior_affecting_parameter_names


def test_pullback_mode_does_not_emit_buy_on_initial_breakout_candle() -> None:
    dataset = _dataset()
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
        ENTRY_MODE="pullback_after_breakout",
    )

    decision = decide_channel_breakout_snapshot(
        candle=dataset.candles[-1],
        candle_index=len(dataset.candles) - 1,
        dataset=dataset,
        parameter_values=params,
    )

    assert decision["signal"] == "HOLD"
    assert "pullback_after_breakout_waiting_for_pullback" in decision["feature_snapshot"]["blocked_filters"]


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


def test_exit_policy_hash_changes_when_trailing_stop_changes() -> None:
    base = _materialized(TRAILING_STOP_RATIO=0.01)
    changed = _materialized(TRAILING_STOP_RATIO=0.02)

    assert "TRAILING_STOP_RATIO" in CHANNEL_BREAKOUT_SPEC.accepted_parameter_names
    assert "TRAILING_STOP_RATIO" in CHANNEL_BREAKOUT_SPEC.behavior_affecting_parameter_names
    assert exit_policy_hash(exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, base)) != exit_policy_hash(
        exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, changed)
    )


def test_exit_policy_hash_changes_when_break_even_stop_changes() -> None:
    base = _materialized(BREAK_EVEN_STOP_ENABLED=False)
    changed = _materialized(BREAK_EVEN_STOP_ENABLED=True)

    assert "BREAK_EVEN_STOP_ENABLED" in CHANNEL_BREAKOUT_SPEC.accepted_parameter_names
    assert "BREAK_EVEN_STOP_ENABLED" in CHANNEL_BREAKOUT_SPEC.behavior_affecting_parameter_names
    assert exit_policy_hash(exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, base)) != exit_policy_hash(
        exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, changed)
    )


def test_exit_policy_hash_changes_when_opposite_signal_exit_changes() -> None:
    base = _materialized(OPPOSITE_SIGNAL_EXIT_ENABLED=False)
    changed = _materialized(OPPOSITE_SIGNAL_EXIT_ENABLED=True)

    assert "OPPOSITE_SIGNAL_EXIT_ENABLED" in CHANNEL_BREAKOUT_SPEC.accepted_parameter_names
    assert "OPPOSITE_SIGNAL_EXIT_ENABLED" in CHANNEL_BREAKOUT_SPEC.behavior_affecting_parameter_names
    assert exit_policy_hash(exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, base)) != exit_policy_hash(
        exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, changed)
    )


def test_exit_policy_hash_changes_when_regime_change_exit_changes() -> None:
    base = _materialized(REGIME_CHANGE_EXIT_ENABLED=False)
    changed = _materialized(REGIME_CHANGE_EXIT_ENABLED=True)

    assert "REGIME_CHANGE_EXIT_ENABLED" in CHANNEL_BREAKOUT_SPEC.accepted_parameter_names
    assert "REGIME_CHANGE_EXIT_ENABLED" in CHANNEL_BREAKOUT_SPEC.behavior_affecting_parameter_names
    assert exit_policy_hash(exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, base)) != exit_policy_hash(
        exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, changed)
    )


def test_existing_stop_loss_max_holding_behavior_is_unchanged() -> None:
    values = _materialized()
    policy = exit_policy_from_parameters(CHANNEL_BREAKOUT_SPEC.strategy_name, values)

    assert policy["common_rules"] == ["stop_loss", "max_holding_time"]
    assert policy["stop_loss"]["stop_loss_ratio"] == 0.01
    assert policy["max_holding_time"]["max_holding_min"] == 30


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


def test_event_builder_emits_required_event_fields() -> None:
    dataset = _dataset()
    params = _materialized(
        CHANNEL_BREAKOUT_RANGE_RATIO_MIN=1.0,
        CHANNEL_BREAKOUT_VOLUME_RATIO_MIN=1.0,
    )
    timing_policy = ExecutionTimingPolicy(decision_guard_ms=250)

    events = build_channel_breakout_research_events(
        dataset=dataset,
        parameter_values=params,
        fee_rate=0.001,
        slippage_bps=0.0,
        execution_timing_policy=timing_policy,
        portfolio_policy=legacy_research_portfolio_policy(),
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
    assert event.order_intent == {"side": "BUY", "sizing": "portfolio_policy_fractional_cash"}
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

    events = build_channel_breakout_research_events(
        dataset=dataset,
        parameter_values=params,
        fee_rate=0.001,
        slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
        portfolio_policy=legacy_research_portfolio_policy(),
    )

    assert len(events) == len(candles)
    assert candles.iteration_count == 1


def test_event_builder_does_not_materialize_dataset_per_candle() -> None:
    candles = _CountingCandles(_synthetic_candles(10_000))
    dataset = _dataset(candles=candles)  # type: ignore[arg-type]
    params = _materialized()

    events = build_channel_breakout_research_events(
        dataset=dataset,
        parameter_values=params,
        fee_rate=0.001,
        slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
        portfolio_policy=legacy_research_portfolio_policy(),
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

    events = module.build_channel_breakout_research_events(
        dataset=dataset,
        parameter_values=_materialized(),
        fee_rate=0.001,
        slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(decision_guard_ms=0),
        portfolio_policy=legacy_research_portfolio_policy(),
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


_REQUIRED_FEATURE_FIELDS = {
    "schema_version",
    "candle_index",
    "close",
    "rolling_high",
    "breakout_distance",
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
