from dataclasses import replace

import pytest

from market_research.research.decision_event import OrderIntent, ResearchDecisionEvent
from market_research.research.execution_model import FixedBpsExecutionModel
from market_research.research.experiment_manifest import (
    ExecutionTimingPolicy,
    legacy_research_portfolio_policy,
)
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import (
    resolve_builtin_strategy as resolve_research_strategy,
)
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
            event = ResearchDecisionEvent(
                candle_ts=candle.ts,
                decision_ts=candle.ts + 60_000,
                strategy_name=base.name,
                strategy_version=base.version,
                raw_signal=values["side"],
                final_signal=values["side"],
                reason=f"fixture-{index}",
                feature_snapshot={"index": index},
                strategy_diagnostics={},
            )
            intent = OrderIntent.from_decision(
                decision_id=event.decision_id(), **values
            )
            events.append(replace(event, order_intent=intent))
        return tuple(events)

    return replace(base, event_builder=event_builder, runtime_factory=None)


def _with_funded_position(plugin):
    target_event_builder = plugin.event_builder

    def event_builder(**kwargs):
        candle_count = len(kwargs["dataset"].candles)
        if candle_count == 1:
            candle = kwargs["dataset"].candles[-1]
            event = ResearchDecisionEvent(
                candle_ts=candle.ts,
                decision_ts=candle.ts + 60_000,
                strategy_name=plugin.name,
                strategy_version=plugin.version,
                raw_signal="BUY",
                final_signal="BUY",
                entry_signal="BUY",
                reason="funded-position-fixture",
                feature_snapshot={},
                strategy_diagnostics={},
            )
            intent = OrderIntent.from_decision(
                decision_id=event.decision_id(),
                side="BUY",
                sizing="portfolio_policy_fractional_cash",
            )
            return (replace(event, order_intent=intent),)
        if candle_count == 2:
            assert target_event_builder is not None
            return target_event_builder(**kwargs)
        return ()

    return replace(plugin, event_builder=event_builder, runtime_factory=None)


def _funded_position_policy():
    base = legacy_research_portfolio_policy()
    return replace(
        base,
        starting_cash_krw=101.0,
        position_sizing=replace(
            base.position_sizing,
            buy_fraction=1.0,
            cash_buffer_policy="none_before_fees",
        ),
        source="funded_position_fixture",
    )


def _run(plugin, *, positioned=False):
    if positioned:
        plugin = _with_funded_position(plugin)
    registry = StrategyRegistry.build((plugin,))
    compiled = StrategyCompiler(registry).compile(
        strategy_name=plugin.name, raw_parameters={}, fee_rate=0, slippage_bps=0
    )
    model = SpyModel()
    policy = _funded_position_policy() if positioned else None
    with pytest.raises(ValueError) as caught:
        run_common_simulation_backtest(
            plugin=plugin,
            registry=registry,
            compiled_contract=compiled,
            dataset=_dataset(),
            parameter_values={},
            fee_rate=0,
            slippage_bps=0,
            execution_model=model,
            portfolio_policy=policy,
        )
    assert model.count == int(positioned)
    return str(caught.value)


def test_ambiguous_sell_sizing_is_rejected_before_execution():
    reason = _run(_plugin_with_intents({"side": "SELL"}), positioned=True)
    assert "partial_or_ambiguous_exit" in reason


def test_partial_quantity_sell_is_rejected_before_execution():
    reason = _run(
        _plugin_with_intents(
            {"side": "SELL", "sizing": "explicit_quantity", "requested_qty": 0.5}
        ),
        positioned=True,
    )
    assert "partial_or_ambiguous_exit" in reason


def test_short_intent_is_not_converted_to_sell():
    assert "direction_rejected" in _run(_plugin_with_intents({"side": "SHORT"}))


def test_pyramiding_intent_fails_before_model_invocation():
    assert "pyramiding_rejected" in _run(
        _plugin_with_intents(
            {"side": "BUY", "sizing": "portfolio_policy_fractional_cash"}
        ),
        positioned=True,
    )


def test_multiple_intents_are_not_silently_dropped():
    assert "multiple_intents" in _run(
        _plugin_with_intents(
            {"side": "BUY", "sizing": "portfolio_policy_fractional_cash"},
            {"side": "BUY", "sizing": "portfolio_policy_fractional_cash"},
        )
    )


def test_explicit_full_position_sell_is_accepted():
    plugin = _with_funded_position(
        _plugin_with_intents({"side": "SELL", "sizing": "full_position"})
    )
    registry = StrategyRegistry.build((plugin,))
    compiled = StrategyCompiler(registry).compile(
        strategy_name=plugin.name, raw_parameters={}, fee_rate=0, slippage_bps=0
    )
    model = SpyModel()
    data = _dataset()
    data = replace(data, candles=data.candles[:3])
    run = run_common_simulation_backtest(
        plugin=plugin,
        registry=registry,
        compiled_contract=compiled,
        dataset=data,
        parameter_values={},
        fee_rate=0,
        slippage_bps=0,
        execution_model=model,
        portfolio_policy=_funded_position_policy(),
        execution_timing_policy=ExecutionTimingPolicy(),
    )
    assert model.count == 2
    assert run.execution_requests[1].requested_qty == pytest.approx(1.0)
    assert [entry.side for entry in run.ledger_entries] == ["BUY", "SELL"]
    assert run.resource_usage["final_asset_qty"] == pytest.approx(0.0)
    assert run.resource_usage["final_cash"] == pytest.approx(102.0)


def test_declared_partial_exit_uses_common_execution_and_ledger_path():
    plugin = _with_funded_position(
        replace(
            _plugin_with_intents(
                {
                    "side": "SELL",
                    "sizing": "explicit_quantity",
                    "requested_qty": 0.5,
                }
            ),
            required_capabilities=StrategyCapabilityContract(partial_exit=True),
        ),
    )
    registry = StrategyRegistry.build((plugin,))
    compiled = StrategyCompiler(registry).compile(
        strategy_name=plugin.name,
        raw_parameters={},
        fee_rate=0,
        slippage_bps=0,
    )
    model = SpyModel()
    data = replace(_dataset(), candles=_dataset().candles[:3])

    run = run_common_simulation_backtest(
        plugin=plugin,
        registry=registry,
        compiled_contract=compiled,
        dataset=data,
        parameter_values={},
        fee_rate=0,
        slippage_bps=0,
        execution_model=model,
        portfolio_policy=_funded_position_policy(),
        execution_timing_policy=ExecutionTimingPolicy(),
    )

    assert model.count == 2
    assert [entry.side for entry in run.ledger_entries] == ["BUY", "SELL"]
    assert run.ledger_entries[1].qty == pytest.approx(0.5)
    assert run.ledger_entries[1].cost_basis_after == pytest.approx(50.5)
    assert run.ledger_entries[1].realized_pnl_after == pytest.approx(0.5)
    assert run.resource_usage["final_cash"] == pytest.approx(51.0)
    assert run.resource_usage["final_asset_qty"] == pytest.approx(0.5)
    assert run.resource_usage["final_marked_equity"] == pytest.approx(102.0)


def test_declared_partial_exit_cannot_exceed_available_position():
    plugin = replace(
        _plugin_with_intents(
            {
                "side": "SELL",
                "sizing": "explicit_quantity",
                "requested_qty": 1.5,
            }
        ),
        required_capabilities=StrategyCapabilityContract(partial_exit=True),
    )

    reason = _run(plugin, positioned=True)

    assert "partial_exit_quantity_exceeds_position" in reason
