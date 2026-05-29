from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .lifecycle_artifacts import StartupResult
from .operator_event_composer import OperatorEventComposer


@dataclass(frozen=True)
class StartupController:
    symbol: str
    startup_gate_evaluator: Callable[[], str | None]
    stale_initial_reconcile_clearer: Callable[[], bool]
    stale_live_execution_broker_clearer: Callable[..., bool]
    state_snapshot: Callable[[], object]
    latest_order_identifiers: Callable[[], tuple[str | None, str | None]]
    count_open_orders: Callable[[], int]
    position_summary: Callable[[], str]
    recommended_commands: Callable[..., list[str]]
    auto_recovery_allowed: Callable[..., bool]

    def evaluate_persisted_halt(self) -> StartupResult:
        self.stale_initial_reconcile_clearer()
        self.stale_live_execution_broker_clearer()
        state = self.state_snapshot()
        if not bool(getattr(state, "halt_new_orders_blocked", False)):
            return StartupResult(status="READY", reason_code=None, evidence={"persisted_halt": False})
        reason_code = str(getattr(state, "halt_reason_code", None) or "PERSISTED_HALT_STATE")
        reason = str(getattr(state, "last_disable_reason", None) or "persisted halt state requires explicit operator resume")
        latest_client_order_id, latest_exchange_order_id = self.latest_order_identifiers()
        event = OperatorEventComposer(self.symbol).trading_halted_event(
            reason_code=reason_code,
            reason=reason,
            unresolved=bool(getattr(state, "halt_state_unresolved", False)),
            operator_action_required=bool(getattr(state, "halt_operator_action_required", False)),
            latest_client_order_id=latest_client_order_id,
            latest_exchange_order_id=latest_exchange_order_id,
            open_order_count=self.count_open_orders(),
            position_summary=self.position_summary(),
            recommended_commands=self.recommended_commands(
                reason_code=reason_code,
                startup_gate=False,
                recovery_required=False,
                unresolved_count=int(getattr(state, "unresolved_open_order_count", 0) or 0),
            ),
            extra={"alert_kind": "startup_gate"},
        )
        return StartupResult(
            status="BLOCKED",
            reason_code=reason_code,
            startup_gate_reason=reason,
            operator_event=event,
            evidence={"persisted_halt": True},
        )

    def evaluate_startup_gate(self) -> StartupResult:
        startup_gate_reason = self.startup_gate_evaluator()
        if startup_gate_reason is None:
            return StartupResult(status="READY", evidence={"startup_gate": "clear"})
        state = self.state_snapshot()
        if self.auto_recovery_allowed(state=state, startup_gate_reason=startup_gate_reason):
            return StartupResult(
                status="DEGRADED_RECOVERY_CONTINUE",
                reason_code="STARTUP_SAFETY_GATE",
                startup_gate_reason=startup_gate_reason,
                evidence={"startup_gate": "auto_recovery_continue"},
            )
        latest_client_order_id, latest_exchange_order_id = self.latest_order_identifiers()
        commands = self.recommended_commands(
            reason_code="STARTUP_SAFETY_GATE",
            startup_gate=True,
            recovery_required=(int(getattr(state, "recovery_required_count", 0) or 0) > 0),
            unresolved_count=int(getattr(state, "unresolved_open_order_count", 0) or 0),
        )
        event = OperatorEventComposer(self.symbol).startup_gate_blocked_event(
            reason_code="STARTUP_BLOCKED",
            reason=startup_gate_reason,
            unresolved_order_count=int(getattr(state, "unresolved_open_order_count", 0) or 0),
            position_may_remain=bool(getattr(state, "halt_position_present", False)),
            latest_client_order_id=latest_client_order_id,
            latest_exchange_order_id=latest_exchange_order_id,
            open_order_count=self.count_open_orders(),
            position_summary=self.position_summary(),
            recommended_commands=commands,
        )
        return StartupResult(
            status="BLOCKED",
            reason_code="STARTUP_SAFETY_GATE",
            startup_gate_reason=startup_gate_reason,
            operator_event=event,
            evidence={"startup_gate": "blocked"},
        )


__all__ = ["StartupController"]
