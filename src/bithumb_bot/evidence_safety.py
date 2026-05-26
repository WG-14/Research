from __future__ import annotations

from typing import Any, Mapping


SMOKE_ONLY_EVIDENCE_SCOPE = "smoke_only_not_manifest_backed"
SMOKE_EVIDENCE_OPERATOR_NEXT_ACTION = "use_manifest_backed_research_validation"


def smoke_only_evidence_rejection_reasons(payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    reasons: list[str] = []
    if payload.get("evidence_scope") == SMOKE_ONLY_EVIDENCE_SCOPE:
        reasons.append("smoke_backtest_artifact_not_promotable")
        reasons.append("standalone_backtest_not_full_validation")
    if payload.get("standalone_backtest_not_full_validation") is True:
        reasons.append("standalone_backtest_not_full_validation")
    if payload.get("non_promotable") is True:
        reasons.append("non_promotable_evidence_artifact")
    if payload.get("promotion_grade") is False:
        reasons.append("promotion_grade_validation_required")
    if payload.get("diagnostic_only") is True and payload.get("non_promotable") is True:
        reasons.append("diagnostic_only_evidence_artifact")
    return tuple(sorted(set(reasons)))


def smoke_only_evidence_rejection_context(payload: Mapping[str, Any] | None) -> dict[str, object]:
    reasons = smoke_only_evidence_rejection_reasons(payload)
    return {
        "accepted": not reasons,
        "reason_codes": list(reasons),
        "operator_next_action": SMOKE_EVIDENCE_OPERATOR_NEXT_ACTION if reasons else "none",
        "recommended_command": (
            "uv run bithumb-bot research-validate --manifest <path>" if reasons else "none"
        ),
    }
