"""Research-only experiment classification contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RESEARCH_CLASSIFICATIONS = frozenset(
    {"research_only", "exploratory", "validated_candidate", "rejected"}
)


def normalize_research_classification(value: object | None) -> str:
    classification = str(value or "research_only").strip().lower()
    if classification not in RESEARCH_CLASSIFICATIONS:
        allowed = ", ".join(sorted(RESEARCH_CLASSIFICATIONS))
        raise ValueError(
            f"research_classification must be one of {allowed}; got {classification!r}"
        )
    return classification


def requires_candidate_validation(value: object | None) -> bool:
    """Whether the research classification requires the full validation contract."""

    return normalize_research_classification(value) == "validated_candidate"


@dataclass(frozen=True)
class ExecutionCalibrationPolicyResult:
    classification: str
    status: str = "NOT_REQUIRED"
    reasons: tuple[str, ...] = ()
    policy_source: str = "research_classification_v2"
    artifact_hash: str | None = None
    artifact_hashes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "status": self.status,
            "reasons": list(self.reasons),
            "policy_source": self.policy_source,
            "artifact_hash": self.artifact_hash,
            "artifact_hashes": list(self.artifact_hashes),
        }


def validate_execution_calibration_policy(
    payload: dict[str, Any], *, target: object | None = None
) -> ExecutionCalibrationPolicyResult:
    del payload
    return ExecutionCalibrationPolicyResult(
        classification=normalize_research_classification(target)
    )
