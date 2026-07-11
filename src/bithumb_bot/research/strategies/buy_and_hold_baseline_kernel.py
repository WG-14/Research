"""Research-only cash/quantity kernel for the buy-and-hold baseline."""

from __future__ import annotations

from typing import Any

from ..backtest_types import BacktestRun, BacktestRunContext
from ..dataset_snapshot import DatasetSnapshot
from ..experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy, legacy_research_portfolio_policy
from ..hashing import sha256_prefixed
from ..metrics import ResearchMetrics
from ..metrics_contract import EquityPoint, ExecutionRecord, PositionInterval, build_metrics_v2
from ..strategy_spec import materialize_strategy_parameters
from .buy_and_hold_baseline_events import build_buy_and_hold_baseline_events


def run_buy_and_hold_baseline_backtest(
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    parameter_stability_score: float | None = None,
    execution_model: Any | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
) -> BacktestRun:
    """Apply the single BUY once, then retain the open position to the final mark."""
    del execution_model, context
    params = materialize_strategy_parameters(
        "buy_and_hold_baseline",
        parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )
    policy = portfolio_policy or legacy_research_portfolio_policy()
    timing = execution_timing_policy or ExecutionTimingPolicy()
    events = build_buy_and_hold_baseline_events(
        dataset=dataset,
        parameter_values=params,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        execution_timing_policy=timing,
        portfolio_policy=policy,
    )
    candles = {int(candle.ts): candle for candle in dataset.candles}
    cash = float(policy.starting_cash_krw)
    qty = float(policy.initial_position_qty)
    starting_cash = cash
    entry_cost_basis = 0.0
    buy_executed = False
    peak_equity = cash + qty * (float(dataset.candles[0].close) if dataset.candles else 0.0)
    max_drawdown = 0.0
    fee_total = 0.0
    slippage_total = 0.0
    trades: list[dict[str, object]] = []
    executions: list[ExecutionRecord] = []
    equity: list[EquityPoint] = []
    intervals: list[PositionInterval] = []
    decisions: list[dict[str, object]] = []

    for event in events:
        candle = candles[int(event.candle_ts)]
        reference_price = float(candle.close)
        final_signal = "HOLD"
        if event.entry_signal == "BUY" and not buy_executed and cash > 0.0:
            notional = cash * float(policy.position_sizing.buy_fraction)
            fill_price = reference_price * (1.0 + float(slippage_bps) / 10_000.0)
            fee = notional * float(fee_rate)
            fill_qty = ((notional - fee) / fill_price) if fill_price > 0.0 else 0.0
            if fill_qty > 0.0:
                cash -= notional
                qty += fill_qty
                entry_cost_basis += notional
                fee_total += fee
                slippage = max(0.0, (fill_price - reference_price) * fill_qty)
                slippage_total += slippage
                buy_executed = True
                final_signal = "BUY"
                trades.append(
                    {
                        "ts": int(event.candle_ts),
                        "side": "BUY",
                        "price": fill_price,
                        "reference_price": reference_price,
                        "notional": notional,
                        "asset_qty": fill_qty,
                        "qty": fill_qty,
                        "fee": fee,
                        "slippage": slippage,
                        "cash": cash,
                        "final_position_marked_to_market": True,
                        "exit_policy": "no_explicit_exit",
                    }
                )
                executions.append(
                    ExecutionRecord(
                        "BUY", "filled", fill_qty, fill_price, fee, slippage,
                        ts=int(event.candle_ts),
                    )
                )
                intervals.append(PositionInterval(open_ts=int(event.candle_ts)))
        mark_equity = cash + qty * reference_price
        peak_equity = max(peak_equity, mark_equity)
        max_drawdown = max(
            max_drawdown,
            ((peak_equity - mark_equity) / peak_equity * 100.0) if peak_equity else 0.0,
        )
        equity.append(EquityPoint(int(event.candle_ts), mark_equity, cash, qty))
        decisions.append(
            {
                "strategy_name": "buy_and_hold_baseline",
                "candle_ts": int(event.candle_ts),
                "decision_ts": int(event.decision_ts),
                "raw_signal": event.raw_signal,
                "entry_signal": event.entry_signal,
                "exit_signal": "HOLD",
                "final_signal": final_signal,
                "reason": event.reason,
                "feature_snapshot": dict(event.feature_snapshot),
                "strategy_diagnostics": dict(event.strategy_diagnostics),
                "exit_policy": "no_explicit_exit",
                "open_position_at_end": bool(qty > 0.0),
                "final_position_marked_to_market": True,
            }
        )

    last_price = float(dataset.candles[-1].close) if dataset.candles else 0.0
    metrics_v2 = build_metrics_v2(
        starting_cash=starting_cash,
        final_cash=cash,
        final_asset_qty=qty,
        final_mark_price=last_price,
        equity_curve=tuple(equity),
        position_intervals=tuple(intervals),
        closed_trades=(),
        execution_records=tuple(executions),
        final_open_cost_basis=entry_cost_basis,
        summary_max_drawdown_pct=max_drawdown,
    )
    quality = metrics_v2.trade_quality
    metrics = ResearchMetrics(
        return_pct=metrics_v2.return_risk.total_return_pct,
        max_drawdown_pct=max_drawdown,
        profit_factor=quality.profit_factor,
        profit_factor_unbounded=quality.profit_factor_unbounded,
        trade_count=quality.closed_trade_count,
        win_rate=quality.win_rate,
        avg_win=quality.avg_win,
        avg_loss=quality.avg_loss,
        fee_total=fee_total,
        slippage_total=slippage_total,
        max_consecutive_losses=quality.max_consecutive_losses,
        single_trade_dependency_score=quality.single_trade_dependency_score,
        parameter_stability_score=parameter_stability_score,
    )
    return BacktestRun(
        metrics=metrics,
        metrics_v2=metrics_v2,
        trades=tuple(trades),
        candle_count=len(dataset.candles),
        warnings=(),
        decisions=tuple(decisions),
        equity_curve=tuple(equity),
        position_intervals=tuple(intervals),
        closed_trades=(),
        execution_event_summary={
            "execution_attempt_count": len(executions),
            "filled_execution_count": len(executions),
            "portfolio_applied_trade_count": len(trades),
        },
        resource_usage={
            "buy_and_hold_research_kernel": "research_only_v1",
            "research_behavior_hash": sha256_prefixed(decisions),
            "executed_portfolio_policy": policy.as_dict(),
            "executed_portfolio_policy_hash": policy.policy_hash(),
            "ledger_starting_cash_krw": starting_cash,
            "ledger_initial_position_qty": float(policy.initial_position_qty),
            "position_sizing_policy": policy.position_sizing.as_dict(),
            "exit_policy": "no_explicit_exit",
            "open_position_at_end": bool(qty > 0.0),
            "final_position_marked_to_market": True,
        },
        strategy_diagnostics={
            "strategy_diagnostics_namespace": "buy_and_hold_baseline",
            "strategy_specific_diagnostics": {
                "buy_and_hold_baseline": {
                    "evaluation_count": len(decisions),
                    "emitted_buy_intent_count": sum(
                        1 for event in events if event.entry_signal == "BUY"
                    ),
                    "exit_policy": "no_explicit_exit",
                    "open_position_at_end": bool(qty > 0.0),
                    "final_position_marked_to_market": True,
                }
            },
        },
    )
