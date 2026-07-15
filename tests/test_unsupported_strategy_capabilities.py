from dataclasses import replace

import pytest

from market_research.research.decision_event import OrderIntent, ResearchDecisionEvent
from market_research.research.execution_model import FixedBpsExecutionModel
from market_research.research.experiment_manifest import ExecutionTimingPolicy, legacy_research_portfolio_policy
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy as resolve_research_strategy
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research.strategy_registry import StrategyRegistry
from market_research.research.strategy_contract import StrategyCapabilityContract
from tests.test_common_simulation_engine import _dataset


class SpyModel(FixedBpsExecutionModel):
    def __init__(self):
        super().__init__(0, 0)
        self.count = 0

    def simulate(self, request):
        self.count += 1
        return super().simulate(request)


def _plugin_with_intents(*intents):
    base = resolve_research_strategy("noop_baseline")

    def event_builder(**kwargs):
        candle = kwargs["dataset"].candles[-1]
        events = []
        for index, values in enumerate(intents):
            event = ResearchDecisionEvent(candle_ts=candle.ts, decision_ts=candle.ts + 60_000,
                strategy_name=base.name, strategy_version=base.version, raw_signal=values["side"],
                final_signal=values["side"], reason=f"fixture-{index}", feature_snapshot={"index": index},
                strategy_diagnostics={})
            intent = OrderIntent.from_decision(decision_id=event.decision_id(), **values)
            events.append(replace(event, order_intent=intent))
        return tuple(events)

    return replace(base, event_builder=event_builder, runtime_factory=None)


def _run(plugin, *, positioned=False):
    registry = StrategyRegistry.build((plugin,))
    compiled = StrategyCompiler(registry).compile(strategy_name=plugin.name,
        raw_parameters={}, fee_rate=0, slippage_bps=0)
    model = SpyModel()
    policy = replace(legacy_research_portfolio_policy(), initial_position_qty=1.0) if positioned else None
    with pytest.raises(ValueError) as caught:
        run_common_simulation_backtest(plugin=plugin, registry=registry, compiled_contract=compiled,
            dataset=_dataset(), parameter_values={}, fee_rate=0, slippage_bps=0,
            execution_model=model, portfolio_policy=policy)
    assert model.count == 0
    return str(caught.value)


def test_ambiguous_sell_sizing_is_rejected_before_execution():
    reason = _run(_plugin_with_intents({"side": "SELL"}), positioned=True)
    assert "partial_or_ambiguous_exit" in reason


def test_partial_quantity_sell_is_rejected_before_execution():
    reason = _run(_plugin_with_intents({"side": "SELL", "sizing": "explicit_quantity",
                                       "requested_qty": .5}), positioned=True)
    assert "partial_or_ambiguous_exit" in reason


def test_short_intent_is_not_converted_to_sell():
    assert "direction_rejected" in _run(_plugin_with_intents({"side": "SHORT"}))


def test_pyramiding_intent_fails_before_model_invocation():
    assert "pyramiding_rejected" in _run(_plugin_with_intents(
        {"side": "BUY", "sizing": "portfolio_policy_fractional_cash"}), positioned=True)


def test_multiple_intents_are_not_silently_dropped():
    assert "multiple_intents" in _run(_plugin_with_intents(
        {"side": "BUY", "sizing": "portfolio_policy_fractional_cash"},
        {"side": "BUY", "sizing": "portfolio_policy_fractional_cash"}))


def test_explicit_full_position_sell_is_accepted():
    plugin = _plugin_with_intents({"side": "SELL", "sizing": "full_position"})
    registry = StrategyRegistry.build((plugin,))
    compiled = StrategyCompiler(registry).compile(strategy_name=plugin.name,
        raw_parameters={}, fee_rate=0, slippage_bps=0)
    model = SpyModel()
    data = _dataset()
    data = replace(data, candles=data.candles[:1])
    run = run_common_simulation_backtest(plugin=plugin, registry=registry, compiled_contract=compiled,
        dataset=data, parameter_values={}, fee_rate=0, slippage_bps=0, execution_model=model,
        portfolio_policy=replace(legacy_research_portfolio_policy(), initial_position_qty=1.0),
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="candle_close_legacy",
            allow_same_candle_close_fill=True,
            source="explicit_test_legacy_opt_in",
        ))
    assert model.count == 1
    assert run.execution_requests[0].requested_qty == 1.0
    assert run.ledger_entries[0].side == "SELL"


def test_declared_partial_exit_uses_common_execution_and_ledger_path():
    plugin = replace(
        _plugin_with_intents({
            "side": "SELL",
            "sizing": "explicit_quantity",
            "requested_qty": 0.5,
        }),
        required_capabilities=StrategyCapabilityContract(partial_exit=True),
    )
    registry = StrategyRegistry.build((plugin,))
    compiled = StrategyCompiler(registry).compile(
        strategy_name=plugin.name,
        raw_parameters={},
        fee_rate=0,
        slippage_bps=0,
    )
    model = SpyModel()
    data = replace(_dataset(), candles=_dataset().candles[:1])

    run = run_common_simulation_backtest(
        plugin=plugin,
        registry=registry,
        compiled_contract=compiled,
        dataset=data,
        parameter_values={},
        fee_rate=0,
        slippage_bps=0,
        execution_model=model,
        portfolio_policy=replace(
            legacy_research_portfolio_policy(), initial_position_qty=1.0
        ),
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="candle_close_legacy",
            allow_same_candle_close_fill=True,
            source="explicit_test_legacy_opt_in",
        ),
    )

    assert model.count == 1
    assert run.ledger_entries[0].qty == 0.5
    assert run.resource_usage["final_asset_qty"] == 0.5


def test_declared_partial_exit_cannot_exceed_available_position():
    plugin = replace(
        _plugin_with_intents({
            "side": "SELL",
            "sizing": "explicit_quantity",
            "requested_qty": 1.5,
        }),
        required_capabilities=StrategyCapabilityContract(partial_exit=True),
    )

    reason = _run(plugin, positioned=True)

    assert "partial_exit_quantity_exceeds_position" in reason
