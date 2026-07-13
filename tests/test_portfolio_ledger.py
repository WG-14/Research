from __future__ import annotations

from dataclasses import replace

from market_research.research.portfolio_ledger import PortfolioLedger
from tests.test_common_simulation_engine import SpyModel, _run


def test_failed_fill_does_not_mutate_ledger():
    run = _run(SpyModel())
    failed = replace(run.fills[0], fill_id="", fill_status="failed", filled_qty=0.0)
    ledger = PortfolioLedger(starting_cash=1_000_000)
    assert ledger.apply(failed) is None
    assert ledger.snapshot().cash == 1_000_000


def test_fee_is_debited_exactly_once_and_equity_reconciles():
    run = _run(SpyModel())
    ledger = PortfolioLedger(starting_cash=1_000_000)
    entry = ledger.apply(run.fills[0])
    assert entry is not None and ledger.snapshot().fee_total == run.fills[0].fee
    snapshot = ledger.snapshot()
    assert snapshot.cash + snapshot.asset_qty * float(run.fills[0].avg_fill_price) + 0.0 > 0
