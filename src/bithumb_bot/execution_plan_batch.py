from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from .canonical_decision import sha256_prefixed


EXECUTION_PLAN_BATCH_SCHEMA_VERSION = 1


def _clean(value: object) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class PairExecutionPlan:
    pair: str
    portfolio_target_hash: str
    execution_submit_plan_hash: str
    idempotency_key: str
    submit_authority_policy_hash: str
    pre_submit_risk_decision_hash: str
    reconcile_status: str = "not_started"
    scope_key_hash: str = ""
    execution_plan_hash: str = ""
    submit_expected: bool = False
    replay_evidence: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "pair", _clean(self.pair))
        for field_name in (
            "portfolio_target_hash",
            "execution_submit_plan_hash",
            "idempotency_key",
            "submit_authority_policy_hash",
            "pre_submit_risk_decision_hash",
            "reconcile_status",
            "scope_key_hash",
            "execution_plan_hash",
        ):
            object.__setattr__(self, field_name, _clean(getattr(self, field_name)))
        object.__setattr__(
            self,
            "replay_evidence",
            MappingProxyType({str(key): value for key, value in dict(self.replay_evidence or {}).items()}),
        )
        missing = [
            field_name
            for field_name in (
                "pair",
                "portfolio_target_hash",
                "execution_submit_plan_hash",
                "idempotency_key",
                "submit_authority_policy_hash",
            )
            if not _clean(getattr(self, field_name))
        ]
        if missing:
            raise ValueError(f"pair_execution_plan_missing:{','.join(missing)}")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": int(self.schema_version),
            "pair": self.pair,
            "scope_key_hash": self.scope_key_hash,
            "portfolio_target_hash": self.portfolio_target_hash,
            "execution_submit_plan_hash": self.execution_submit_plan_hash,
            "execution_plan_hash": self.execution_plan_hash,
            "idempotency_key": self.idempotency_key,
            "submit_authority_policy_hash": self.submit_authority_policy_hash,
            "pre_submit_risk_decision_hash": self.pre_submit_risk_decision_hash,
            "reconcile_status": self.reconcile_status,
            "submit_expected": bool(self.submit_expected),
            "replay_evidence": dict(self.replay_evidence),
        }

    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict())


@dataclass(frozen=True)
class ExecutionPlanBatch:
    runtime_strategy_set_manifest_hash: str
    allocation_decision_hash: str
    pair_plans: tuple[PairExecutionPlan, ...]
    batch_risk_decision_evidence: Mapping[str, object]
    budget_lock_hash: str
    status: str = "planned"
    replay_evidence: Mapping[str, object] = field(default_factory=dict)
    batch_id: str = ""
    schema_version: int = EXECUTION_PLAN_BATCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "runtime_strategy_set_manifest_hash", _clean(self.runtime_strategy_set_manifest_hash))
        object.__setattr__(self, "allocation_decision_hash", _clean(self.allocation_decision_hash))
        object.__setattr__(self, "budget_lock_hash", _clean(self.budget_lock_hash))
        object.__setattr__(self, "status", _clean(self.status) or "planned")
        for plan in self.pair_plans:
            if not isinstance(plan, PairExecutionPlan):
                raise TypeError("execution_plan_batch_requires_pair_plans")
        sorted_plans = tuple(sorted(self.pair_plans, key=lambda item: (item.pair, item.idempotency_key)))
        object.__setattr__(self, "pair_plans", sorted_plans)
        object.__setattr__(
            self,
            "batch_risk_decision_evidence",
            MappingProxyType(dict(self.batch_risk_decision_evidence or {})),
        )
        object.__setattr__(
            self,
            "replay_evidence",
            MappingProxyType(dict(self.replay_evidence or {})),
        )
        missing = [
            field_name
            for field_name in (
                "runtime_strategy_set_manifest_hash",
                "allocation_decision_hash",
                "budget_lock_hash",
            )
            if not _clean(getattr(self, field_name))
        ]
        if missing:
            raise ValueError(f"execution_plan_batch_missing:{','.join(missing)}")
        if not sorted_plans:
            raise ValueError("execution_plan_batch_pair_plans_missing")
        if len({plan.pair for plan in sorted_plans}) != len(sorted_plans):
            raise ValueError("execution_plan_batch_duplicate_pair_plan")
        if not self.batch_id:
            object.__setattr__(
                self,
                "batch_id",
                sha256_prefixed(
                    {
                        "runtime_strategy_set_manifest_hash": self.runtime_strategy_set_manifest_hash,
                        "allocation_decision_hash": self.allocation_decision_hash,
                        "pair_plan_hashes": [plan.content_hash() for plan in sorted_plans],
                        "budget_lock_hash": self.budget_lock_hash,
                    }
                ),
            )

    def as_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": int(self.schema_version),
            "batch_id": self.batch_id,
            "runtime_strategy_set_manifest_hash": self.runtime_strategy_set_manifest_hash,
            "allocation_decision_hash": self.allocation_decision_hash,
            "pair_plans": [plan.as_dict() for plan in self.pair_plans],
            "pair_plan_hashes": [plan.content_hash() for plan in self.pair_plans],
            "batch_risk_decision_evidence": dict(self.batch_risk_decision_evidence),
            "batch_risk_decision_evidence_hash": sha256_prefixed(dict(self.batch_risk_decision_evidence)),
            "budget_lock_hash": self.budget_lock_hash,
            "status": self.status,
            "replay_evidence": dict(self.replay_evidence),
        }
        payload["batch_hash"] = sha256_prefixed(
            {key: value for key, value in payload.items() if key != "batch_hash"}
        )
        return payload

    def content_hash(self) -> str:
        return str(self.as_dict()["batch_hash"])


def reject_dict_only_batch_authority(payload: object) -> None:
    if isinstance(payload, Mapping):
        raise TypeError("dict_only_execution_batch_not_authority")
