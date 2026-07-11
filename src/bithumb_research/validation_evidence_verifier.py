from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class ValidationEvidenceVerification:
    accepted: bool
    reason_codes: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.accepted

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": bool(self.accepted),
            "ok": bool(self.accepted),
            "reason_codes": list(self.reason_codes),
        }


def verify_validation_candidate_execution_evidence(
    payload: Mapping[str, object],
) -> ValidationEvidenceVerification:
    """Single validation execution-evidence gate for typed, non-fallback evidence."""
    failures: list[str] = []
    if payload.get("compatibility_fallback") is not False:
        failures.append("validation_evidence_compatibility_fallback")
    if payload.get("research_compatibility_execution_fallback") is not False:
        failures.append("validation_evidence_research_compatibility_execution_fallback")
    if payload.get("validation_grade") is not True:
        failures.append("validation_evidence_not_validation_grade")
    if str(payload.get("artifact_grade") or "") == "diagnostic_only":
        failures.append("validation_evidence_diagnostic_only_artifact_grade")
    if str(payload.get("authority_plane") or "") == "diagnostic_research_compatibility_only":
        failures.append("validation_evidence_diagnostic_authority_plane")
    if payload.get("typed_execution_summary_present") is not True:
        failures.append("validation_evidence_typed_execution_summary_missing")
    if payload.get("typed_submit_plan") is not True:
        failures.append("validation_evidence_typed_submit_plan_missing")
    if payload.get("execution_plan_bundle_present") is not True:
        failures.append("validation_evidence_execution_plan_bundle_missing")
    if (
        payload.get("compatibility_fallback") is True
        and str(payload.get("recommended_next_action") or "") == "none"
    ):
        failures.append("validation_evidence_fallback_requires_regeneration_action")
    reason_codes = tuple(sorted(set(failures)))
    return ValidationEvidenceVerification(
        accepted=not reason_codes,
        reason_codes=reason_codes,
    )


__all__ = [
    "ValidationEvidenceVerification",
    "verify_validation_candidate_execution_evidence",
]
