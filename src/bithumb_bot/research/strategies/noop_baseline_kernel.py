"""Research-only no-execution kernel for the noop baseline."""

from __future__ import annotations

from typing import Any

from ..backtest_types import BacktestRun, BacktestRunContext
from ..dataset_snapshot import DatasetSnapshot
from ..execution_timing import candle_close_ts
from ..experiment_manifest import (
    ExecutionTimingPolicy,
    PortfolioPolicy,
    legacy_research_portfolio_policy,
)
from ..hashing import sha256_prefixed
from ..metrics import ResearchMetrics
from ..metrics_contract import EquityPoint, build_metrics_v2
from ..strategy_spec import NOOP_BASELINE_SPEC, materialize_strategy_parameters
from .noop_baseline_events import build_noop_baseline_events


def run_noop_baseline_backtest(
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
    """Mark an unchanged portfolio for each noop event without execution planning."""
    del execution_model, context
    params = materialize_strategy_parameters(
        "noop_baseline",
        parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )
    policy = portfolio_policy or legacy_research_portfolio_policy()
    timing = execution_timing_policy or ExecutionTimingPolicy()
    events = build_noop_baseline_events(
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
    first_price = float(dataset.candles[0].close) if dataset.candles else 0.0
    peak_equity = cash + qty * first_price
    max_drawdown = 0.0
    equity: list[EquityPoint] = []
    decisions: list[dict[str, object]] = []

    for event in events:
        candle = candles[int(event.candle_ts)]
        mark_price = float(candle.close)
        marked_equity = cash + qty * mark_price
        peak_equity = max(peak_equity, marked_equity)
        if peak_equity > 0.0:
            max_drawdown = max(
                max_drawdown,
                (peak_equity - marked_equity) / peak_equity * 100.0,
            )
        equity.append(
            EquityPoint(
                int(event.decision_ts),
                marked_equity,
                cash,
                qty,
            )
        )
        decisions.append(
            {
                "strategy_name": "noop_baseline",
                "candle_ts": int(event.candle_ts),
                "decision_ts": int(event.decision_ts),
                "raw_signal": "HOLD",
                "entry_signal": "HOLD",
                "exit_signal": "HOLD",
                "final_signal": "HOLD",
                "reason": event.reason,
                "feature_snapshot": dict(event.feature_snapshot),
                "strategy_diagnostics": dict(event.strategy_diagnostics),
                "strategy_decision_contract_version": (
                    NOOP_BASELINE_SPEC.decision_contract_version
                ),
                "strategy_diagnostics_namespace": "noop_baseline",
                "execution_intent": "none",
                "exit_policy": "no_entry_no_exit",
                "position_unchanged": True,
                "cash_unchanged": True,
            }
        )

    last_price = float(dataset.candles[-1].close) if dataset.candles else 0.0
    if events:
        equity.append(
            EquityPoint(
                candle_close_ts(dataset.candles[-1], interval=dataset.interval),
                cash + qty * last_price,
                cash,
                qty,
            )
        )
    metrics_v2 = build_metrics_v2(
        starting_cash=starting_cash,
        final_cash=cash,
        final_asset_qty=qty,
        final_mark_price=last_price,
        equity_curve=tuple(equity),
        position_intervals=(),
        closed_trades=(),
        execution_records=(),
        final_open_cost_basis=0.0,
        summary_max_drawdown_pct=max_drawdown,
        decision_records=tuple(decisions),
    )
    quality = metrics_v2.trade_quality
    strategy_behavior_material = [
        {
            "strategy_name": event.strategy_name,
            "strategy_version": event.strategy_version,
            "raw_signal": event.raw_signal,
            "final_signal": event.final_signal,
            "reason": event.reason,
            "feature_snapshot": dict(event.feature_snapshot),
            "strategy_diagnostics": dict(event.strategy_diagnostics),
        }
        for event in events
    ]
    common_behavior_material = [
        {
            "candle_ts": item["candle_ts"],
            "decision_ts": item["decision_ts"],
            "entry_signal": item["entry_signal"],
            "exit_signal": item["exit_signal"],
            "final_signal": item["final_signal"],
            "execution_intent": item["execution_intent"],
        }
        for item in decisions
    ]
    strategy_behavior_hash = sha256_prefixed(strategy_behavior_material)
    common_decision_behavior_hash = sha256_prefixed(common_behavior_material)
    behavior_hash = sha256_prefixed(
        {
            "strategy_behavior_hash": strategy_behavior_hash,
            "common_decision_behavior_hash": common_decision_behavior_hash,
        }
    )
    final_equity = cash + qty * last_price
    return BacktestRun(
        metrics=ResearchMetrics(
            return_pct=metrics_v2.return_risk.total_return_pct,
            max_drawdown_pct=max_drawdown,
            profit_factor=quality.profit_factor,
            profit_factor_unbounded=quality.profit_factor_unbounded,
            trade_count=quality.closed_trade_count,
            win_rate=quality.win_rate,
            avg_win=quality.avg_win,
            avg_loss=quality.avg_loss,
            fee_total=0.0,
            slippage_total=0.0,
            max_consecutive_losses=quality.max_consecutive_losses,
            single_trade_dependency_score=quality.single_trade_dependency_score,
            parameter_stability_score=parameter_stability_score,
        ),
        metrics_v2=metrics_v2,
        trades=(),
        candle_count=len(dataset.candles),
        warnings=(),
        decisions=tuple(decisions),
        equity_curve=tuple(equity),
        position_intervals=(),
        closed_trades=(),
        execution_event_summary={
            "execution_attempt_count": 0,
            "filled_execution_count": 0,
            "portfolio_applied_trade_count": 0,
        },
        resource_usage={
            "noop_baseline_research_kernel": "research_only_v1",
            "execution_intent": "none",
            "exit_policy": "no_entry_no_exit",
            "position_unchanged": True,
            "cash_unchanged": True,
            "research_behavior_hash": sha256_prefixed(decisions),
            "strategy_behavior_hash": strategy_behavior_hash,
            "common_decision_behavior_hash": common_decision_behavior_hash,
            "behavior_hash": behavior_hash,
            "composite_behavior_hash": behavior_hash,
            "decision_count": len(decisions),
            "equity_point_count": len(equity),
            "ledger_starting_cash_krw": starting_cash,
            "ledger_initial_position_qty": float(policy.initial_position_qty),
            "final_cash": cash,
            "final_asset_qty": qty,
            "final_marked_equity": final_equity,
            "executed_portfolio_policy": policy.as_dict(),
            "executed_portfolio_policy_hash": policy.policy_hash(),
        },
        strategy_diagnostics={
            "strategy_diagnostics_namespace": "noop_baseline",
            "strategy_specific_diagnostics": {
                "noop_baseline": {
                    "evaluation_count": len(decisions),
                    "hold_decision_count": len(decisions),
                    "raw_signal_count": 0,
                    "entry_signal_count": 0,
                    "exit_signal_count": 0,
                    "final_signal_count": 0,
                    "execution_intent": "none",
                    "exit_policy": "no_entry_no_exit",
                    "position_unchanged": True,
                    "cash_unchanged": True,
                }
            },
        },
    )
