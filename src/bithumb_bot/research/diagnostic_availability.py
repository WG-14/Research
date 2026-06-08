from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


DiagnosticAvailabilityStatus = Literal["available", "degraded", "unavailable"]


@dataclass(frozen=True)
class DiagnosticAvailability:
    status: DiagnosticAvailabilityStatus
    fail_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    target_count: int
    sample_count: int
    feature_value_count: int

    def __post_init__(self) -> None:
        if self.status not in {"available", "degraded", "unavailable"}:
            raise ValueError(f"invalid diagnostic availability status={self.status!r}")
        if self.status == "available" and (self.sample_count <= 0 or self.target_count <= 0):
            raise ValueError("available diagnostics require positive target and sample counts")
        if self.status == "unavailable" and not self.fail_reasons:
            raise ValueError("unavailable diagnostics require fail_reasons")

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "fail_reasons": list(self.fail_reasons),
            "warnings": list(self.warnings),
            "target_count": self.target_count,
            "sample_count": self.sample_count,
            "feature_value_count": self.feature_value_count,
        }


def build_diagnostic_availability(
    *,
    candle_count: int,
    horizons: tuple[int, ...],
    target_count: int,
    sample_count: int,
    feature_value_count: int,
    warnings: tuple[str, ...] = (),
) -> DiagnosticAvailability:
    reasons: list[str] = []
    if horizons and candle_count <= max(horizons):
        reasons.append("horizon_exceeds_dataset")
    if target_count == 0:
        reasons.append("no_forward_targets")
    if sample_count == 0:
        reasons.append("no_feature_observations")
    if target_count > 0 and feature_value_count == 0:
        reasons.append("all_features_missing")
    fail_reasons = tuple(dict.fromkeys(reasons))
    if fail_reasons:
        return DiagnosticAvailability(
            status="unavailable",
            fail_reasons=fail_reasons,
            warnings=tuple(dict.fromkeys(warnings)),
            target_count=int(target_count),
            sample_count=int(sample_count),
            feature_value_count=int(feature_value_count),
        )
    status: DiagnosticAvailabilityStatus = "degraded" if warnings else "available"
    return DiagnosticAvailability(
        status=status,
        fail_reasons=(),
        warnings=tuple(dict.fromkeys(warnings)),
        target_count=int(target_count),
        sample_count=int(sample_count),
        feature_value_count=int(feature_value_count),
    )
