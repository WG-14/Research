from __future__ import annotations

from .config import settings
from .execution_service import build_execution_decision_summary
from .run_loop_execution_planner import prepare_strategy_decision_persistence_context
from .runtime.execution_coordinator import (
    resolve_typed_execution_submit_expectation as _resolve_typed_execution_submit_expectation,
)
from .runtime.cleanup_revalidation import (
    revalidate_cleanup_state_after_failure_compat as _revalidate_cleanup_state_after_failure,
)
from .runtime.runner import (
    ResumeBlocker,
    _attempt_open_order_cancellation,
    _classify_balance_split_blocker,
    _close_guard_ms,
    _is_closed_candle,
    _legacy_db_strategy_fallback_allowed,
    _load_previous_target_exposure_for_run_loop,
    _persist_target_position_state_for_run_loop,
    _promotion_grade_typed_runtime_decision_required,
    _resolve_target_position_state_for_run_loop,
    _select_latest_closed_candle,
    _typed_runtime_handoff_failure_reason,
    authoritative_execution_signal_for_trade,
    build_resume_guidance,
    build_signal_execution_request,
    compute_strategy_decision_snapshot,
    evaluate_restart_readiness,
    get_stale_risk_state_mismatch_halt_diagnostics,
    maybe_clear_stale_initial_reconcile_halt,
)
from .runtime.public_api import (
    evaluate_resume_eligibility,
    evaluate_startup_safety_gate,
    get_health_status,
    perform_panic_stop_cleanup,
)


def resolve_typed_execution_submit_expectation(summary):
    return _resolve_typed_execution_submit_expectation(
        summary,
        execution_engine_name=str(getattr(settings, "EXECUTION_ENGINE", "lot_native") or "lot_native"),
    )


def run_loop() -> None:
    from .runtime.app_container import create_default_runtime_app

    create_default_runtime_app(settings).runner.run_forever()


__all__ = [
    "ResumeBlocker",
    "_attempt_open_order_cancellation",
    "_classify_balance_split_blocker",
    "_close_guard_ms",
    "_is_closed_candle",
    "_legacy_db_strategy_fallback_allowed",
    "_load_previous_target_exposure_for_run_loop",
    "_persist_target_position_state_for_run_loop",
    "_promotion_grade_typed_runtime_decision_required",
    "_resolve_target_position_state_for_run_loop",
    "_revalidate_cleanup_state_after_failure",
    "_select_latest_closed_candle",
    "_typed_runtime_handoff_failure_reason",
    "authoritative_execution_signal_for_trade",
    "build_resume_guidance",
    "build_signal_execution_request",
    "build_execution_decision_summary",
    "compute_strategy_decision_snapshot",
    "evaluate_restart_readiness",
    "evaluate_resume_eligibility",
    "evaluate_startup_safety_gate",
    "get_health_status",
    "get_stale_risk_state_mismatch_halt_diagnostics",
    "maybe_clear_stale_initial_reconcile_halt",
    "perform_panic_stop_cleanup",
    "run_loop",
    "resolve_typed_execution_submit_expectation",
    "prepare_strategy_decision_persistence_context",
]
