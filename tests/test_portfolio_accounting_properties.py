from __future__ import annotations

from dataclasses import replace

import pytest

from market_research.research.portfolio_ledger import PortfolioLedger
from tests.test_common_simulation_engine import SpyModel, _run


def test_partial_buy_applies_only_filled_quantity():
    fill = _run(SpyModel()).fills[0]
    partial = replace(fill, fill_id=fill.fill_id+"p", filled_qty=fill.filled_qty/2, remaining_qty=fill.filled_qty/2, fill_status="partial", fee=fill.fee/2)
    ledger = PortfolioLedger(starting_cash=1_000_000)
    ledger.apply(partial)
    assert ledger.asset_qty == pytest.approx(partial.filled_qty)


def test_partial_sell_allocates_cost_basis_proportionally_and_replays():
    buy = _run(SpyModel()).fills[0]
    ledger = PortfolioLedger(starting_cash=1_000_000)
    ledger.apply(buy)
    basis = ledger.cost_basis
    sell = replace(buy, fill_id=buy.fill_id+"s", request_id=buy.request_id+"s", side="SELL", filled_qty=buy.filled_qty/2, remaining_qty=buy.filled_qty/2, fill_status="partial")
    ledger.apply(sell)
    assert ledger.cost_basis == pytest.approx(basis/2)
    assert ledger.asset_qty == pytest.approx(buy.filled_qty/2)
    replay = PortfolioLedger.replay(starting_cash=1_000_000, entries=ledger.entries)
    assert replay == ledger.snapshot()

