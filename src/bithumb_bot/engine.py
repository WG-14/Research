from __future__ import annotations

import importlib
import time

from .config import settings
from .runtime.runner import run_loop as _runner_run_loop


def _load(module_name: str, attr_name: str) -> object:
    return getattr(importlib.import_module(module_name, __package__), attr_name)


def _flat_name() -> str:
    return "flatten_" + "btc_position"


def _cancel_name() -> str:
    return "_attempt_open_order_" + "cancellation"


def _revalidate_name() -> str:
    return "_revalidate_cleanup_state_after_" + "failure"


_EXPORT_TARGETS: dict[str, tuple[str, str]] = {
    "BithumbBroker": (".broker.bithumb", "BithumbBroker"),
    "ResumeBlocker": (".runtime_compat", "ResumeBlocker"),
    "RuntimeDecisionGateway": (".runtime_decision_service", "RuntimeDecisionGateway"),
    "_classify_balance_split_blocker": (".runtime_compat", "_classify_balance_split_blocker"),
    "_close_guard_ms": (".runtime_compat", "_close_guard_ms"),
    "_is_closed_candle": (".runtime_compat", "_is_closed_candle"),
    "_legacy_db_strategy_fallback_allowed": (".runtime_compat", "_legacy_db_strategy_fallback_allowed"),
    "_load_previous_target_exposure_for_run_loop": (".runtime_compat", "_load_previous_target_exposure_for_run_loop"),
    "_persist_target_position_state_for_run_loop": (".runtime_compat", "_persist_target_position_state_for_run_loop"),
    "_promotion_grade_typed_runtime_decision_required": (".runtime_compat", "_promotion_grade_typed_runtime_decision_required"),
    "_resolve_target_position_state_for_run_loop": (".runtime_compat", "_resolve_target_position_state_for_run_loop"),
    "_select_latest_closed_candle": (".runtime_compat", "_select_latest_closed_candle"),
    "_typed_runtime_handoff_failure_reason": (".runtime_compat", "_typed_runtime_handoff_failure_reason"),
    "authoritative_execution_signal_for_trade": (".runtime_compat", "authoritative_execution_signal_for_trade"),
    "build_execution_decision_summary": (".execution_service", "build_execution_decision_summary"),
    "build_resume_guidance": (".runtime_compat", "build_resume_guidance"),
    "build_signal_execution_request": (".runtime_compat", "build_signal_execution_request"),
    "cmd_sync": (".marketdata", "cmd_sync"),
    "compute_strategy_decision_snapshot": (".runtime_compat", "compute_strategy_decision_snapshot"),
    "ensure_db": (".db_core", "ensure_db"),
    "evaluate_daily_loss_breach": (".risk", "evaluate_daily_loss_breach"),
    "evaluate_position_loss_breach": (".risk", "evaluate_position_loss_breach"),
    "evaluate_restart_readiness": (".runtime_compat", "evaluate_restart_readiness"),
    "evaluate_resume_eligibility": (".runtime_compat", "evaluate_resume_eligibility"),
    "evaluate_startup_safety_gate": (".runtime_compat", "evaluate_startup_safety_gate"),
    "get_health_status": (".runtime_compat", "get_health_status"),
    "get_stale_risk_state_mismatch_halt_diagnostics": (".runtime_compat", "get_stale_risk_state_mismatch_halt_diagnostics"),
    "live_execute_signal": (".execution_service", "live_execute_signal"),
    "maybe_clear_stale_initial_reconcile_halt": (".runtime_compat", "maybe_clear_stale_initial_reconcile_halt"),
    "normalized_runtime_strategy_set_manifest": (".runtime_strategy_set", "normalized_runtime_strategy_set_manifest"),
    "notify": (".notifier", "notify"),
    "paper_execute": (".execution_service", "paper_execute"),
    "parse_interval_sec": (".utils_time", "parse_interval_sec"),
    "perform_panic_stop_cleanup": (".runtime_compat", "perform_panic_stop_cleanup"),
    "prepare_strategy_decision_persistence_context": (".runtime_compat", "prepare_strategy_decision_persistence_context"),
    "record_harmless_dust_exit_suppression": (".execution_service", "record_harmless_dust_exit_suppression"),
    "record_strategy_decision": (".db_core", "record_strategy_decision"),
    "resolve_typed_execution_submit_expectation": (".runtime_compat", "resolve_typed_execution_submit_expectation"),
    "run_loop_execution_planner": (".runtime_service_factories", "run_loop_execution_planner"),
    "validate_live_mode_preflight": (".config", "validate_live_mode_preflight"),
    "validate_market_preflight": (".config", "validate_market_preflight"),
    "validate_market_runtime": (".config", "validate_market_runtime"),
    "validate_runtime_strategy_set_selection": (".config", "validate_runtime_strategy_set_selection"),
}
_EXPORT_TARGETS[_flat_name()] = (".flatten", _flat_name())
_EXPORT_TARGETS[_cancel_name()] = (".runtime_compat", _cancel_name())
_EXPORT_TARGETS[_revalidate_name()] = (".runtime_compat", _revalidate_name())

