"""Research classification only; this repository has no deployment tiers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEPLOYMENT_TIERS = frozenset({"research_only", "exploratory", "validated_candidate", "rejected"})
PRODUCTION_BOUND_TIERS = frozenset()


@dataclass(frozen=True)
class DeploymentCalibrationPolicyResult:
    target: str
    production_bound: bool = False
    required: bool = False
    status: str = "NOT_REQUIRED"
    reasons: tuple[str, ...] = ()
    artifact_hash: str | None = None
    artifact_hashes: tuple[str, ...] = ()
    policy_source: str = "research_classification_policy_v1"
    operator_next_step: str = "none"

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "production_bound": False,
            "required": False,
            "status": self.status,
            "reasons": list(self.reasons),
            "artifact_hash": None,
            "artifact_hashes": [],
            "policy_source": self.policy_source,
            "operator_next_step": self.operator_next_step,
        }


def normalize_deployment_tier(value: object | None) -> str:
    target = str(value or "research_only").strip().lower()
    if target not in DEPLOYMENT_TIERS:
        raise ValueError(f"unsupported_research_classification:{target}")
    return target


def deployment_tier_for_profile_mode(mode: object) -> str:
    raise ValueError("profile_modes_are_not_supported_in_bithumb_research")


def is_production_bound_target(target: object | None) -> bool:
    normalize_deployment_tier(target)
    return False


def validate_production_calibration_policy(
    payload: dict[str, Any], *, target: object | None = None
) -> DeploymentCalibrationPolicyResult:
    del payload
    return DeploymentCalibrationPolicyResult(target=normalize_deployment_tier(target))
