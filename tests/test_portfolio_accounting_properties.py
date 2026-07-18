from __future__ import annotations

from dataclasses import replace

import pytest

from market_research.research.metrics_contract import build_metrics_v2
from market_research.research.execution_model import FixedBpsExecutionModel
from market_research.research.experiment_manifest import ExecutionTimingPolicy
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy
from market_research.research.portfolio_ledger import PortfolioLedger
from tests.test_common_simulation_engine import SpyModel, _dataset, _run


def test_partial_buy_applies_only_filled_quantity():
    fill = _run(SpyModel()).fills[0]
    partial = replace(
        fill,
        fill_id="",
        filled_qty=fill.filled_qty / 2,
        remaining_qty=fill.filled_qty / 2,
        fill_status="partial",
        fee=fill.fee / 2,
    )
    ledger = PortfolioLedger(starting_cash=1_000_000)
    ledger.apply(partial)
    assert ledger.asset_qty == pytest.approx(partial.filled_qty)


def test_partial_sell_allocates_cost_basis_proportionally_and_replays():
    model = SpyModel()
    buy = _run(model).fills[0]
    ledger = PortfolioLedger(starting_cash=1_000_000)
    ledger.apply(buy)
    basis = ledger.cost_basis
    sell_request = replace(model.requests[0], request_id="", side="SELL")
    sell = replace(
        buy,
        fill_id="",
        request_id=sell_request.request_id,
        side="SELL",
        filled_qty=buy.filled_qty / 2,
        remaining_qty=buy.filled_qty / 2,
        fill_status="partial",
    )
    ledger.apply(sell)
    assert ledger.cost_basis == pytest.approx(basis / 2)
    assert ledger.asset_qty == pytest.approx(buy.filled_qty / 2)
    replay = PortfolioLedger.replay(starting_cash=1_000_000, entries=ledger.entries)
    assert replay == ledger.snapshot()


def test_partial_sell_realized_and_unrealized_pnl_reconcile_to_total_pnl():
    model = SpyModel()
    buy = _run(model).fills[0]
    ledger = PortfolioLedger(starting_cash=1_000_000)
    ledger.apply(buy)
    sell_request = replace(model.requests[0], request_id="", side="SELL")
    sell = replace(
        buy,
        fill_id="",
        request_id=sell_request.request_id,
        side="SELL",
        filled_qty=buy.filled_qty / 2,
        remaining_qty=buy.filled_qty / 2,
        fill_status="partial",
        fee=buy.fee / 2,
    )
    ledger.apply(sell)
    snapshot = ledger.snapshot()
    mark_price = float(sell.avg_fill_price)
    metrics = build_metrics_v2(
        starting_cash=1_000_000,
        final_cash=snapshot.cash,
        final_asset_qty=snapshot.asset_qty,
        final_mark_price=mark_price,
        final_open_cost_basis=snapshot.cost_basis,
        accounting_realized_pnl=snapshot.realized_pnl,
        equity_curve=(),
        position_intervals=(),
        closed_trades=(),
        execution_records=(),
    )
    total_pnl = snapshot.cash + snapshot.asset_qty * mark_price - 1_000_000
    assert metrics.return_risk.realized_return_pct == pytest.approx(
        snapshot.realized_pnl / 1_000_000 * 100
    )
    assert (
        snapshot.realized_pnl + metrics.return_risk.unrealized_pnl_end
        == pytest.approx(total_pnl)
    )


def test_execution_cost_breakdown_is_explicit_about_modeled_scope():
    fill = _run(SpyModel()).fills[0]
    costs = fill.cost_breakdown().as_dict()
    assert costs["fee_cash_debit"] == fill.fee
    assert costs["slippage_embedded"] == pytest.approx(
        abs(float(fill.avg_fill_price) - fill.reference_price) * fill.filled_qty
    )
    assert costs["cash_debit_total"] == fill.fee
    assert costs["not_applicable_components"] == ["tax", "borrow", "rollover"]
    assert set(costs["unavailable_components"]) == {"market_impact", "spread"}


def test_higher_common_execution_costs_do_not_increase_net_return():
    common = {
        "plugin": resolve_builtin_strategy("buy_and_hold_baseline"),
        "dataset": _dataset(),
        "parameter_values": {"BUY_HOLD_BUY_INDEX": 1},
        "execution_timing_policy": ExecutionTimingPolicy(),
    }
    zero = run_common_simulation_backtest(
        **common,
        fee_rate=0.0,
        slippage_bps=0.0,
        execution_model=FixedBpsExecutionModel(0.0, 0.0),
    )
    costly = run_common_simulation_backtest(
        **common,
        fee_rate=0.01,
        slippage_bps=100.0,
        execution_model=FixedBpsExecutionModel(0.01, 100.0),
    )
    assert costly.metrics.return_pct <= zero.metrics.return_pct


@pytest.mark.parametrize(
    ("starting_cash", "initial_qty", "reason"),
    (
        (-1.0, 0.0, "ledger_starting_cash_invalid"),
        (float("nan"), 0.0, "ledger_starting_cash_invalid"),
        (1.0, -1.0, "ledger_initial_position_qty_invalid"),
        (1.0, float("inf"), "ledger_initial_position_qty_invalid"),
    ),
)
def test_ledger_rejects_invalid_initial_state(starting_cash, initial_qty, reason):
    with pytest.raises(ValueError, match=reason):
        PortfolioLedger(
            starting_cash=starting_cash,
            initial_position_qty=initial_qty,
        )


def test_ledger_rejects_out_of_order_fill_without_partial_mutation():
    fill = _run(SpyModel()).fills[0]
    first = replace(
        fill,
        fill_id="",
        request_id="ordered-buy",
        portfolio_effective_ts=200_000,
    )
    second = replace(
        fill,
        fill_id="",
        request_id="backward-sell",
        side="SELL",
        filled_qty=fill.filled_qty / 2,
        remaining_qty=fill.filled_qty / 2,
        fill_status="partial",
        fee=fill.fee / 2,
        portfolio_effective_ts=199_999,
    )
    ledger = PortfolioLedger(starting_cash=1_000_000)
    ledger.apply(first)
    before = ledger.snapshot()

    with pytest.raises(ValueError, match="ledger_fill_timestamp_out_of_order"):
        ledger.apply(second)

    assert ledger.snapshot() == before
    assert len(ledger.entries) == 1


def test_ledger_replay_rejects_identity_and_non_finite_tampering():
    fill = _run(SpyModel()).fills[0]
    ledger = PortfolioLedger(starting_cash=1_000_000)
    entry = ledger.apply(fill)
    assert entry is not None

    with pytest.raises(ValueError, match="ledger_entry_id_content_mismatch"):
        PortfolioLedger.replay(
            starting_cash=1_000_000,
            entries=(replace(entry, fill_id="tampered-fill"),),
        )
    with pytest.raises(ValueError, match="ledger_replay_non_finite_transaction"):
        PortfolioLedger.replay(
            starting_cash=1_000_000,
            entries=(replace(entry, slippage=float("nan")),),
        )
