from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from . import backtest_support as support
from .backtest_stages import (
    ExperimentRecorder,
    MarketReplayClock,
    MetricsCollector,
    PortfolioLedgerStage,
    RiskGate,
    StrategyEvaluator,
)

if TYPE_CHECKING:
    from .backtest_support import BacktestRun, BacktestRunContext
    from .dataset_snapshot import DatasetSnapshot
    from .decision_event import ResearchDecisionEvent
    from .execution_model import ExecutionModel
    from .experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy


BacktestRun = support.BacktestRun
BacktestRunContext = support.BacktestRunContext
empty_execution_event_summary = support.empty_execution_event_summary
execution_event_summary = support.execution_event_summary


class ExecutionSimulator(Protocol):
    def execute(self, *args: Any, **kwargs: Any) -> Any:
        ...


@dataclass(frozen=True)
class BacktestStageSet:
    market_clock: MarketReplayClock | None = None
    portfolio_ledger: PortfolioLedgerStage | None = None
    strategy_evaluator: StrategyEvaluator | None = None
    risk_gate: RiskGate | None = None
    execution_simulator: ExecutionSimulator | None = None
    metrics_collector: MetricsCollector | None = None
    experiment_recorder: ExperimentRecorder | None = None

    def ordered(self) -> tuple[object, ...]:
        return tuple(
            stage
            for stage in (
                self.market_clock,
                self.portfolio_ledger,
                self.strategy_evaluator,
                self.risk_gate,
                self.execution_simulator,
                self.metrics_collector,
                self.experiment_recorder,
            )
            if stage is not None
        )


@dataclass(frozen=True)
class DefaultBacktestPipeline:
    """Stage-composition boundary behind the public backtest kernel facade."""

    stages: BacktestStageSet = field(default_factory=BacktestStageSet)
    injected_stages: tuple[object, ...] = ()

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
        stages = self.injected_stages or self.stages.ordered()
        if stages:
            return self._run_injected_stages(
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
                stages=stages,
            )
        return _run_decision_event_backtest_impl(
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

    def _run_injected_stages(self, **payload: object) -> BacktestRun:
        stages = tuple(payload.pop("stages"))
        state: object = payload
        for stage in stages:
            runner = getattr(stage, "run", None)
            if runner is None:
                if not callable(stage):
                    raise TypeError(f"backtest_stage_not_callable:{type(stage).__name__}")
                state = stage(state)  # type: ignore[misc]
            else:
                state = runner(state)
        return state  # type: ignore[return-value]


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
    return _run_decision_event_backtest_impl(
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


def _run_decision_event_backtest_impl(
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
    """Execute strategy decision events through the shared research backtest kernel stages."""
    from . import backtest_loop as _default_loop

    # Compatibility for tests and downstream research tooling that patch the
    # historical backtest_pipeline symbols while the implementation lives behind
    # the stage runner.
    _default_loop._research_execution_plan_bundle = _research_execution_plan_bundle
    _default_loop.ResearchVirtualExecutionService = ResearchVirtualExecutionService

    # The default runner still owns plugin resolution and exit rule discovery
    # through the strategy plugin boundary:
    # resolve_research_strategy_plugin(strategy_name)
    # strategy plugin boundary: strategy_plugin.exit_rule_factory and common_rules.
    return _default_loop._run_decision_event_backtest_impl(
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
from .backtest_loop import (  # noqa: E402
    ResearchExecutionPlanBundle,
    _execution_plan_evidence,
    _research_execution_plan_bundle,
    _research_position_snapshot,
)
from .execution_simulator import (  # noqa: E402
    ResearchExecutionContext,
    ResearchVirtualExecutionService,
    execution_submit_plan_to_research_request,
)
