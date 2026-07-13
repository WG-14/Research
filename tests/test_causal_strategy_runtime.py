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
