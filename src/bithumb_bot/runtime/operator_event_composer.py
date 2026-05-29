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
        latest_client_order_id: str | None = None,
        latest_exchange_order_id: str | None = None,
        open_order_count: int = 0,
        position_summary: str = "-",
        recommended_commands: Sequence[str] = (),
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
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
            **dict(extra or {}),
        )

    def startup_gate_blocked_event(
        self,
        *,
        reason_code: str,
        reason: str,
        unresolved_order_count: int,
        position_may_remain: bool,
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
            unresolved_order_count=int(unresolved_order_count),
            position_may_remain=int(bool(position_may_remain)),
            latest_client_order_id=latest_client_order_id,
            latest_exchange_order_id=latest_exchange_order_id,
            operator_action_required=1,
            operator_next_action="operator must reconcile unresolved orders before startup",
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


__all__ = ["OperatorEventComposer"]
