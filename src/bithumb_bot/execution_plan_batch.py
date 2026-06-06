from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

from .canonical_decision import sha256_prefixed


EXECUTION_PLAN_BATCH_SCHEMA_VERSION = 1


def _clean(value: object) -> str:
    return str(value or "").strip()


def _clean_hashes(values: object) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        raw_values: Sequence[object] = (values,)
    elif isinstance(values, Sequence):
        raw_values = values
    else:
        raw_values = tuple(values) if isinstance(values, set) else ()
    return tuple(sorted({_clean(item) for item in raw_values if _clean(item)}))


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
    scope_key_hashes: tuple[str, ...] = ()
    execution_plan_hash: str = ""
    order_rule_snapshot_hash: str = ""
    order_rule_signature: str = ""
    order_rule_snapshot: Mapping[str, object] = field(default_factory=dict)
    lock_evidence_hash: str = ""
    lock_type: str = "none"
    lock_status: str = "not_required"
    submit_expected: bool = False
    pre_submit_risk_required: bool = False
    pre_submit_risk_proof_status: str = ""
    pre_submit_risk_not_required_reason: str = ""
    pre_submit_risk_finalization_required: bool = False
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
            "order_rule_snapshot_hash",
            "order_rule_signature",
            "lock_evidence_hash",
            "lock_type",
            "lock_status",
            "pre_submit_risk_proof_status",
            "pre_submit_risk_not_required_reason",
        ):
            object.__setattr__(self, field_name, _clean(getattr(self, field_name)))
        scope_hashes = _clean_hashes(self.scope_key_hashes)
        if not scope_hashes and self.scope_key_hash:
            scope_hashes = (self.scope_key_hash,)
        object.__setattr__(self, "scope_key_hashes", scope_hashes)
        if not self.scope_key_hash and scope_hashes:
            object.__setattr__(self, "scope_key_hash", scope_hashes[0])
        object.__setattr__(
            self,
            "order_rule_snapshot",
            MappingProxyType({str(key): value for key, value in dict(self.order_rule_snapshot or {}).items()}),
        )
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
        if self.submit_expected and not self.order_rule_snapshot_hash:
            raise ValueError("pair_execution_plan_order_rule_evidence_missing")
        proof_required = bool(self.pre_submit_risk_required)
        proof_hash = _clean(self.pre_submit_risk_decision_hash)
        not_required_reason = _clean(self.pre_submit_risk_not_required_reason)
        pending_finalization = bool(self.pre_submit_risk_finalization_required)
        if proof_required and not proof_hash and not pending_finalization:
            raise ValueError("pair_execution_plan_pre_submit_risk_proof_missing")
        if not proof_required and not not_required_reason:
            raise ValueError("pair_execution_plan_pre_submit_risk_not_required_reason_missing")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": int(self.schema_version),
            "pair": self.pair,
            "scope_key_hash": self.scope_key_hash,
            "scope_key_hashes": list(self.scope_key_hashes),
            "portfolio_target_hash": self.portfolio_target_hash,
            "execution_submit_plan_hash": self.execution_submit_plan_hash,
            "execution_plan_hash": self.execution_plan_hash,
            "order_rule_snapshot_hash": self.order_rule_snapshot_hash,
            "order_rule_signature": self.order_rule_signature,
            "order_rule_snapshot": dict(self.order_rule_snapshot),
            "lock_evidence_hash": self.lock_evidence_hash,
            "lock_type": self.lock_type,
            "lock_status": self.lock_status,
            "idempotency_key": self.idempotency_key,
            "submit_authority_policy_hash": self.submit_authority_policy_hash,
            "pre_submit_risk_decision_hash": self.pre_submit_risk_decision_hash,
            "pre_submit_risk_required": bool(self.pre_submit_risk_required),
            "pre_submit_risk_proof_status": self.pre_submit_risk_proof_status,
            "pre_submit_risk_not_required_reason": self.pre_submit_risk_not_required_reason,
            "pre_submit_risk_finalization_required": bool(self.pre_submit_risk_finalization_required),
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


