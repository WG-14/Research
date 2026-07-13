from market_research.research.portfolio_ledger import PortfolioLedger
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.strategy_catalog import resolve_research_strategy
from tests.test_common_simulation_engine import _dataset


def test_ledger_replay_matches_final_runtime_snapshot():
    run = run_common_simulation_backtest(plugin=resolve_research_strategy("buy_and_hold_baseline"),
        dataset=_dataset(), parameter_values={"BUY_HOLD_BUY_INDEX": 1}, fee_rate=.001, slippage_bps=10)
    replay = PortfolioLedger.replay(starting_cash=1_000_000.0, entries=run.ledger_entries)
    assert replay.cash == run.resource_usage["final_cash"]
    assert replay.asset_qty == run.resource_usage["final_asset_qty"]


def test_legacy_pending_fill_helper_is_not_public():
    import market_research.research.backtest_common as common
    import market_research.research.backtest_support as support
    assert not hasattr(common, "apply_pending_fills")
    assert not hasattr(support, "apply_pending_fills")
