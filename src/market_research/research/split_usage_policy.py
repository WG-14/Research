from __future__ import annotations

from typing import Any


FINAL_HOLDOUT_DIAGNOSTIC_OVERRIDE_REQUIRED = "final_holdout_diagnostic_override_required"
FINAL_HOLDOUT_DIAGNOSTIC_CONTAMINATION_RISK = "final_holdout_diagnostic_contamination_risk"


class SplitUsagePolicyError(ValueError):
    def __init__(self, *, reason: str, split_name: str, purpose: str) -> None:
        self.reason = reason
        self.split_name = split_name
        self.purpose = purpose
        super().__init__(reason)


def validate_split_usage(
    *,
    split_name: str,
    purpose: str,
    explicit_override: bool = False,
) -> tuple[dict[str, Any], ...]:
    split = str(split_name or "").strip()
    usage_purpose = str(purpose or "").strip()
    if usage_purpose == "feature_mining" and split == "final_holdout":
        if not explicit_override:
            raise SplitUsagePolicyError(
                reason=FINAL_HOLDOUT_DIAGNOSTIC_OVERRIDE_REQUIRED,
                split_name=split,
                purpose=usage_purpose,
            )
        return (
            {
                "reason": FINAL_HOLDOUT_DIAGNOSTIC_CONTAMINATION_RISK,
                "split_name": split,
            },
        )
    return ()
