from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


FeatureHorizonCoverageStatus = Literal["available", "degraded", "unavailable"]


@dataclass(frozen=True)
class FeatureHorizonCoverage:
    feature_name: str
    horizon_label: str
    requested: bool
    computed_count: int
    missing_count: int
    status: FeatureHorizonCoverageStatus
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "feature_name": self.feature_name,
            "horizon_label": self.horizon_label,
            "requested": self.requested,
            "computed_count": self.computed_count,
            "missing_count": self.missing_count,
            "status": self.status,
            "reasons": list(self.reasons),
        }


def build_feature_horizon_coverage(
    *,
    feature_names: tuple[str, ...],
    horizon_labels: tuple[str, ...],
    target_counts_by_horizon: dict[str, int],
    computed_counts: dict[tuple[str, str], int],
) -> tuple[FeatureHorizonCoverage, ...]:
    rows: list[FeatureHorizonCoverage] = []
    for feature_name in feature_names:
        for horizon_label in horizon_labels:
            target_count = int(target_counts_by_horizon.get(horizon_label, 0))
            computed_count = int(computed_counts.get((feature_name, horizon_label), 0))
            missing_count = max(target_count - computed_count, 0)
            reasons: list[str] = []
            if target_count == 0:
                reasons.append("no_forward_targets")
            if computed_count == 0 and target_count > 0:
                reasons.append("feature_history_unavailable")
            elif missing_count > 0:
                reasons.append("partial_feature_history_unavailable")
            if computed_count == 0:
                status: FeatureHorizonCoverageStatus = "unavailable"
            elif missing_count > 0:
                status = "degraded"
            else:
                status = "available"
            rows.append(
                FeatureHorizonCoverage(
                    feature_name=feature_name,
                    horizon_label=horizon_label,
                    requested=True,
                    computed_count=computed_count,
                    missing_count=missing_count,
                    status=status,
                    reasons=tuple(reasons),
                )
            )
    return tuple(rows)
