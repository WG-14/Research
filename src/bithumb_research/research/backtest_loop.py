"""Compatibility import surface for historical backtest-loop users.

The old loop body has been retired. This module intentionally delegates the
legacy implementation name to DefaultBacktestPipeline so callers cannot bypass
the stage-owned authority boundaries.
"""

from __future__ import annotations

from . import backtest_support as support
from .execution_planning import (
    ResearchExecutionPlanBundle,
    _execution_plan_evidence,
    _research_execution_plan_bundle,
    _research_position_snapshot,
)
from .compatibility_execution_planning import _research_execution_submit_plan
from .execution_simulator import (
    ResearchExecutionContext,
    ResearchVirtualExecutionService,
    execution_submit_plan_to_research_request,
)

BacktestRun = support.BacktestRun
BacktestRunContext = support.BacktestRunContext
empty_execution_event_summary = support.empty_execution_event_summary
execution_event_summary = support.execution_event_summary


def _run_decision_event_backtest_impl(**kwargs: object) -> BacktestRun:
    """Compatibility shim; canonical execution is owned by DefaultBacktestPipeline."""
    from .backtest_pipeline import DefaultBacktestPipeline

    return DefaultBacktestPipeline().run(**kwargs)  # type: ignore[arg-type]


__all__ = [
    "BacktestRun",
    "BacktestRunContext",
    "ResearchExecutionContext",
    "ResearchExecutionPlanBundle",
    "ResearchVirtualExecutionService",
    "_execution_plan_evidence",
    "_run_decision_event_backtest_impl",
    "_research_execution_plan_bundle",
    "_research_execution_submit_plan",
    "_research_position_snapshot",
    "empty_execution_event_summary",
    "execution_event_summary",
    "execution_submit_plan_to_research_request",
]
