from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from ..observability import safety_event
from ..reason_codes import CANCEL_FAILURE
from .lifecycle_artifacts import SafetyDecision, StateTransitionResult
from .operator_event_composer import OperatorEventComposer


@dataclass(frozen=True)
class HaltReason:
    code: str
    detail: str


@dataclass(frozen=True)
class CleanupResult:
    halt_reason: HaltReason
    canceled_ok: bool
    unresolved: bool
    decision: SafetyDecision


@dataclass(frozen=True)
class SafetyController:
    symbol: str
    state_snapshot: Callable[[], object]
    enter_halt: Callable[..., None]
    resume_evaluator: Callable[[], tuple[bool, list[object]]]
    latest_order_identifiers: Callable[[], tuple[str | None, str | None]]
    count_open_orders: Callable[[], int]
    position_summary: Callable[[], str]
    notification_sender: object
    cancel_open_orders_with_broker: Callable[[object], Mapping[str, object]]
    record_cancel_open_orders_result: Callable[..., None]
    flatten_position: Callable[..., Mapping[str, object]]
    record_flatten_position_result: Callable[..., None]
    exposure_snapshot: Callable[[int], tuple[bool, bool]]
    revalidate_cleanup_state_after_failure: Callable[..., tuple[bool, str]]
    now_ms: Callable[[], int]
    live_dry_run: Callable[[], bool]

    def halt_trading(self, reason: HaltReason, *, unresolved: bool = False, attempt_flatten: bool = False) -> SafetyDecision:
        self.enter_halt(
            reason_code=reason.code,
            reason=reason.detail,
            unresolved=unresolved,
            attempt_flatten=attempt_flatten,
        )
        halt_state = self.state_snapshot()
        _resume_allowed, resume_blockers = self.resume_evaluator()
        latest_client_order_id, latest_exchange_order_id = self.latest_order_identifiers()
        operator_action_required = bool(getattr(halt_state, "halt_operator_action_required", False))
        open_order_count = self.count_open_orders()
        position_summary = self.position_summary()
        recommended_commands = recommended_operator_commands(
            reason_code=reason.code,
            startup_gate=False,
            recovery_required=False,
            unresolved_count=int(getattr(halt_state, "unresolved_open_order_count", 0) or 0),
        )
        primary_blocker_code = getattr(resume_blockers[0], "code", "-") if resume_blockers else "-"
        force_resume_allowed = bool(resume_blockers) and all(bool(getattr(b, "overridable", False)) for b in resume_blockers)
        blocker_summary = (
            f"total={len(resume_blockers)} "
            f"non_overridable={sum(1 for b in resume_blockers if not bool(getattr(b, 'overridable', False)))} "
            f"overridable={sum(1 for b in resume_blockers if bool(getattr(b, 'overridable', False)))}"
        )
        event = OperatorEventComposer(self.symbol).trading_halted_event(
            reason_code=reason.code,
            reason=reason.detail,
            unresolved=unresolved,
            operator_action_required=operator_action_required,
            latest_client_order_id=latest_client_order_id,
            latest_exchange_order_id=latest_exchange_order_id,
            open_order_count=open_order_count,
            position_summary=position_summary,
            recommended_commands=recommended_commands,
            extra={
                "unresolved_order_count": int(getattr(halt_state, "unresolved_open_order_count", 0) or 0),
                "position_may_remain": int(bool(getattr(halt_state, "halt_position_present", False))),
                "operator_next_action": format_operator_next_action(
                    reason_code=reason.code,
                    unresolved=unresolved,
                    operator_action_required=operator_action_required,
                    open_orders_present=bool(getattr(halt_state, "halt_open_orders_present", False)),
                    position_present=bool(getattr(halt_state, "halt_position_present", False)),
                ),
                "operator_hint_command": operator_hint_command(reason.code, force_resume_allowed=False),
                "primary_blocker_code": primary_blocker_code,
                "blocker_summary": blocker_summary,
                "force_resume_allowed": int(force_resume_allowed),
                "halt_policy_stage": getattr(halt_state, "halt_policy_stage", None),
                "block_new_orders": int(bool(getattr(halt_state, "halt_policy_block_new_orders", False))),
                "attempt_cancel_open_orders": int(bool(getattr(halt_state, "halt_policy_attempt_cancel_open_orders", False))),
                "auto_liquidate_positions": int(bool(getattr(halt_state, "halt_policy_auto_liquidate_positions", False))),
                "halt_position_present": int(bool(getattr(halt_state, "halt_position_present", False))),
                "halt_open_orders_present": int(bool(getattr(halt_state, "halt_open_orders_present", False))),
                "operator_compact_summary": operator_compact_summary(
                    halt_reason=reason.code,
                    unresolved_order_count=int(getattr(halt_state, "unresolved_open_order_count", 0) or 0),
                    open_order_count=open_order_count,
                    position_summary=position_summary,
                    recommended_commands=recommended_commands,
                ),
            },
        )
        self.notification_sender.send_event(event)
        transition = StateTransitionResult(
            status="applied",
            reason_code=reason.code,
            state_from="READY",
            state_to="HALTED",
            applied=True,
        )
        return SafetyDecision(
            action="HALT",
            reason_code=reason.code,
            reason=reason.detail,
            unresolved=unresolved,
            attempt_flatten=attempt_flatten,
            state_transition=transition,
            operator_event=event,
            evidence={"resume_blocker_count": len(resume_blockers)},
        )

    def attempt_open_order_cancellation(self, broker: object, trigger: str) -> bool:
        try:
            summary = self.cancel_open_orders_with_broker(broker)
        except Exception as exc:
            self.record_cancel_open_orders_result(
                trigger=trigger,
                status="error",
                summary={"error": f"{type(exc).__name__}: {exc}"},
            )
            self.notification_sender.send_message(
                safety_event(
                    "cancel_open_orders_failed",
                    alert_kind="cancel_failure",
                    trigger=trigger,
                    reason_code=CANCEL_FAILURE,
                    cancel_detail_code="CANCEL_OPEN_ORDERS_ERROR",
                    error_type=type(exc).__name__,
                    reason=str(exc),
                )
            )
            return False

        remote_open_count = int(summary["remote_open_count"])
        canceled_count = int(summary["canceled_count"])
        failed_count = int(summary["failed_count"])
        status = "partial" if failed_count > 0 else "ok"
        event = OperatorEventComposer(self.symbol).cancel_open_orders_result_event(
            trigger=trigger,
            remote_open_count=remote_open_count,
            canceled_count=canceled_count,
            failed_count=failed_count,
            status=status,
        )
        self.notification_sender.send_event(event)
        for message in summary.get("stray_messages", []):
            self.notification_sender.send_message(str(message))
        for message in summary.get("error_messages", []):
            self.notification_sender.send_message(str(message))
        self.record_cancel_open_orders_result(trigger=trigger, status=status, summary=summary)
        if failed_count > 0:
            self.notification_sender.send_message(
                safety_event(
                    "cancel_open_orders_failed",
                    alert_kind="cancel_failure",
                    trigger=trigger,
                    reason_code=CANCEL_FAILURE,
                    cancel_detail_code="CANCEL_OPEN_ORDERS_INCOMPLETE",
                    failed_count=failed_count,
                )
            )
            return False
        return True

    def attempt_cleanup_with_optional_flatten(
        self,
        broker: object,
        *,
        reason_code: str,
        reason_detail: str,
        cancel_trigger: str,
        flatten_trigger: str,
        attempt_flatten: bool,
    ) -> CleanupResult:
        initial_open_orders_present, initial_position_present = self.exposure_snapshot(self.now_ms())
        canceled_ok = self.attempt_open_order_cancellation(broker, trigger=cancel_trigger)
        flatten_outcome: Mapping[str, object] | None = None
        if attempt_flatten and canceled_ok:
            flatten_outcome = self.flatten_position(
                broker=broker,
                dry_run=self.live_dry_run(),
                trigger=flatten_trigger,
            )
            flatten_status = str(flatten_outcome.get("status") or "-")
        elif attempt_flatten:
            flatten_status = "skipped_cancel_failed"
        else:
            flatten_status = "skipped"

        if flatten_status in {"skipped", "skipped_cancel_failed"}:
            self.record_flatten_position_result(
                status=flatten_status,
                summary={
                    "status": flatten_status,
                    "attempted": int(bool(attempt_flatten)),
                    "cancel_ok": int(bool(canceled_ok)),
                    "reason_code": reason_code,
                    "reason_detail": reason_detail,
                    "trigger": flatten_trigger,
                },
            )

        detail_parts = [
            reason_detail,
            "emergency cancellation attempted" if canceled_ok else "emergency cancellation failed",
            f"flatten_status={flatten_status}",
        ]
        flatten_failed = flatten_status == "failed"
        if flatten_failed and flatten_outcome is not None:
            detail_parts.append(f"flatten_error={str(flatten_outcome.get('error') or '-')}")

        cleanup_uncertain = (not canceled_ok) or flatten_failed
        if cleanup_uncertain:
            revalidated_safe, revalidation_detail = self.revalidate_cleanup_state_after_failure(
                broker,
                trigger=flatten_trigger,
            )
            detail_parts.append(revalidation_detail)
            unresolved = not revalidated_safe
        else:
            post_open_orders_present, post_position_present = self.exposure_snapshot(self.now_ms())
            if post_open_orders_present or post_position_present:
                detail_parts.append(
                    "risk_open_exposure_remains("
                    f"open_orders={1 if post_open_orders_present else 0},"
                    f"position={1 if post_position_present else 0})"
                )
            unresolved = post_open_orders_present or post_position_present

        if initial_open_orders_present or initial_position_present:
            detail_parts.append(
                "cleanup_started_with_exposure("
                f"open_orders={1 if initial_open_orders_present else 0},"
                f"position={1 if initial_position_present else 0})"
            )
        halt_reason = HaltReason(reason_code, "; ".join(detail_parts))
        decision = SafetyDecision(
            action="HALT",
            reason_code=reason_code,
            reason=halt_reason.detail,
            unresolved=unresolved,
            attempt_flatten=attempt_flatten,
            evidence={
                "canceled_ok": bool(canceled_ok),
                "flatten_status": flatten_status,
                "cleanup_uncertain": bool(cleanup_uncertain),
            },
        )
        return CleanupResult(
            halt_reason=halt_reason,
            canceled_ok=canceled_ok,
            unresolved=unresolved,
            decision=decision,
        )


