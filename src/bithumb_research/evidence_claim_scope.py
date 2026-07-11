from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class EvidenceArtifactType(StrEnum):
    SYNTHETIC_GATE = "SyntheticGateEvidence"
    BROKER_PIPELINE_SMOKE = "BrokerPipelineSmokeEvidence"
    DECISION_PARITY = "DecisionParityEvidence"
    PLANNING_PARITY = "PlanningParityEvidence"
    PAIRED_EXPERIMENT = "PairedExperimentEvidence"
    LIVE_SUBMIT = "LiveSubmitEvidence"
    FULL_LIFECYCLE_COMPARISON = "FullLifecycleComparison"


@dataclass(frozen=True)
class EvidenceClaimScope:
    artifact_type: EvidenceArtifactType
    claims_scope: str
    full_lifecycle_equivalence_supported: bool
    submit_plan_equivalence_supported: bool = False

    @classmethod
    def parse_and_validate(cls, payload: Mapping[str, object]) -> "EvidenceClaimScope":
        raw_type = str(payload.get("artifact_type") or "").strip()
        if not raw_type:
            raise ValueError("evidence_artifact_type_missing")
        try:
            artifact_type = EvidenceArtifactType(raw_type)
        except ValueError as exc:
            raise ValueError(f"evidence_artifact_type_unknown:{raw_type}") from exc
        claims_payload = payload.get("claims_scope")
        if isinstance(claims_payload, Mapping):
            claims_scope = str(claims_payload.get("scope") or claims_payload.get("claim_scope") or "").strip()
            full_lifecycle = bool(claims_payload.get("full_lifecycle_equivalence_supported"))
            submit_plan = bool(claims_payload.get("submit_plan_equivalence_supported"))
        else:
            claims_scope = str(claims_payload or "").strip()
            full_lifecycle = bool(payload.get("full_lifecycle_equivalence_supported"))
            submit_plan = bool(payload.get("submit_plan_equivalence_supported"))
        if not claims_scope:
            raise ValueError("evidence_claims_scope_missing")
        if artifact_type == EvidenceArtifactType.SYNTHETIC_GATE and full_lifecycle:
            raise ValueError("synthetic_gate_full_lifecycle_claim_forbidden")
        return cls(
            artifact_type=artifact_type,
            claims_scope=claims_scope,
            full_lifecycle_equivalence_supported=full_lifecycle,
            submit_plan_equivalence_supported=submit_plan,
        )

    def require(
        self,
        required_type: EvidenceArtifactType,
        *,
        required_scope: str | None = None,
        allow_diagnostic_only: bool = False,
    ) -> None:
        if self.artifact_type != required_type:
            raise ValueError(f"evidence_artifact_type_mismatch:{required_type.value}:{self.artifact_type.value}")
        if required_scope is not None and self.claims_scope != required_scope:
            raise ValueError(f"evidence_claim_scope_mismatch:{required_scope}:{self.claims_scope}")
        if not allow_diagnostic_only and self.claims_scope.endswith("_diagnostic_only"):
            raise ValueError(f"evidence_claim_scope_diagnostic_only:{self.claims_scope}")


def require_artifact_claim_scope(
    payload: Mapping[str, object],
    *,
    required_type: EvidenceArtifactType,
    required_scope: str | None = None,
    allow_diagnostic_only: bool = False,
) -> EvidenceClaimScope:
    scope = EvidenceClaimScope.parse_and_validate(payload)
    scope.require(required_type, required_scope=required_scope, allow_diagnostic_only=allow_diagnostic_only)
    return scope


__all__ = ["EvidenceArtifactType", "EvidenceClaimScope", "require_artifact_claim_scope"]
