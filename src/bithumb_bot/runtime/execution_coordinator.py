from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..decision_equivalence import sha256_prefixed


@dataclass(frozen=True)
class ExecutionCycleResult:
    candle_ts: int
    decision_id: int | None
    planning_status: str
    submit_expected: bool
    submitted: bool
    post_trade_reconciled: bool
    mark_processed_allowed: bool
    halt_transition: Mapping[str, Any] | None = None
    input_hash: str | None = None
    evidence_hash: str | None = None
    decision_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "artifact_type": "execution_cycle_result",
            "schema_version": 1,
            "candle_ts": self.candle_ts,
            "decision_id": self.decision_id,
            "planning_status": self.planning_status,
            "submit_expected": bool(self.submit_expected),
            "submitted": bool(self.submitted),
            "post_trade_reconciled": bool(self.post_trade_reconciled),
            "mark_processed_allowed": bool(self.mark_processed_allowed),
            "halt_transition": dict(self.halt_transition or {}),
            "input_hash": self.input_hash
            or sha256_prefixed({"candle_ts": self.candle_ts, "decision_id": self.decision_id}),
            "evidence_hash": self.evidence_hash
            or sha256_prefixed(
                {
                    "planning_status": self.planning_status,
                    "submit_expected": bool(self.submit_expected),
                    "submitted": bool(self.submitted),
                    "post_trade_reconciled": bool(self.post_trade_reconciled),
                }
            ),
        }
        payload["decision_hash"] = self.decision_hash or sha256_prefixed(payload)
        return payload


@dataclass(frozen=True)
class ExecutionCoordinator:
    execution_engine_name: str

    def target_delta_submit_expected(self, *, submit_expected: bool) -> bool:
        return self.execution_engine_name.strip().lower() == "target_delta" and bool(submit_expected)


__all__ = ["ExecutionCoordinator", "ExecutionCycleResult"]
