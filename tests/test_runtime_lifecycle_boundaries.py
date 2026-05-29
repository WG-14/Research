from __future__ import annotations

import inspect
import ast
from dataclasses import dataclass

from bithumb_bot import engine
from bithumb_bot import runtime_state
from bithumb_bot.runtime import runner
from bithumb_bot.runtime.execution_coordinator import ExecutionCoordinator
from bithumb_bot.runtime.lifecycle_artifacts import RuntimeCycleArtifact
from bithumb_bot.runtime.notification_adapter import NotificationAdapter
from bithumb_bot.runtime.operator_event_composer import OperatorEventComposer
from bithumb_bot.runtime.recovery_controller import RecoveryController, ReconcileClearEvidence
from bithumb_bot.runtime.safety_controller import HaltReason, SafetyController
from bithumb_bot.runtime.startup_controller import StartupController
from bithumb_bot.runtime.state_store import RuntimeStateStore


@dataclass
class _State:
    halt_new_orders_blocked: bool = False
    halt_state_unresolved: bool = False
    halt_reason_code: str | None = None
    last_disable_reason: str | None = None
    halt_operator_action_required: bool = False
    halt_open_orders_present: bool = False
    halt_position_present: bool = False
    unresolved_open_order_count: int = 0
    recovery_required_count: int = 0
    last_reconcile_status: str | None = None
    last_reconcile_reason_code: str | None = None


class _Notifications:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.messages: list[str] = []

    def send_event(self, event_name: str, **fields: object) -> None:
        self.events.append((event_name, fields))

    def send_message(self, message: str) -> None:
        self.messages.append(message)


def test_engine_is_explicit_entrypoint_or_facade() -> None:
    source = inspect.getsource(engine)
    assert "sys.modules[__name__]" not in source
    assert hasattr(engine, "run_loop")


def test_engine_does_not_reexport_runtime_private_helpers() -> None:
    exported = set(getattr(engine, "__all__", ()))
    assert "_attempt_open_order_cancellation" not in exported
    assert "_revalidate_cleanup_state_after_failure" not in exported
    assert "maybe_clear_stale_initial_reconcile_halt" not in exported
    assert all(not name.startswith("_") for name in exported)


def test_engine_imports_only_runner_or_public_facade() -> None:
    source = inspect.getsource(engine)
    tree = ast.parse(source)
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    modules = {node.module for node in imports}
    assert modules <= {"__future__", "config", "runtime.runner"}
    for node in imports:
        if node.module == "runtime.runner":
            assert [alias.name for alias in node.names] == ["run_loop"]
        if node.module == "config":
            assert [alias.name for alias in node.names] == ["settings"]
    assert "from .runtime.runner import (" not in source
    assert "_attempt_open_order_cancellation" not in source
    assert "_revalidate_cleanup_state_after_failure" not in source


def test_runner_does_not_compose_operator_notifications() -> None:
    source = inspect.getsource(runner)
    assert "safety_event(" not in source
    assert "operator_next_action" not in source
    assert "operator_hint_command" not in source
    assert "operator_compact_summary" not in source
    assert "operator_recommended_commands" not in source
    assert "globals()[" not in source


def test_runner_only_coordinates_controllers() -> None:
    source = inspect.getsource(runner.run_loop)
    assert "prepare_runtime_start(" in source
    assert ".decide_cycle(" in source
    assert ".evaluate_and_apply_live_runtime(" in source
    assert ".execute_cycle(" in source
    assert ".evaluate_closed_candle(" in source
    assert ".apply(" in source
    for forbidden in {
        "RuntimeDecisionGateway().decide_bundle(",
        "record_strategy_decision(",
        "run_loop_execution_planner(",
        "execution_service.execute(",
        "BrokerError",
        "evaluate_daily_loss_breach(",
        "evaluate_position_loss_breach(",
    }:
        assert forbidden not in source
    tree = ast.parse(source)
    direct_composer_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "OperatorEventComposer"
    ]
    assert direct_composer_calls == []


def test_runner_does_not_own_startup_gate_branches() -> None:
    source = inspect.getsource(runner.run_loop)
    assert "prepare_runtime_start" in source
    assert "_startup_gate_allows_process_auto_recovery" not in source
    assert "startup_gate_blocked" not in source


def test_runner_does_not_shadow_controller_owned_reason_code_sets() -> None:
    source = inspect.getsource(runner)
    assert "SAFE_CLEARABLE_RECONCILE_HALT_REASON_CODES =" not in source
    assert "NON_CLEARING_RECONCILE_REASON_CODES =" not in source
    assert "class HaltReason" not in source


