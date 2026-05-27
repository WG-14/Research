from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .config import settings
from .run_loop_execution_planner import ExecutionPlanner, ExecutionPlanningResult


def _live_real_order_enabled() -> bool:
    return bool(
        str(settings.MODE).strip().lower() == "live"
        and bool(settings.LIVE_REAL_ORDER_ARMED)
        and not bool(settings.LIVE_DRY_RUN)
    )


def legacy_context_planning_allowed_for_compatibility(
    *,
    signal_handoff_fn: object,
    runtime_handoff_fn: object,
) -> bool:
    """Allow dict planning only for patched paper/smoke compatibility callers."""
    if _live_real_order_enabled():
        return False
    return signal_handoff_fn is not runtime_handoff_fn


@dataclass(frozen=True)
class RunLoopCompatibilityPlanner:
    """Non-production bridge for old dict signal handoff tests and smoke callers."""

    planner_factory: Callable[[], ExecutionPlanner]
    runtime_handoff_fn: object

    def plan_legacy_context(
        self,
        conn,
        *,
        decision_context: dict[str, object],
        signal: str,
        reason: str,
        updated_ts: int,
        signal_handoff_fn: object,
    ) -> ExecutionPlanningResult:
        return self.planner_factory().plan_strategy_decision(
            conn,
            decision_context=decision_context,
            signal=signal,
            reason=reason,
            updated_ts=updated_ts,
            allow_legacy_context_planning=legacy_context_planning_allowed_for_compatibility(
                signal_handoff_fn=signal_handoff_fn,
                runtime_handoff_fn=self.runtime_handoff_fn,
            ),
        )
