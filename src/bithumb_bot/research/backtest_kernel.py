from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .backtest_pipeline import DefaultBacktestPipeline
from .backtest_support import BacktestRun, BacktestRunContext
from .dataset_snapshot import DatasetSnapshot
from .decision_event import ResearchDecisionEvent
from .execution_model import ExecutionModel
from .experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy


@dataclass(frozen=True)
class BacktestKernel:
    """Stable public facade for decision-event backtests."""

    pipeline: DefaultBacktestPipeline = DefaultBacktestPipeline()

    def run(
        self,
        *,
        dataset: DatasetSnapshot,
        strategy_name: str,
        parameter_values: dict[str, Any],
        fee_rate: float,
        slippage_bps: float,
        decision_events: tuple[ResearchDecisionEvent, ...],
        parameter_stability_score: float | None = None,
        execution_model: ExecutionModel | None = None,
        execution_timing_policy: ExecutionTimingPolicy | None = None,
        portfolio_policy: PortfolioPolicy | None = None,
        context: BacktestRunContext | None = None,
    ) -> BacktestRun:
        return self.pipeline.run(
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
        )


def run_decision_event_backtest(
    *,
    dataset: DatasetSnapshot,
    strategy_name: str,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    decision_events: tuple[ResearchDecisionEvent, ...],
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
) -> BacktestRun:
    return BacktestKernel().run(
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
    )


# Compatibility re-exports for existing tests and downstream research tooling.
from .backtest_pipeline import (  # noqa: E402
    ResearchExecutionPlanBundle,
    _execution_plan_evidence,
    _research_execution_plan_bundle,
    _research_position_snapshot,
    _run_decision_event_backtest_impl,
)
from .execution_simulator import (  # noqa: E402
    ResearchExecutionContext,
    ResearchVirtualExecutionService,
    execution_submit_plan_to_research_request,
)
