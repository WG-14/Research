from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..decision_equivalence import sha256_prefixed


def _stable_hash(payload: Mapping[str, Any] | Sequence[Any] | None) -> str:
    return sha256_prefixed({} if payload is None else payload)


@dataclass(frozen=True)
class StateTransitionResult:
    status: str
    reason_code: str
    state_from: str | None = None
    state_to: str | None = None
    applied: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)
    input_hash: str | None = None
    evidence_hash: str | None = None
    decision_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "artifact_type": "state_transition_result",
            "schema_version": 1,
            "status": self.status,
            "reason_code": self.reason_code,
            "state_from": self.state_from,
            "state_to": self.state_to,
            "applied": bool(self.applied),
            "evidence": dict(self.evidence),
            "input_hash": self.input_hash or _stable_hash({"reason_code": self.reason_code, "state_from": self.state_from}),
            "evidence_hash": self.evidence_hash or _stable_hash(self.evidence),
        }
        payload["decision_hash"] = self.decision_hash or _stable_hash(payload)
        return payload


@dataclass(frozen=True)
class SafetyDecision:
    action: str
    reason_code: str
    reason: str
    unresolved: bool = False
    attempt_flatten: bool = False
    state_transition: StateTransitionResult | Mapping[str, Any] | None = None
    operator_event: Mapping[str, Any] | None = None
    input_hash: str | None = None
    evidence_hash: str | None = None
    decision_hash: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        transition = (
            self.state_transition.as_dict()
            if isinstance(self.state_transition, StateTransitionResult)
            else dict(self.state_transition or {})
        )
        payload = {
            "artifact_type": "safety_decision",
            "schema_version": 1,
            "action": self.action,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "unresolved": bool(self.unresolved),
            "attempt_flatten": bool(self.attempt_flatten),
            "state_transition": transition,
            "operator_event": dict(self.operator_event or {}),
            "operator_event_hashes": [_stable_hash(self.operator_event or {})] if self.operator_event else [],
            "evidence": dict(self.evidence),
            "input_hash": self.input_hash or _stable_hash({"reason_code": self.reason_code, "reason": self.reason}),
            "evidence_hash": self.evidence_hash or _stable_hash(self.evidence),
        }
        payload["decision_hash"] = self.decision_hash or _stable_hash(payload)
        return payload


@dataclass(frozen=True)
class RecoveryClearance:
    status: str
    reason_code: str
    allowed: bool
    state_transition: Mapping[str, Any] | StateTransitionResult | None = None
    operator_event_hashes: Sequence[str] = ()
    input_hash: str | None = None
    evidence_hash: str | None = None
    decision_hash: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        transition = (
            self.state_transition.as_dict()
            if isinstance(self.state_transition, StateTransitionResult)
            else dict(self.state_transition or {})
        )
        payload = {
            "artifact_type": "recovery_clearance",
            "schema_version": 1,
            "status": self.status,
            "reason_code": self.reason_code,
            "allowed": bool(self.allowed),
            "state_transition": transition,
            "operator_event_hashes": list(self.operator_event_hashes),
            "evidence": dict(self.evidence),
            "input_hash": self.input_hash or _stable_hash({"reason_code": self.reason_code, "evidence": self.evidence}),
            "evidence_hash": self.evidence_hash or _stable_hash(self.evidence),
        }
        payload["decision_hash"] = self.decision_hash or _stable_hash(payload)
        return payload


@dataclass(frozen=True)
class StartupResult:
    status: str
    broker: object | None = None
    startup_gate_reason: str | None = None
    reason_code: str | None = None
    operator_event: Mapping[str, Any] | None = None
    halt_transition: Mapping[str, Any] | StateTransitionResult | None = None
    input_hash: str | None = None
    evidence_hash: str | None = None
    decision_hash: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        transition = (
            self.halt_transition.as_dict()
            if isinstance(self.halt_transition, StateTransitionResult)
            else dict(self.halt_transition or {})
        )
        payload = {
            "artifact_type": "startup_result",
            "schema_version": 1,
            "status": self.status,
            "reason_code": self.reason_code,
            "startup_gate_reason": self.startup_gate_reason,
            "broker_present": self.broker is not None,
            "operator_event": dict(self.operator_event or {}),
            "operator_event_hashes": [_stable_hash(self.operator_event or {})] if self.operator_event else [],
            "halt_transition": transition,
            "evidence": dict(self.evidence),
            "input_hash": self.input_hash or _stable_hash({"status": self.status, "startup_gate_reason": self.startup_gate_reason}),
            "evidence_hash": self.evidence_hash or _stable_hash(self.evidence),
        }
        payload["decision_hash"] = self.decision_hash or _stable_hash(payload)
        return payload


@dataclass(frozen=True)
class RuntimeCycleArtifact:
    cycle_id: str
    candle_ts: int | None
    startup_state: str | None = None
    readiness_hash: str | None = None
    strategy_decision_hash: str | None = None
    execution_plan_bundle_hash: str | None = None
    safety_decision_hash: str | None = None
    recovery_decision_hash: str | None = None
    state_transition_hash: str | None = None
    notification_event_hashes: Sequence[str] = ()

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "artifact_type": "runtime_cycle_artifact",
            "schema_version": 1,
            "cycle_id": self.cycle_id,
            "candle_ts": self.candle_ts,
            "startup_state": self.startup_state,
            "readiness_hash": self.readiness_hash,
            "strategy_decision_hash": self.strategy_decision_hash,
            "execution_plan_bundle_hash": self.execution_plan_bundle_hash,
            "safety_decision_hash": self.safety_decision_hash,
            "recovery_decision_hash": self.recovery_decision_hash,
            "state_transition_hash": self.state_transition_hash,
            "notification_event_hashes": list(self.notification_event_hashes),
        }
        payload["input_hash"] = _stable_hash(
            {
                "cycle_id": self.cycle_id,
                "candle_ts": self.candle_ts,
                "startup_state": self.startup_state,
            }
        )
        payload["evidence_hash"] = _stable_hash(
            {
                "readiness_hash": self.readiness_hash,
                "strategy_decision_hash": self.strategy_decision_hash,
                "execution_plan_bundle_hash": self.execution_plan_bundle_hash,
                "safety_decision_hash": self.safety_decision_hash,
                "recovery_decision_hash": self.recovery_decision_hash,
                "state_transition_hash": self.state_transition_hash,
                "notification_event_hashes": list(self.notification_event_hashes),
            }
        )
        payload["decision_hash"] = _stable_hash(payload)
        return payload


__all__ = [
    "RecoveryClearance",
    "RuntimeCycleArtifact",
    "SafetyDecision",
    "StartupResult",
    "StateTransitionResult",
]
