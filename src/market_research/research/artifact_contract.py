from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .evidence_policy import (
    DIAGNOSTIC_FEATURE_MINING_FORBIDDEN_USES,
    DIAGNOSTIC_FEATURE_MINING_RESEARCHER_NEXT_ACTION,
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
    forbidden_uses: tuple[str, ...]
    researcher_next_action: str = DIAGNOSTIC_FEATURE_MINING_RESEARCHER_NEXT_ACTION

    def common_fields(self) -> dict[str, object]:
        return diagnostic_feature_mining_taxonomy(
            researcher_next_action=self.researcher_next_action
        )


_ARTIFACT_CONTRACTS = {
    artifact_type: ArtifactContract(
        artifact_type=artifact_type,
        evidence_scope=DIAGNOSTIC_FEATURE_MINING_SCOPE,
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
    researcher_next_action = str(
        payload.get("researcher_next_action") or contract.researcher_next_action
    )
    merged = dict(payload)
    merged.update(
        diagnostic_feature_mining_taxonomy(
            researcher_next_action=researcher_next_action
        )
    )
    merged["artifact_type"] = artifact_type
    merged["schema_version"] = 2
    validate_artifact_contract(merged)
    return merged


def validate_artifact_contract(payload: Mapping[str, Any]) -> None:
    artifact_type = str(payload.get("artifact_type") or "")
    contract = artifact_contract_for_type(artifact_type)
    if (
        payload.get("artifact_role") != "diagnostic"
        or payload.get("diagnostic_only") is not True
    ):
        raise ValueError(f"{artifact_type} must remain diagnostic-only")
    if payload.get("validation_evidence") is not False:
        raise ValueError(f"{artifact_type} must not be validation evidence")
    if payload.get("candidate_selection_eligible") is not False:
        raise ValueError(f"{artifact_type} must not be candidate-selection evidence")
    if payload.get("evidence_scope") != contract.evidence_scope:
        raise ValueError(f"{artifact_type} evidence_scope mismatch")
    forbidden_uses = payload.get("forbidden_uses")
    if not isinstance(forbidden_uses, list) or not set(
        contract.forbidden_uses
    ).issubset({str(item) for item in forbidden_uses}):
        raise ValueError(f"{artifact_type} forbidden_uses incomplete")
    if not str(payload.get("researcher_next_action") or "").strip():
        raise ValueError(f"{artifact_type} researcher_next_action required")


def diagnostic_artifact_rejection_reasons(
    payload: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    artifact_type = str(payload.get("artifact_type") or "")
    if artifact_type not in _ARTIFACT_CONTRACTS:
        return ()
    reasons = [
        "diagnostic_artifact_not_validation_evidence",
        "diagnostic_artifact_not_candidate_selection_evidence",
    ]
    reasons.extend(
        f"forbidden_use:{value}"
        for value in _ARTIFACT_CONTRACTS[artifact_type].forbidden_uses
    )
    try:
        validate_artifact_contract(payload)
    except ValueError as exc:
        reasons.append(f"artifact_contract_invalid:{exc}")
    return tuple(sorted(set(reasons)))
