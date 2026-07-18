"""Compatibility import surface for historical research backtest users.

Decision-event backtests delegate through BacktestKernel and DefaultBacktestPipeline.
This module keeps old import paths stable without owning common strategy, risk,
execution, or ledger authority.
"""

from __future__ import annotations

from typing import Any, Iterable

from .backtest_common import (
    _behavior_hashes as _behavior_hashes,
    _trade_hash_payload as _trade_hash_payload,
    closed_trade_diagnostics as closed_trade_diagnostics,
    complete_audit_trace as complete_audit_trace,
    depth_request_fields as depth_request_fields,
    empty_execution_event_summary as empty_execution_event_summary,
    empty_metrics as empty_metrics,
    empty_metrics_v2 as empty_metrics_v2,
    execution_event_summary as execution_event_summary,
    execution_reference_warnings as execution_reference_warnings,
    failed_fill as failed_fill,
    fill_applies_to_mark as fill_applies_to_mark,
    fill_effective_ts as fill_effective_ts,
    mark_pending_fills_at_end as mark_pending_fills_at_end,
    metrics as metrics,
    metrics_v2_ledgers_from_trades as metrics_v2_ledgers_from_trades,
    model_latency_ms as model_latency_ms,
    pending_trade_from_fill as pending_trade_from_fill,
    record_equity_mark as record_equity_mark,
    research_decision_payload as research_decision_payload,
    retained_detail_summary as retained_detail_summary,
    timing_request_fields as timing_request_fields,
    trace_decision as trace_decision,
    trace_equity_mark as trace_equity_mark,
    trace_execution as trace_execution,
    trade_from_fill as trade_from_fill,
    trade_hash_payload as trade_hash_payload,
)
from .backtest_types import (
    BacktestHeartbeatPolicy as BacktestHeartbeatPolicy,
    BacktestResourceLimitExceeded as BacktestResourceLimitExceeded,
    BacktestResourceLimits as BacktestResourceLimits,
    BacktestRun,
    BacktestRunContext,
    MemorySample as MemorySample,
)
from .dataset_snapshot import DatasetSnapshot
from .decision_event import ResearchDecisionEvent
from .execution_model import ExecutionModel
from .experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy
from .strategy_registry import StrategyRegistry


def run_registered_strategy_backtest(
    strategy_name: str,
    *,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
    strategy_registry: StrategyRegistry,
) -> BacktestRun:
    from .strategy_catalog import resolve_research_strategy

    from .simulation_engine import run_common_simulation_backtest

    return run_common_simulation_backtest(
        plugin=resolve_research_strategy(strategy_name, registry=strategy_registry),
        registry=strategy_registry,
        dataset=dataset,
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        parameter_stability_score=parameter_stability_score,
        execution_model=execution_model,
        execution_timing_policy=execution_timing_policy,
        portfolio_policy=portfolio_policy,
        context=context,
    )


def run_sma_backtest(
    *,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
    strategy_registry: StrategyRegistry,
) -> BacktestRun:
    return run_registered_strategy_backtest(
        "sma_with_filter",
        dataset=dataset,
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        parameter_stability_score=parameter_stability_score,
        execution_model=execution_model,
        execution_timing_policy=execution_timing_policy,
        portfolio_policy=portfolio_policy,
        context=context,
        strategy_registry=strategy_registry,
    )


def run_sma_backtest_via_kernel(**kwargs: Any) -> BacktestRun:
    return run_sma_backtest(**kwargs)


def run_noop_baseline_backtest(
    *,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
    strategy_registry: StrategyRegistry,
) -> BacktestRun:
    # Compatibility path ultimately reaches run_decision_event_backtest.
    return run_registered_strategy_backtest(
        "noop_baseline",
        dataset=dataset,
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        parameter_stability_score=parameter_stability_score,
        execution_model=execution_model,
        execution_timing_policy=execution_timing_policy,
        portfolio_policy=portfolio_policy,
        context=context,
        strategy_registry=strategy_registry,
    )


def run_buy_and_hold_baseline_backtest(
    *,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
    strategy_registry: StrategyRegistry,
) -> BacktestRun:
    return run_registered_strategy_backtest(
        "buy_and_hold_baseline",
        dataset=dataset,
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        parameter_stability_score=parameter_stability_score,
        execution_model=execution_model,
        execution_timing_policy=execution_timing_policy,
        portfolio_policy=portfolio_policy,
        context=context,
        strategy_registry=strategy_registry,
    )


def run_decision_event_backtest(
    *,
    dataset: DatasetSnapshot,
    strategy_name: str,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    decision_events: Iterable[ResearchDecisionEvent],
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
    strategy_registry: StrategyRegistry,
) -> BacktestRun:
    """Compatibility wrapper for the common backtest kernel boundary."""
    from .backtest_kernel import (
        run_decision_event_backtest as _run_decision_event_backtest,
    )

    return _run_decision_event_backtest(
        dataset=dataset,
        strategy_name=strategy_name,
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        decision_events=decision_events,
        parameter_stability_score=parameter_stability_score,
        execution_model=execution_model,
        execution_timing_policy=execution_timing_policy,
        portfolio_policy=portfolio_policy,
        context=context,
        strategy_registry=strategy_registry,
    )


def _run_decision_event_backtest_impl(
    *,
    dataset: DatasetSnapshot,
    strategy_name: str,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    decision_events: Iterable[ResearchDecisionEvent],
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
    strategy_registry: StrategyRegistry,
) -> BacktestRun:
    """Compatibility implementation name; delegates to BacktestKernel."""
    from .backtest_kernel import (
        run_decision_event_backtest as _run_decision_event_backtest,
    )

    return _run_decision_event_backtest(
        dataset=dataset,
        strategy_name=strategy_name,
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        decision_events=decision_events,
        parameter_stability_score=parameter_stability_score,
        execution_model=execution_model,
        execution_timing_policy=execution_timing_policy,
        portfolio_policy=portfolio_policy,
        context=context,
        strategy_registry=strategy_registry,
    )
