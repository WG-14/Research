from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..risk import RISK_STATE_MISMATCH
from .lifecycle_artifacts import RecoveryClearance, StateTransitionResult

SAFE_CLEARABLE_RECONCILE_HALT_REASON_CODES = frozenset(
    {
        "INITIAL_RECONCILE_FAILED",
        "PERIODIC_RECONCILE_FAILED",
        "LIVE_EXECUTION_BROKER_ERROR",
        "POST_TRADE_RECONCILE_FAILED",
    }
)
NON_CLEARING_RECONCILE_REASON_CODES = frozenset(
    {
        "RECONCILE_FAILED",
        "SUBMIT_UNKNOWN_UNRESOLVED",
        "SOURCE_CONFLICT_HALT",
        "FILL_FEE_PENDING_RECOVERY_REQUIRED",
    }
)


@dataclass(frozen=True)
class ReconcileClearEvidence:
    open_orders_present: bool
    position_present: bool
    mismatch_count: int
    dust_effective_flat: bool


@dataclass(frozen=True)
class RecoveryController:
    state_snapshot: Callable[[], object]
    refresh_open_order_health: Callable[[], None]
    startup_gate_evaluator: Callable[[], str | None]
    reconcile_clear_evidence: Callable[[object], ReconcileClearEvidence]
    risk_state_clear_allowed: Callable[..., bool]
    enable_trading: Callable[[], None]
    disable_trading_until: Callable[..., None]
    set_resume_gate: Callable[..., None]
    auto_recovery_allowed: Callable[..., bool] | None = None
    logger: Callable[..., None] | None = None

    def evaluate_clearance(
        self,
        *,
        snapshot: object,
        startup_gate_reason: str | None,
        clearance_type: str,
    ) -> RecoveryClearance:
        reason_code = str(getattr(snapshot, "halt_reason_code", None) or "")
        base_evidence = {
            "clearance_type": clearance_type,
            "halt_new_orders_blocked": bool(getattr(snapshot, "halt_new_orders_blocked", False)),
            "halt_state_unresolved": bool(getattr(snapshot, "halt_state_unresolved", False)),
            "halt_reason_code": reason_code,
            "startup_gate_reason": startup_gate_reason,
            "last_reconcile_status": getattr(snapshot, "last_reconcile_status", None),
            "last_reconcile_reason_code": getattr(snapshot, "last_reconcile_reason_code", None),
            "unresolved_open_order_count": int(getattr(snapshot, "unresolved_open_order_count", 0) or 0),
            "recovery_required_count": int(getattr(snapshot, "recovery_required_count", 0) or 0),
        }
        transition = StateTransitionResult(
            status="not_allowed",
            reason_code=reason_code or clearance_type,
            state_from="HALTED" if bool(getattr(snapshot, "halt_new_orders_blocked", False)) else "READY",
            state_to="READY",
            applied=False,
        )
        if not (
            bool(getattr(snapshot, "halt_new_orders_blocked", False))
            and bool(getattr(snapshot, "halt_state_unresolved", False))
        ):
            return RecoveryClearance(
                status="not_halted",
                reason_code=reason_code or clearance_type,
                allowed=False,
                state_transition=transition,
                evidence=base_evidence,
            )

        if clearance_type == "startup_gate_auto_recovery_continue":
            allowed = reason_code == "STARTUP_SAFETY_GATE" and self._startup_gate_allows_auto_recovery(
                snapshot=snapshot,
                startup_gate_reason=startup_gate_reason,
            )
            status = "allowed" if allowed else "blocked"
            return RecoveryClearance(
                status=status,
                reason_code=reason_code,
                allowed=allowed,
                state_transition=StateTransitionResult(
                    status=status,
                    reason_code=reason_code,
                    state_from="HALTED",
                    state_to="DEGRADED_RECOVERY_CONTINUE",
                    applied=False,
                    evidence=base_evidence,
                ),
                evidence=base_evidence,
            )

        if clearance_type == "risk_state_mismatch":
            allowed = reason_code == RISK_STATE_MISMATCH and bool(
                self.risk_state_clear_allowed(state=snapshot, startup_gate_reason=startup_gate_reason)
            )
            status = "allowed" if allowed else "blocked"
            return RecoveryClearance(
                status=status,
                reason_code=reason_code,
                allowed=allowed,
                state_transition=StateTransitionResult(
                    status=status,
                    reason_code=reason_code,
                    state_from="HALTED",
                    state_to="READY",
                    applied=False,
                    evidence=base_evidence,
                ),
                evidence=base_evidence,
            )

        allowed_reason = (
            reason_code == "LIVE_EXECUTION_BROKER_ERROR"
            if clearance_type == "live_execution_broker"
            else reason_code in SAFE_CLEARABLE_RECONCILE_HALT_REASON_CODES
        )
        evidence = self.reconcile_clear_evidence(snapshot)
        evidence_payload = {
            **base_evidence,
            "open_orders_present": bool(evidence.open_orders_present),
            "position_present": bool(evidence.position_present),
            "mismatch_count": int(evidence.mismatch_count),
            "dust_effective_flat": bool(evidence.dust_effective_flat),
        }
        reconcile_reason = str(getattr(snapshot, "last_reconcile_reason_code", None) or "").strip()
        allowed = bool(
            allowed_reason
            and getattr(snapshot, "last_reconcile_status", None) == "ok"
            and reconcile_reason
            and reconcile_reason not in NON_CLEARING_RECONCILE_REASON_CODES
            and int(evidence.mismatch_count) == 0
            and not startup_gate_reason
            and int(getattr(snapshot, "unresolved_open_order_count", 0) or 0) == 0
            and int(getattr(snapshot, "recovery_required_count", 0) or 0) == 0
            and not bool(evidence.open_orders_present)
            and (not bool(evidence.position_present) or bool(evidence.dust_effective_flat))
        )
        status = "allowed" if allowed else "blocked"
        return RecoveryClearance(
            status=status,
            reason_code=reason_code,
            allowed=allowed,
            state_transition=StateTransitionResult(
                status=status,
                reason_code=reason_code,
                state_from="HALTED",
                state_to="READY",
                applied=False,
                evidence=evidence_payload,
            ),
            evidence=evidence_payload,
        )

    def apply_clearance(self, clearance: RecoveryClearance) -> StateTransitionResult:
        if not clearance.allowed:
            return StateTransitionResult(
                status="not_applied",
                reason_code=clearance.reason_code,
                state_from="HALTED",
                state_to="HALTED",
                applied=False,
                evidence=clearance.as_dict(),
            )
        if clearance.status == "allowed" and clearance.reason_code == "STARTUP_SAFETY_GATE":
            self.enable_trading()
            self.set_resume_gate(blocked=True, reason=clearance.evidence.get("startup_gate_reason"))
            state_to = "DEGRADED_RECOVERY_CONTINUE"
        else:
            snapshot = self.state_snapshot()
            clear_type = str(clearance.evidence.get("clearance_type") or "")
            self.disable_trading_until(
                float("inf"),
                reason=None if clear_type == "initial_reconcile" else getattr(snapshot, "last_disable_reason", None),
                halt_new_orders_blocked=False,
                unresolved=False,
            )
            self.set_resume_gate(blocked=False, reason=None)
            state_to = "READY"
        return StateTransitionResult(
            status="applied",
            reason_code=clearance.reason_code,
            state_from="HALTED",
            state_to=state_to,
            applied=True,
            evidence=clearance.as_dict(),
        )

    def evaluate_and_apply(self, *, clearance_type: str, startup_gate_reason: str | None = None) -> bool:
        self.refresh_open_order_health()
        snapshot = self.state_snapshot()
        gate_reason = startup_gate_reason if startup_gate_reason is not None else self.startup_gate_evaluator()
        clearance = self.evaluate_clearance(
            snapshot=snapshot,
            startup_gate_reason=gate_reason,
            clearance_type=clearance_type,
        )
        if not clearance.allowed:
            return False
        self.apply_clearance(clearance)
        return True

    def _startup_gate_allows_auto_recovery(self, *, snapshot: object, startup_gate_reason: str | None) -> bool:
        reason = str(startup_gate_reason or "")
        if self.auto_recovery_allowed is not None:
            return bool(
                self.auto_recovery_allowed(
                    state=snapshot,
                    startup_gate_reason=startup_gate_reason,
                )
            )
        return bool(
            "fee_pending_auto_recovering=" in reason
            and getattr(snapshot, "last_reconcile_status", None) == "ok"
            and int(getattr(snapshot, "recovery_required_count", 0) or 0) == 0
        )


__all__ = [
    "NON_CLEARING_RECONCILE_REASON_CODES",
    "RecoveryController",
    "ReconcileClearEvidence",
    "SAFE_CLEARABLE_RECONCILE_HALT_REASON_CODES",
]