def format_operator_next_action(
    *,
    reason_code: str,
    unresolved: bool,
    operator_action_required: bool,
    open_orders_present: bool,
    position_present: bool,
) -> str:
    from ..reason_codes import POSITION_LOSS_LIMIT
    from ..risk import RISK_STATE_MISMATCH

    if reason_code in {"DAILY_LOSS_LIMIT", POSITION_LOSS_LIMIT}:
        return "review risk breach details, verify exposure, then run recovery-report"
    if reason_code == RISK_STATE_MISMATCH:
        return "review risk-report, verify reconcile and portfolio state, then run recovery-report"
    if "RECONCILE" in reason_code:
        return "run reconcile, validate order state, then run recovery-report before resume"
    if operator_action_required or unresolved:
        if open_orders_present or position_present:
            return "operator must review open exposure and reconcile before resume"
        return "operator must review halt reason and run safe resume checks"
    return "no immediate operator action required"


def operator_hint_command(reason_code: str, *, force_resume_allowed: bool = False) -> str:
    from ..risk import RISK_STATE_MISMATCH

    if force_resume_allowed:
        return "uv run python bot.py resume --force"
    if reason_code == RISK_STATE_MISMATCH:
        return "uv run bithumb-bot risk-report && uv run python bot.py recovery-report"
    if "RECONCILE" in reason_code:
        return "uv run python bot.py reconcile && uv run python bot.py recovery-report"
    return "uv run python bot.py recovery-report"


