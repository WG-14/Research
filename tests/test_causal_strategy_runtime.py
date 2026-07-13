import pytest
from dataclasses import replace
from market_research.research.causal_market_view import CausalMarketView, FutureMarketAccessError
from tests.test_common_simulation_engine import _dataset
from market_research.research_composition import builtin_strategy_registry
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.strategy_registry import StrategyRegistry
from market_research.strategy_sdk.runtime import make_event_builder_runtime_factory


def test_strategy_cannot_read_future_candle():
    view = CausalMarketView(_dataset(), 1, 120_000)
    with pytest.raises(FutureMarketAccessError, match="future_candle"):
        view.candle(2)


def test_strategy_cannot_read_quote_after_decision_boundary():
    view = CausalMarketView(_dataset(), 1, 1)
    assert all(quote.ts <= 1 for quote in view.quotes())


def test_private_fields_do_not_expose_future_candle_quote_or_depth():
    view = CausalMarketView(_dataset(), 1, 1)
    snapshot = view._causal_snapshot
    assert len(snapshot.candles) == 2
    assert all(quote.ts <= 1 for quote in snapshot.execution_top_of_book_quotes())
    assert all(depth.ts <= 1 for depth in snapshot.orderbook_depth_snapshots)
    assert not hasattr(view, "_dataset")


def test_all_production_builtin_plugins_have_runtime_factory():
    registry = builtin_strategy_registry()
    assert set(registry.plugins) == {
        "sma_with_filter", "buy_and_hold_baseline", "noop_baseline", "threshold_research_only"}
    assert all(plugin.runtime_factory is not None for plugin in registry.plugins.values())


def test_common_runtime_adapter_invokes_current_only_builder_once_per_candle():
    observed_rows = []

    def event_builder(**values):
        observed_rows.append(len(values["dataset"].candles))
        return ()

    base = builtin_strategy_registry().resolve("noop_baseline")
    plugin = replace(
        base,
        event_builder=event_builder,
        runtime_factory=make_event_builder_runtime_factory(
            event_builder,
            current_candle_only=True,
        ),
    )
    registry = StrategyRegistry.build((plugin,))

    run_common_simulation_backtest(
        plugin=plugin,
        registry=registry,
        dataset=_dataset(),
        parameter_values={},
        fee_rate=0,
        slippage_bps=0,
    )

    assert observed_rows == [1] * len(_dataset().candles)