def verify_pair_plan_replay_complete(pair_plan_payload: Mapping[str, object]) -> dict[str, object]:
    payload = dict(pair_plan_payload)
    submit_expected = bool(payload.get("submit_expected"))
    pre_submit_required = bool(payload.get("pre_submit_risk_required"))
    missing: list[str] = []
    if not _clean(payload.get("pair")):
        missing.append("pair")
    if not _clean_hashes(payload.get("scope_key_hashes")):
        missing.append("scope_key_hashes")
    if not _clean(payload.get("order_rule_snapshot_hash")):
        missing.append("order_rule_snapshot_hash")
    if pre_submit_required:
        if not _clean(payload.get("pre_submit_risk_decision_hash")) and not bool(
            payload.get("pre_submit_risk_finalization_required")
        ):
            missing.append("pre_submit_risk_decision_hash")
    elif not _clean(payload.get("pre_submit_risk_not_required_reason")):
        missing.append("pre_submit_risk_not_required_reason")
    if submit_expected and not _clean(payload.get("order_rule_snapshot_hash")):
        missing.append("submit_expected_order_rule_evidence")
    return {
        "schema_version": 1,
        "status": "fail" if missing else "pass",
        "missing_fields": sorted(set(missing)),
        "pair_execution_plan_hash": sha256_prefixed(payload) if not missing else "",
    }


def build_pre_submit_risk_finalization_artifact(final_submit_payload: Mapping[str, object]) -> dict[str, object]:
    payload = dict(final_submit_payload)
    replay_hash_chain = {
        "schema_version": 1,
        "execution_plan_batch_hash": _clean(payload.get("execution_plan_batch_hash")),
        "pair_execution_plan_hash": _clean(payload.get("pair_execution_plan_hash")),
        "execution_submit_plan_hash": _clean(
            payload.get("submit_plan_hash") or payload.get("execution_submit_plan_hash")
        ),
        "pre_submit_risk_decision_hash": _clean(payload.get("pre_submit_risk_decision_hash")),
    }
    artifact = {
        "schema_version": 1,
        "artifact_type": "pre_submit_risk_finalization",
        "execution_plan_batch_hash": replay_hash_chain["execution_plan_batch_hash"],
        "execution_plan_batch_id": _clean(payload.get("execution_plan_batch_id")),
        "pair_execution_plan_hash": replay_hash_chain["pair_execution_plan_hash"],
        "pair_execution_plan_pair": _clean(payload.get("pair_execution_plan_pair")),
        "execution_submit_plan_hash": replay_hash_chain["execution_submit_plan_hash"],
        "final_submit_payload_content_hash": _clean(payload.get("content_hash")),
        "pre_submit_risk_decision_hash": replay_hash_chain["pre_submit_risk_decision_hash"],
        "pre_submit_risk_policy_hash": _clean(payload.get("pre_submit_risk_policy_hash")),
        "pre_submit_risk_input_hash": _clean(payload.get("pre_submit_risk_input_hash")),
        "pre_submit_risk_evidence_hash": _clean(payload.get("pre_submit_risk_evidence_hash")),
        "pre_submit_risk_plan_hash": _clean(payload.get("pre_submit_risk_plan_hash")),
        "replay_hash_chain": replay_hash_chain,
        "replay_hash_chain_hash": sha256_prefixed(replay_hash_chain),
    }
    artifact["pre_submit_risk_finalization_hash"] = sha256_prefixed(artifact)
    return artifact


def verify_pre_submit_risk_finalization_artifact(final_submit_payload: Mapping[str, object]) -> dict[str, object]:
    payload = dict(final_submit_payload)
    artifact = payload.get("pre_submit_risk_finalization_artifact")
    if not isinstance(artifact, Mapping):
        return {
            "schema_version": 1,
            "status": "fail",
            "mismatch_reason": "pre_submit_risk_finalization_artifact_missing",
        }
    expected = str(payload.get("pre_submit_risk_finalization_hash") or "").strip()
    recomputed = build_pre_submit_risk_finalization_artifact(
        {
            key: value
            for key, value in payload.items()
            if key not in {"pre_submit_risk_finalization_artifact", "pre_submit_risk_finalization_hash"}
        }
    )
    actual = str(recomputed.get("pre_submit_risk_finalization_hash") or "")
    checks = {
        "execution_plan_batch_hash": _clean(payload.get("execution_plan_batch_hash")),
        "pair_execution_plan_hash": _clean(payload.get("pair_execution_plan_hash")),
        "execution_submit_plan_hash": _clean(payload.get("submit_plan_hash")),
        "pre_submit_risk_decision_hash": _clean(payload.get("pre_submit_risk_decision_hash")),
    }
    mismatches = [
        key
        for key, value in checks.items()
        if _clean(dict(artifact).get(key)) != value
    ]
    if expected != actual:
        mismatches.append("pre_submit_risk_finalization_hash")
    return {
        "schema_version": 1,
        "status": "fail" if mismatches else "pass",
        "mismatch_reason": "" if not mismatches else ",".join(sorted(set(mismatches))),
        "expected_pre_submit_risk_finalization_hash": expected,
        "recomputed_pre_submit_risk_finalization_hash": actual,
    }