def test_runner_does_not_call_runtime_state_halt_mutators_directly() -> None:
    source = inspect.getsource(runner.run_loop)
    assert "runtime_state.enter_halt(" not in source
    assert "runtime_state.disable_trading_until(" not in source
    assert "runtime_state.mark_processed_candle(" not in source


def test_runner_does_not_directly_submit_orders_or_reconcile_post_trade() -> None:
    source = inspect.getsource(runner.run_loop)
    assert "execution_service.execute(" not in source
    assert "build_signal_execution_request(" not in source
    assert "except BrokerError" not in source
    assert "POST_TRADE_RECONCILE_FAILED" not in source
    assert "LIVE_EXECUTION_FAILED" not in source
    assert "LIVE_EXECUTION_BROKER_ERROR" not in source


def test_runner_does_not_own_safety_policy_branches() -> None:
    source = inspect.getsource(runner.run_loop)
    for forbidden in {
        "KILL_SWITCH",
        "KILL_SWITCH_LIQUIDATE",
        "DAILY_LOSS_LIMIT",
        "POSITION_LOSS_LIMIT",
        "MAX_OPEN_ORDER_AGE_SEC",
        "OPEN_ORDER_RECONCILE_MIN_INTERVAL_SEC",
        "stale unresolved open order",
        "emergency cancellation",
    }:
        assert forbidden not in source


def test_execution_coordinator_execute_cycle_is_used_by_runner() -> None:
    source = inspect.getsource(runner.run_loop)
    assert ".execute_cycle(" in source
    assert ".resolve_submit_expectation(" not in source
    assert ".target_delta_submit_expected(" not in source


def test_runtime_recovery_gate_does_not_call_side_effect_clearers_in_prepare_phase() -> None:
    from bithumb_bot.runtime_recovery_gate import RuntimeRecoveryGateService

    source = inspect.getsource(RuntimeRecoveryGateService.prepare_resume_gate)
    assert "clearer" not in source
    assert "evaluate_and_apply" not in source


def test_runtime_recovery_gate_compat_clearers_are_not_main_path_side_effects() -> None:
    from bithumb_bot.runtime_recovery_gate import RuntimeRecoveryGateService

    calls: list[str] = []
    service = RuntimeRecoveryGateService(
        startup_gate_evaluator=lambda: None,
        state_snapshot=lambda: _State(),
        stale_initial_reconcile_halt_clearer=lambda: calls.append("initial") or True,
        stale_live_execution_broker_halt_clearer=lambda **_kwargs: calls.append("broker") or True,
        stale_risk_state_mismatch_halt_clearer=lambda **_kwargs: calls.append("risk") or True,
    )

    preparation = service.prepare_resume_gate()

    assert calls == []
    assert preparation.initial_reconcile_halt_cleared is False
    assert preparation.live_execution_broker_halt_cleared is False
    assert preparation.risk_state_mismatch_halt_cleared is False


def test_startup_controller_does_not_call_side_effect_stale_clearers_in_evaluate_phase() -> None:
    source = inspect.getsource(StartupController.evaluate_persisted_halt)
    assert "stale_initial_reconcile_clearer" not in source
    assert "stale_live_execution_broker_clearer" not in source


def test_recovery_controller_evaluate_phase_has_no_state_apply() -> None:
    source = inspect.getsource(RecoveryController.evaluate_clearance)
    assert "enable_trading" not in source
    assert "disable_trading_until" not in source
    assert "set_resume_gate" not in source


def test_recovery_clearance_public_wrappers_are_side_effect_free() -> None:
    forbidden = {
        "refresh_open_order_health",
        "disable_trading_until",
        "enable_trading",
        "set_resume_gate",
        "evaluate_and_apply",
        "apply_clearance",
    }
    for func in (
        runner.evaluate_initial_reconcile_halt_clearance,
        runner.evaluate_live_execution_broker_halt_clearance,
        runner.evaluate_risk_state_mismatch_halt_clearance,
    ):
        source = inspect.getsource(func)
        tree = ast.parse(source)
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        called.update(
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        )
        assert forbidden.isdisjoint(called)


def test_runtime_state_disable_trading_until_does_not_query_exposure_sources() -> None:
    source = inspect.getsource(runtime_state.disable_trading_until)
    assert "ensure_db(" not in source
    assert "summarize_position_lots" not in source
    assert "build_position_state_model" not in source


def test_runtime_state_store_owns_state_application_boundary() -> None:
    source = inspect.getsource(RuntimeStateStore)
    assert "def snapshot" in source
    assert "def persist" in source
    assert "def apply_transition" in source
    assert "def pause_until" in source
    assert "def enable" in source
    assert "def set_resume_gate" in source
    assert "HaltStateProjector" in source