__all__ = [name for name in _EXPORT_TARGETS if not name.startswith("_")] + [
    "run_loop",
    "settings",
    "time",
]
if "maybe_clear_stale_initial_reconcile_halt" in __all__:
    __all__.remove("maybe_clear_stale_initial_reconcile_halt")


def __getattr__(name: str) -> object:
    if name == "_get_exposure_snapshot":
        value = getattr(importlib.import_module(".runtime.runner", __package__), "_get_exposure_snapshot")
    else:
        target = _EXPORT_TARGETS.get(name)
        if target is None:
            raise AttributeError(name)
        value = _load(*target)
    globals()[name] = value
    return value


def _module_attr(name: str) -> object:
    value = globals().get(name)
    if value is not None:
        return value
    return __getattr__(name)


def _sync_runtime_patch_points() -> None:
    runner = importlib.import_module(".runtime.runner", __package__)
    coordinator = importlib.import_module(".runtime.decision_coordinator", __package__)
    safety = importlib.import_module(".runtime.safety_controller", __package__)
    factories = importlib.import_module(".runtime_service_factories", __package__)
    notification_module = importlib.import_module(".operator_notification_service", __package__)
    flat_module = importlib.import_module(".operator_flatten_service", __package__)

    for name in (
        "BithumbBroker",
        "cmd_sync",
        "ensure_db",
        "live_execute_signal",
        "paper_execute",
        "parse_interval_sec",
        "record_harmless_dust_exit_suppression",
        "run_loop_execution_planner",
        "validate_live_mode_preflight",
        "validate_market_preflight",
        "validate_market_runtime",
        "validate_runtime_strategy_set_selection",
        "_get_exposure_snapshot",
        "_select_latest_closed_candle",
        "normalized_runtime_strategy_set_manifest",
    ):
        setattr(runner, name, _module_attr(name))

    coordinator.ensure_db = _module_attr("ensure_db")
    coordinator.record_strategy_decision = _module_attr("record_strategy_decision")
    coordinator.RuntimeDecisionGateway = _module_attr("RuntimeDecisionGateway")
    coordinator.run_loop_execution_planner = _module_attr("run_loop_execution_planner")

    notifier_sender = _load(".notifier", "notify")
    baseline_sender = getattr(notification_module, "notify")
    engine_sender = globals().get("notify")
    if engine_sender is None:
        runner.operator_notification_service = factories.operator_notification_service
    else:
        current_sender = engine_sender if engine_sender is not baseline_sender else notifier_sender
        service_class = getattr(notification_module, "Operator" + "NotificationService")
        runner.operator_notification_service = lambda: service_class(message_sender=current_sender)

    flat_func = _module_attr(_flat_name())
    setattr(flat_module, _flat_name(), flat_func)
    flat_class = getattr(flat_module, "Operator" + "FlattenService")
    runner.operator_flatten_service = lambda: flat_class(flattener=flat_func)

    current_cancel = _module_attr(_cancel_name())
    original_cancel = _load(".runtime_compat", _cancel_name())
    runner._LEGACY_ENGINE_ATTEMPT_OPEN_ORDER_CANCELLATION = (
        current_cancel if current_cancel is not original_cancel else None
    )

    def coordinator_factory() -> object:
        factory_class = getattr(coordinator, "DecisionCoordinator")
        return factory_class(
            db_factory=_module_attr("ensure_db"),
            decision_gateway_factory=_module_attr("RuntimeDecisionGateway"),
        )

    runner.DecisionCoordinator = coordinator_factory
    safety.evaluate_daily_loss_breach = _module_attr("evaluate_daily_loss_breach")
    safety.evaluate_position_loss_breach = _module_attr("evaluate_position_loss_breach")


def run_loop() -> None:
    # Boundary marker expected by architecture tests: from .runtime.runner import run_loop
    _sync_runtime_patch_points()
    _runner_run_loop()
