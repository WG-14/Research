from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from bithumb_bot.evidence_safety import (
    DIAGNOSTIC_FEATURE_MINING_FORBIDDEN_USES,
    DIAGNOSTIC_FEATURE_MINING_OPERATOR_NEXT_ACTION,
    DIAGNOSTIC_FEATURE_MINING_SCOPE,
    diagnostic_feature_mining_taxonomy,
)


FORWARD_DIAGNOSTIC_ARTIFACT_TYPES = frozenset(
    {
        "forward_return_diagnostic_report",
        "forward_return_diagnostic_warnings",
        "forward_return_diagnostic_failure",
        "forward_return_diagnostic_policy_denial",
    }
)


@dataclass(frozen=True)
class ArtifactContract:
    artifact_type: str
    evidence_scope: str
    diagnostic_only: bool
    forbidden_uses: tuple[str, ...]
    operator_next_action: str = DIAGNOSTIC_FEATURE_MINING_OPERATOR_NEXT_ACTION

    def common_fields(self) -> dict[str, object]:
        return {
            "diagnostic_only": self.diagnostic_only,
            "promotion_evidence": False,
            "approved_profile_evidence": False,
            "live_readiness_evidence": False,
            "capital_allocation_evidence": False,
            **diagnostic_feature_mining_taxonomy(operator_next_action=self.operator_next_action),
        }


_ARTIFACT_CONTRACTS: dict[str, ArtifactContract] = {
    artifact_type: ArtifactContract(
        artifact_type=artifact_type,
        evidence_scope=DIAGNOSTIC_FEATURE_MINING_SCOPE,
        diagnostic_only=True,
        forbidden_uses=DIAGNOSTIC_FEATURE_MINING_FORBIDDEN_USES,
    )
    for artifact_type in FORWARD_DIAGNOSTIC_ARTIFACT_TYPES
}


def artifact_contract_for_type(artifact_type: str) -> ArtifactContract:
    try:
        return _ARTIFACT_CONTRACTS[str(artifact_type)]
    except KeyError as exc:
        raise ValueError(f"unknown diagnostic artifact_type={artifact_type!r}") from exc


def apply_artifact_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    artifact_type = str(payload.get("artifact_type") or "")
    contract = artifact_contract_for_type(artifact_type)
    operator_next_action = str(payload.get("operator_next_action") or contract.operator_next_action)
    common_fields = {
        **contract.common_fields(),
        **diagnostic_feature_mining_taxonomy(operator_next_action=operator_next_action),
    }
    merged = dict(payload)
    merged.update(common_fields)
    merged["artifact_type"] = artifact_type
    validate_artifact_contract(merged)
    return merged


def validate_artifact_contract(payload: Mapping[str, Any]) -> None:
    artifact_type = str(payload.get("artifact_type") or "")
    contract = artifact_contract_for_type(artifact_type)
    if payload.get("diagnostic_only") is not contract.diagnostic_only:
        raise ValueError(f"{artifact_type} must be diagnostic_only")
    if any(
        bool(payload.get(field))
        for field in (
            "promotion_evidence",
            "approved_profile_evidence",
            "live_readiness_evidence",
            "capital_allocation_evidence",
            "promotion_eligible",
            "promotion_grade",
        )
    ):
        raise ValueError(f"{artifact_type} must remain diagnostic-only")
    if payload.get("non_promotable") is not True:
        raise ValueError(f"{artifact_type} must be non_promotable")
    if payload.get("evidence_scope") != contract.evidence_scope:
        raise ValueError(f"{artifact_type} evidence_scope mismatch")
    forbidden_uses = payload.get("forbidden_uses")
    if not isinstance(forbidden_uses, list) or not set(contract.forbidden_uses).issubset(
        {str(item) for item in forbidden_uses}
    ):
        raise ValueError(f"{artifact_type} forbidden_uses incomplete")
    if not str(payload.get("operator_next_action") or "").strip():
        raise ValueError(f"{artifact_type} operator_next_action required")


def diagnostic_artifact_rejection_reasons(payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    artifact_type = str(payload.get("artifact_type") or "")
    if artifact_type not in _ARTIFACT_CONTRACTS:
        return ()
    reasons = [
        "diagnostic_feature_mining_not_promotable",
        "diagnostic_only_evidence_artifact",
        "non_promotable_evidence_artifact",
        "promotion_eligible_false",
        "promotion_grade_validation_required",
    ]
    for forbidden_use in _ARTIFACT_CONTRACTS[artifact_type].forbidden_uses:
        reasons.append(f"forbidden_use:{forbidden_use}")
    try:
        validate_artifact_contract(payload)
    except ValueError as exc:
        reasons.append(f"artifact_contract_invalid:{exc}")
    return tuple(sorted(set(reasons)))
