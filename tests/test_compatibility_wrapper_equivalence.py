from market_research.research.backtest_engine import run_buy_and_hold_baseline_backtest
from market_research.research_composition import builtin_strategy_registry
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy as resolve_research_strategy
from tests.test_common_simulation_engine import _dataset


def test_compatibility_wrapper_stream_hashes_equal_direct_engine():
    values = dict(dataset=_dataset(), parameter_values={"BUY_HOLD_BUY_INDEX": 1},
                  fee_rate=.001, slippage_bps=10)
    wrapped = run_buy_and_hold_baseline_backtest(
        **values, strategy_registry=builtin_strategy_registry()
    )
    direct = run_common_simulation_backtest(plugin=resolve_research_strategy("buy_and_hold_baseline"), **values)
    for key in ("execution_request_stream_hash", "execution_fill_stream_hash", "ledger_stream_hash"):
        assert wrapped.execution_event_summary[key] == direct.execution_event_summary[key]
    assert wrapped.metrics_hash == direct.metrics_hash
