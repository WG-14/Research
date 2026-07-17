from __future__ import annotations

from dataclasses import replace
from typing import Any

from .backtest_types import BacktestRun, BacktestRunContext
from .dataset_snapshot import DatasetSnapshot
from .execution_model import ExecutionModel
from .experiment_manifest import (
    ExecutionTimingPolicy,
    PortfolioPolicy,
    legacy_research_portfolio_policy,
)


def run_plugin_backtest(
    *,
    plugin: Any,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
) -> BacktestRun:
    from .simulation_engine import run_common_simulation_backtest

    policy = portfolio_policy or legacy_research_portfolio_policy()
    return _with_portfolio_policy_evidence(
        run_common_simulation_backtest(
            plugin=plugin,
            dataset=dataset,
            parameter_values=parameter_values,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            parameter_stability_score=parameter_stability_score,
            execution_model=execution_model,
            execution_timing_policy=execution_timing_policy,
            portfolio_policy=policy,
            context=context,
        ),
        policy=policy,
    )


def _empty_plugin_backtest_result(
    *,
    plugin: Any,
    dataset: DatasetSnapshot,
    parameter_stability_score: float | None,
    portfolio_policy: PortfolioPolicy,
    context: BacktestRunContext | None,
) -> BacktestRun:
    from . import backtest_support as support

    run_context = context or BacktestRunContext(report_detail="full")
    starting_cash = float(portfolio_policy.starting_cash_krw)
    initial_qty = float(portfolio_policy.initial_position_qty)
    accumulator = support.BacktestAccumulator(
        context=run_context,
        total_candles=len(dataset.candles),
        diagnostics_namespace=str(plugin.diagnostics_namespace),
    )
    audit_trace_index = support.complete_audit_trace(run_context, status="completed")
    return BacktestRun(
        metrics=support.empty_metrics(parameter_stability_score),
        metrics_v2=support.empty_metrics_v2(
            starting_cash=starting_cash,
            initial_position_qty=initial_qty,
        ),
        trades=(),
        candle_count=len(dataset.candles),
        warnings=("not_enough_candles",),
        regime_performance=(),
        regime_coverage=(),
        execution_event_summary=support.empty_execution_event_summary(),
        decisions=(),
        equity_curve=(),
        resource_usage=accumulator.resource_usage(
            candles_processed=len(dataset.candles)
        ),
        strategy_diagnostics=accumulator.strategy_diagnostics(trades=[]),
        retained_detail_summary=support.retained_detail_summary(
            accumulator,
            retained_regime_snapshot_count=0,
        ),
        audit_trace_index=audit_trace_index,
    )


def _portfolio_policy_evidence(policy: PortfolioPolicy) -> dict[str, Any]:
    from . import backtest_support as support

    return support.portfolio_policy_evidence(policy)


def _with_portfolio_policy_evidence(
    run: BacktestRun, *, policy: PortfolioPolicy
) -> BacktestRun:
    resource_usage = dict(run.resource_usage or {})
    resource_usage.update(_portfolio_policy_evidence(policy))
    warnings = set(run.warnings)
    if run.decisions or run.trades or "not_enough_candles" not in warnings:
        warnings.update(policy.warning_codes())
    return replace(run, resource_usage=resource_usage, warnings=tuple(sorted(warnings)))