def recommended_operator_commands(
    *,
    reason_code: str,
    startup_gate: bool,
    recovery_required: bool,
    unresolved_count: int,
) -> list[str]:
    if startup_gate:
        return ["uv run python bot.py reconcile", "uv run python bot.py recovery-report"]
    if recovery_required:
        return ["uv run python bot.py recover-order --client-order-id <id>", "uv run python bot.py recovery-report"]
    if reason_code == "KILL_SWITCH":
        return ["uv run python bot.py recovery-report", "uv run python bot.py resume"]
    if unresolved_count > 0:
        return ["uv run python bot.py recovery-report"]
    return ["uv run python bot.py resume"]


def operator_compact_summary(
    *,
    halt_reason: str,
    unresolved_order_count: int,
    open_order_count: int,
    position_summary: str,
    recommended_commands: list[str],
) -> str:
    return (
        f"halt_reason={halt_reason} "
        f"unresolved_order_count={unresolved_order_count} "
        f"open_order_count={open_order_count} "
        f"position={position_summary} "
        f"next={' | '.join(recommended_commands)}"
    )


__all__ = [
    "CleanupResult",
    "HaltReason",
    "SafetyController",
    "format_operator_next_action",
    "operator_compact_summary",
    "operator_hint_command",
    "recommended_operator_commands",
]