def test_runtime_cycle_artifact_is_recorded_for_halt_recovery_and_execution_failure() -> None:
    source = inspect.getsource(runner)
    assert '"recovery:initial_reconcile_clear"' in source
    assert '"recovery:live_execution_broker_clear"' in source
    assert '"recovery:risk_state_mismatch_clear"' in source
    assert "safety_decision_hash=decision.as_dict()" in source
    assert "state_transition_hash=decision.as_dict()" in source
    assert "notification_event_hashes=" in source
    assert "execution_result.planning_status" in source


def test_startup_controller_prepare_runtime_start_statuses() -> None:
    blocked_state = _State(halt_new_orders_blocked=True, halt_reason_code="HALTED")
    controller = StartupController(
        symbol="BTC_KRW",
        startup_gate_evaluator=lambda: None,
        stale_initial_reconcile_clearer=lambda: False,
        stale_live_execution_broker_clearer=lambda **_kwargs: False,
        state_snapshot=lambda: blocked_state,
        latest_order_identifiers=lambda: (None, None),
        count_open_orders=lambda: 0,
        position_summary=lambda: "flat",
        recommended_commands=lambda **_kwargs: ["resume"],
        auto_recovery_allowed=lambda **_kwargs: False,
    )
    assert controller.prepare_runtime_start(live_mode=False).status == "BLOCKED"

    state = _State()
    ready = StartupController(
        symbol="BTC_KRW",
        startup_gate_evaluator=lambda: None,
        stale_initial_reconcile_clearer=lambda: False,
        stale_live_execution_broker_clearer=lambda **_kwargs: False,
        state_snapshot=lambda: state,
        latest_order_identifiers=lambda: (None, None),
        count_open_orders=lambda: 0,
        position_summary=lambda: "flat",
        recommended_commands=lambda **_kwargs: ["resume"],
        auto_recovery_allowed=lambda **_kwargs: False,
    )
    assert ready.prepare_runtime_start(live_mode=False).status == "READY"

    degraded = StartupController(
        symbol="BTC_KRW",
        startup_gate_evaluator=lambda: "fee_pending_auto_recovering=1",
        stale_initial_reconcile_clearer=lambda: False,
        stale_live_execution_broker_clearer=lambda **_kwargs: False,
        state_snapshot=lambda: state,
        latest_order_identifiers=lambda: (None, None),
        count_open_orders=lambda: 0,
        position_summary=lambda: "flat",
        recommended_commands=lambda **_kwargs: ["resume"],
        auto_recovery_allowed=lambda **_kwargs: True,
    )
    assert degraded.prepare_runtime_start(live_mode=False).status == "DEGRADED_RECOVERY_CONTINUE"


def test_recovery_controller_evaluate_and_apply_split() -> None:
    mutations: list[str] = []
    state = _State(
        halt_new_orders_blocked=True,
        halt_state_unresolved=True,
        halt_reason_code="INITIAL_RECONCILE_FAILED",
        last_reconcile_status="ok",
        last_reconcile_reason_code="RECONCILE_OK",
    )
    controller = RecoveryController(
        state_snapshot=lambda: state,
        refresh_open_order_health=lambda: None,
        startup_gate_evaluator=lambda: None,
        reconcile_clear_evidence=lambda _state: ReconcileClearEvidence(False, False, 0, True),
        risk_state_clear_allowed=lambda **_kwargs: False,
        enable_trading=lambda: mutations.append("enable"),
        disable_trading_until=lambda *args, **kwargs: mutations.append("disable"),
        set_resume_gate=lambda **kwargs: mutations.append("resume_gate"),
    )

    clearance = controller.evaluate_clearance(
        snapshot=state,
        startup_gate_reason=None,
        clearance_type="initial_reconcile",
    )
    assert clearance.allowed is True
    assert mutations == []

    transition = controller.apply_clearance(clearance)
    assert transition.applied is True
    assert mutations == ["disable", "resume_gate"]


