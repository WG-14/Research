import pytest
from market_research.research.causal_market_view import CausalMarketView, FutureMarketAccessError
from tests.test_common_simulation_engine import _dataset


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
