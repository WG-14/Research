from dataclasses import replace

import pytest

from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import (
    resolve_builtin_strategy as resolve_research_strategy,
)
from tests.test_common_simulation_engine import _dataset
from market_research.research.dataset_snapshot import TopOfBookQuote
from market_research.research.experiment_manifest import ExecutionTimingPolicy


def _run():
    return run_common_simulation_backtest(
        plugin=resolve_research_strategy("buy_and_hold_baseline"),
        dataset=_dataset(),
        parameter_values={"BUY_HOLD_BUY_INDEX": 1},
        fee_rate=0.001,
        slippage_bps=10,
    )


def test_every_effective_mutating_fill_has_one_ledger_entry():
    run = _run()
    effective = {
        fill.fill_id
        for fill in run.fills
        if fill.fill_status in {"filled", "partial"} and fill.filled_qty > 0
    }
    applied = [entry.fill_id for entry in run.ledger_entries]
    assert effective == set(applied)
    assert len(applied) == len(set(applied))


def test_no_ledger_entry_exists_without_effective_fill():
    run = _run()
    corrupted = replace(run, fills=())
    with pytest.raises(ValueError, match="orphan_ledger_entry"):
        corrupted.validate_execution_lineage()


def test_trade_projection_mutation_does_not_change_ledger():
    run = _run()
    before = tuple(run.ledger_entries)
    run.trades[0]["cash"] = -999
    assert run.ledger_entries == before


def test_pending_after_dataset_fill_has_explicit_pending_evidence():
    data = _dataset()
    quote = TopOfBookQuote(
        ts=301_000,
        pair="KRW-BTC",
        bid_price=104,
        ask_price=105,
        spread_bps=10,
        source="fixture",
    )
    data = replace(data, top_of_book_event_quotes=(quote,))
    run = run_common_simulation_backtest(
        plugin=resolve_research_strategy("buy_and_hold_baseline"),
        dataset=data,
        parameter_values={"BUY_HOLD_BUY_INDEX": 4},
        fee_rate=0,
        slippage_bps=0,
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="first_orderbook_after_decision",
            max_quote_wait_ms=3000,
        ),
    )
    assert len(run.fills) == 1 and not run.ledger_entries
    pending = next(
        trade for trade in run.trades if trade.get("fill_id") == run.fills[0].fill_id
    )
    assert pending["pending_execution_at_end"] is True
    assert pending["pending_execution_after_dataset_end"] is True
    run.validate_execution_lineage()


def test_after_period_fill_costs_are_excluded_from_performance_accounting():
    data = _dataset()
    quote = TopOfBookQuote(
        ts=301_000,
        pair="KRW-BTC",
        bid_price=104,
        ask_price=105,
        spread_bps=10,
        source="fixture",
    )
    data = replace(data, top_of_book_event_quotes=(quote,))
    run = run_common_simulation_backtest(
        plugin=resolve_research_strategy("buy_and_hold_baseline"),
        dataset=data,
        parameter_values={"BUY_HOLD_BUY_INDEX": 4},
        fee_rate=0.001,
        slippage_bps=10,
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="first_orderbook_after_decision",
            max_quote_wait_ms=3000,
        ),
    )
    assert run.fills[0].fee > 0.0
    assert not run.ledger_entries
    assert run.metrics.fee_total == 0.0
    assert run.metrics_v2.cost_execution.fee_total == 0.0
    assert run.metrics_v2.cost_execution.slippage_total == 0.0
    assert run.metrics_v2.trade_quality.execution_count == 0