def test_safety_controller_decision_creation_is_separate_from_notification_send() -> None:
    notifications = _Notifications()
    mutations: list[str] = []
    state = _State()
    controller = SafetyController(
        symbol="BTC_KRW",
        state_snapshot=lambda: state,
        enter_halt=lambda **_kwargs: mutations.append("halt"),
        resume_evaluator=lambda: (False, []),
        latest_order_identifiers=lambda: (None, None),
        count_open_orders=lambda: 0,
        position_summary=lambda: "flat",
        cancel_open_orders_with_broker=lambda _broker: {},
        record_cancel_open_orders_result=lambda **_kwargs: None,
        flatten_position=lambda **_kwargs: {},
        record_flatten_position_result=lambda **_kwargs: None,
        exposure_snapshot=lambda _now_ms: (False, False),
        revalidate_cleanup_state_after_failure=lambda *_args, **_kwargs: (True, "ok"),
        now_ms=lambda: 1,
        live_dry_run=lambda: True,
    )

    decision = controller.evaluate_halt(HaltReason("TEST", "detail"), unresolved=True)
    assert decision.as_dict()["decision_hash"].startswith("sha256:")
    assert notifications.events == []
    assert mutations == []

    controller.apply(decision)
    assert notifications.events == []
    assert mutations == ["halt"]


def test_safety_controller_apply_does_not_send_notifications() -> None:
    source = inspect.getsource(SafetyController.apply)
    assert "send_event" not in source
    assert "send_message" not in source
    assert "NotificationAdapter" not in source
    assert "notification_sender" not in inspect.getsource(SafetyController)


def test_operator_event_composer_owns_operator_action_fields() -> None:
    source = inspect.getsource(SafetyController)
    assert "operator_next_action" not in source
    assert "operator_hint_command" not in source
    assert "operator_compact_summary" not in source
    assert "operator_recommended_commands" not in source

    event = OperatorEventComposer("BTC_KRW").trading_halted_event(
        reason_code="TEST",
        reason="detail",
        unresolved=True,
        operator_action_required=True,
        open_orders_present=True,
        position_present=False,
        recommended_commands=["uv run python bot.py recovery-report"],
    )
    assert event["event_type"] == "trading_halted"
    assert event["operator_next_action"]
    assert event["operator_hint_command"]
    assert event["operator_compact_summary"]
    assert event["operator_recommended_commands"]
    assert event["event_hash"].startswith("sha256:")


def test_runner_does_not_create_market_runtime_halt_reason_codes() -> None:
    source = inspect.getsource(runner.run_loop)
    assert "MARKET_RUNTIME_POLICY_INVALID" not in source
    assert "MARKET_RUNTIME_CONTRACT_FAILED" not in source


def test_operator_event_composer_has_no_runtime_state_side_effects() -> None:
    source = inspect.getsource(OperatorEventComposer)
    assert "runtime_state" not in source
    event = OperatorEventComposer("BTC_KRW").startup_gate_blocked_event(
        reason_code="STARTUP_BLOCKED",
        reason="blocked",
        unresolved_order_count=1,
        position_may_remain=True,
    )
    assert event["event_type"] == "startup_gate_blocked"
    assert event["event_hash"].startswith("sha256:")
    assert "operator_recommended_commands" in event


def test_notification_adapter_sends_only_already_composed_events() -> None:
    notifications = _Notifications()
    event = OperatorEventComposer("BTC_KRW").recovery_required_event(
        reason_code="TEST",
        reason="already composed",
    )
    NotificationAdapter(notifications).send_event(event)
    assert notifications.events == [("recovery_required", {"schema_version": 1, "alert_kind": "recovery_required", "symbol": "BTC_KRW", "reason_code": "TEST", "reason": "already composed"})]


def test_execution_coordinator_owns_submit_checkpoint_and_post_trade_reconcile() -> None:
    coordinator = ExecutionCoordinator("lot_native")
    assert coordinator.execute_cycle(
        candle_ts=1,
        decision_id=None,
        execution_decision_summary=object(),
    ).planning_status == "decision_persistence_failed"
    assert coordinator.execute_cycle(
        candle_ts=1,
        decision_id=1,
        execution_decision_summary=None,
    ).planning_status == "execution_summary_missing"

    class _Summary:
        submit_expected = True

    result = coordinator.execute_cycle(
        candle_ts=1,
        decision_id=1,
        execution_decision_summary=_Summary(),
        submit_invoker=lambda: None,
        post_trade_reconcile=lambda: None,
    )
    assert result.submitted is True
    assert result.post_trade_reconciled is True


def test_runtime_cycle_artifact_hashes_required_paths() -> None:
    artifact = RuntimeCycleArtifact(
        cycle_id="submit",
        candle_ts=1,
        startup_state="READY",
        readiness_hash="sha256:ready",
        strategy_decision_hash="sha256:strategy",
        execution_plan_bundle_hash="sha256:plan",
        safety_decision_hash="sha256:safety",
        recovery_decision_hash="sha256:recovery",
        state_transition_hash="sha256:state",
        notification_event_hashes=["sha256:event"],
    )
    payload = artifact.as_dict()
    assert payload["artifact_type"] == "runtime_cycle_artifact"
    assert payload["decision_hash"].startswith("sha256:")
