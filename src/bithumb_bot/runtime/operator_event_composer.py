from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..decision_equivalence import sha256_prefixed


def _event(event_type: str, **fields: Any) -> dict[str, Any]:
    payload = {
        "event_type": event_type,
        "schema_version": 1,
        **fields,
    }
    payload["event_hash"] = sha256_prefixed(payload)
    return payload


@dataclass(frozen=True)
class OperatorEventComposer:
    symbol: str

    def trading_halted_event(
        self,
        *,
        reason_code: str,
        reason: str,
        unresolved: bool,
        operator_action_required: bool,
        force_resume_allowed: bool = False,
        open_orders_present: bool = False,
        position_present: bool = False,
        unresolved_order_count: int = 0,
        primary_blocker_code: str = "-",
        blocker_summary: str = "total=0 non_overridable=0 overridable=0",
        halt_policy_stage: str | None = None,
        block_new_orders: bool = False,
        attempt_cancel_open_orders: bool = False,
        auto_liquidate_positions: bool = False,
        latest_client_order_id: str | None = None,
        latest_exchange_order_id: str | None = None,
        open_order_count: int = 0,
        position_summary: str = "-",
        recommended_commands: Sequence[str] = (),
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        extra_fields = dict(extra or {})
        extra_fields.pop("alert_kind", None)
        return _event(
            "trading_halted",
            status="HALTED",
            severity="CRITICAL",
            alert_kind="halt",
            symbol=self.symbol,
            reason_code=reason_code,
            reason=reason,
            unresolved=int(bool(unresolved)),
            operator_action_required=int(bool(operator_action_required)),
            latest_client_order_id=latest_client_order_id,
            latest_exchange_order_id=latest_exchange_order_id,
            open_order_count=int(open_order_count),
            position_summary=position_summary,
            operator_recommended_commands=" | ".join(recommended_commands),
            unresolved_order_count=int(unresolved_order_count),
            position_may_remain=int(bool(position_present)),
            operator_next_action=format_operator_next_action(
                reason_code=reason_code,
                unresolved=unresolved,
                operator_action_required=operator_action_required,
                open_orders_present=open_orders_present,
                position_present=position_present,
            ),
            operator_hint_command=operator_hint_command(reason_code, force_resume_allowed=force_resume_allowed),
            primary_blocker_code=primary_blocker_code,
            blocker_summary=blocker_summary,
            force_resume_allowed=int(force_resume_allowed),
            halt_policy_stage=halt_policy_stage,
            block_new_orders=int(bool(block_new_orders)),
            attempt_cancel_open_orders=int(bool(attempt_cancel_open_orders)),
            auto_liquidate_positions=int(bool(auto_liquidate_positions)),
            halt_position_present=int(bool(position_present)),
            halt_open_orders_present=int(bool(open_orders_present)),
            operator_compact_summary=operator_compact_summary(
                halt_reason=reason_code,
                unresolved_order_count=int(unresolved_order_count),
                open_order_count=open_order_count,
                position_summary=position_summary,
                recommended_commands=recommended_commands,
            ),
            **extra_fields,
        )

    def startup_gate_blocked_event(
        self,
        *,
        reason_code: str,
        reason: str,
        unresolved_order_count: int,
        position_may_remain: bool,
        timestamp: int | None = None,
        latest_client_order_id: str | None = None,
        latest_exchange_order_id: str | None = None,
        open_order_count: int = 0,
        position_summary: str = "-",
        recommended_commands: Sequence[str] = (),
        state_to: str = "HALTED",
    ) -> dict[str, Any]:
        return _event(
            "startup_gate_blocked",
            alert_kind="startup_gate",
            symbol=self.symbol,
            reason_code=reason_code,
            reason=reason,
            timestamp=timestamp,
            unresolved_order_count=int(unresolved_order_count),
            position_may_remain=int(bool(position_may_remain)),
            latest_client_order_id=latest_client_order_id,
            latest_exchange_order_id=latest_exchange_order_id,
            operator_action_required=1,
            operator_next_action="operator must reconcile unresolved orders before startup",
            operator_compact_summary="halt_reason=STARTUP_SAFETY_GATE",
            legacy_reason_code_text="reason_code=STARTUP_SAFETY_GATE",
            open_order_count=int(open_order_count),
            position_summary=position_summary,
            operator_recommended_commands=" | ".join(recommended_commands),
            state_to=state_to,
        )

    def recovery_required_event(self, *, reason_code: str, reason: str, **fields: Any) -> dict[str, Any]:
        return _event(
            "recovery_required",
            alert_kind="recovery_required",
            symbol=self.symbol,
            reason_code=reason_code,
            reason=reason,
            **fields,
        )

    def stale_open_order_recovery_required_event(
        self,
        *,
        reason: str,
        marked_count: int,
        latest_client_order_id: str | None,
        latest_exchange_order_id: str | None,
        open_order_count: int,
        unresolved_order_count: int,
        position_summary: str,
    ) -> dict[str, Any]:
        commands = ["uv run python bot.py reconcile", "uv run python bot.py recover-order --client-order-id <id>"]
        return _event(
            "recovery_required_marked",
            alert_kind="recovery_required",
            symbol=self.symbol,
            reason_code="STALE_OPEN_ORDER",
            marked_count=int(marked_count),
            latest_client_order_id=latest_client_order_id,
            latest_exchange_order_id=latest_exchange_order_id,
            reason=reason,
            operator_next_action="inspect stale order(s), run reconcile, then recovery-report",
            operator_hint_command="uv run python bot.py reconcile && uv run python bot.py recovery-report",
            open_order_count=int(open_order_count),
            position_summary=position_summary,
            operator_recommended_commands=" | ".join(commands),
            operator_compact_summary=operator_compact_summary(
                halt_reason="STALE_OPEN_ORDER",
                unresolved_order_count=int(unresolved_order_count),
                open_order_count=int(open_order_count),
                position_summary=position_summary,
                recommended_commands=commands,
            ),
        )

    def cancel_open_orders_result_event(
        self,
        *,
        trigger: str,
        remote_open_count: int,
        canceled_count: int,
        failed_count: int,
        status: str,
    ) -> dict[str, Any]:
        return _event(
            "cancel_open_orders_result",
            symbol=self.symbol,
            trigger=trigger,
            remote_open_count=int(remote_open_count),
            canceled_count=int(canceled_count),
            failed_count=int(failed_count),
            status=status,
        )

    def panic_cleanup_event(self, *, reason_code: str, status: str, **fields: Any) -> dict[str, Any]:
        return _event(
            "panic_cleanup",
            alert_kind="cleanup",
            symbol=self.symbol,
            reason_code=reason_code,
            status=status,
            **fields,
        )

    def failsafe_retry_window_reached_event(self) -> dict[str, Any]:
        return _event(
            "failsafe_retry_window_reached",
            alert_kind="runtime_resume",
            symbol=self.symbol,
            reason_code="FAILSAFE_RETRY_WINDOW_REACHED",
            reason="failsafe retry window reached; attempting auto-resume",
        )

    def no_candles_after_sync_event(self) -> dict[str, Any]:
        return _event(
            "no_candles_after_sync",
            alert_kind="market_data",
            symbol=self.symbol,
            reason_code="NO_CANDLES_AFTER_SYNC",
            reason="sync completed but latest candle row was not found",
        )

    def sync_failed_event(self, *, fail_count: int, max_fails: int, error: str) -> dict[str, Any]:
        return _event(
            "sync_failed",
            alert_kind="market_data",
            symbol=self.symbol,
            reason_code="SYNC_FAILED",
            fail_count=int(fail_count),
            max_fails=int(max_fails),
            reason=error,
        )

    def failsafe_pause_enabled_event(self, *, retry_at_epoch_sec: float) -> dict[str, Any]:
        return _event(
            "failsafe_pause_enabled",
            alert_kind="runtime_pause",
            symbol=self.symbol,
            reason_code="FAILSAFE_PAUSE_ENABLED",
            retry_at_epoch_sec=int(retry_at_epoch_sec),
            reason="failsafe enabled after consecutive sync failures",
        )

    def stale_candle_detected_event(self, *, age_sec: float, stale_cutoff_sec: float) -> dict[str, Any]:
        return _event(
            "stale_candle_detected",
            alert_kind="market_data",
            symbol=self.symbol,
            reason_code="STALE_CANDLE_DETECTED",
            age_sec=float(age_sec),
            stale_cutoff_sec=float(stale_cutoff_sec),
            reason="stale candle detected; order blocked",
        )

    def open_order_blocked_event(self, *, reason_code: str, reason: str) -> dict[str, Any]:
        return self.recovery_required_event(
            reason_code=reason_code,
            reason=reason,
            event_name="order_submit_blocked",
        )

    def execution_failed_event(self, *, reason_code: str, reason: str, **fields: Any) -> dict[str, Any]:
        return _event(
            "execution_failed",
            alert_kind="execution",
            symbol=self.symbol,
            reason_code=reason_code,
            reason=reason,
            **fields,
        )

    def post_trade_reconcile_failed_event(self, *, reason: str, **fields: Any) -> dict[str, Any]:
        return _event(
            "post_trade_reconcile_failed",
            alert_kind="reconcile",
            symbol=self.symbol,
            reason_code="POST_TRADE_RECONCILE_FAILED",
            reason=reason,
            **fields,
        )


@dataclass(frozen=True)
class RuntimeOperatorEventComposer:
    symbol: str

    @property
    def composer(self) -> OperatorEventComposer:
        return OperatorEventComposer(self.symbol)

    def event(self, name: str, **fields: Any) -> dict[str, Any]:
        method = getattr(self.composer, f"{name}_event")
        return method(**fields)

    def execution_failure_from_transition(self, transition: Mapping[str, Any]) -> dict[str, Any]:
        reason_code = str(transition.get("reason_code") or "EXECUTION_FAILED")
        evidence = transition.get("evidence")
        reason = (
            str(evidence.get("error"))
            if isinstance(evidence, Mapping) and evidence.get("error") is not None
            else reason_code
        )
        if reason_code == "POST_TRADE_RECONCILE_FAILED":
            return self.composer.post_trade_reconcile_failed_event(reason=reason)
        return self.composer.execution_failed_event(reason_code=reason_code, reason=reason)


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
    recommended_commands: Sequence[str],
) -> str:
    return (
        f"halt_reason={halt_reason} "
        f"unresolved_order_count={unresolved_order_count} "
        f"open_order_count={open_order_count} "
        f"position={position_summary} "
        f"next={' | '.join(recommended_commands)}"
    )


__all__ = [
    "OperatorEventComposer",
    "RuntimeOperatorEventComposer",
    "format_operator_next_action",
    "operator_compact_summary",
    "operator_hint_command",
    "recommended_operator_commands",
]
