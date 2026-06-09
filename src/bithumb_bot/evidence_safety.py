from __future__ import annotations

from typing import Any, Mapping


SMOKE_ONLY_EVIDENCE_SCOPE = "smoke_only_not_manifest_backed"
SMOKE_EVIDENCE_OPERATOR_NEXT_ACTION = "use_manifest_backed_research_validation"
DIAGNOSTIC_FEATURE_MINING_SCOPE = "diagnostic_feature_mining"
DIAGNOSTIC_FEATURE_MINING_OPERATOR_NEXT_ACTION = "run_research_validate_from_fixed_manifest"
DIAGNOSTIC_FEATURE_MINING_FORBIDDEN_USES = (
    "strategy_promotion",
    "approved_profile",
    "live_readiness",
    "capital_allocation",
)


def diagnostic_feature_mining_taxonomy(
    *,
    operator_next_action: str = DIAGNOSTIC_FEATURE_MINING_OPERATOR_NEXT_ACTION,
) -> dict[str, object]:
    return {
        "evidence_scope": DIAGNOSTIC_FEATURE_MINING_SCOPE,
        "promotion_eligible": False,
        "promotion_grade": False,
        "non_promotable": True,
        "forbidden_uses": list(DIAGNOSTIC_FEATURE_MINING_FORBIDDEN_USES),
        "operator_next_action": operator_next_action,
    }


def smoke_only_evidence_rejection_reasons(payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    reasons: list[str] = []
    if payload.get("evidence_scope") == SMOKE_ONLY_EVIDENCE_SCOPE:
        reasons.append("smoke_backtest_artifact_not_promotable")
        reasons.append("standalone_backtest_not_full_validation")
    if payload.get("evidence_scope") == DIAGNOSTIC_FEATURE_MINING_SCOPE:
        reasons.append("diagnostic_feature_mining_not_promotable")
    if payload.get("standalone_backtest_not_full_validation") is True:
        reasons.append("standalone_backtest_not_full_validation")
    if payload.get("non_promotable") is True:
        reasons.append("non_promotable_evidence_artifact")
    if payload.get("promotion_eligible") is False:
        reasons.append("promotion_eligible_false")
    if payload.get("promotion_grade") is False:
        reasons.append("promotion_grade_validation_required")
    if payload.get("diagnostic_only") is True:
        reasons.append("diagnostic_only_evidence_artifact")
    return tuple(sorted(set(reasons)))


def evidence_rejection_reasons(payload: Mapping[str, Any] | None) -> tuple[str, ...]:
    reasons = list(smoke_only_evidence_rejection_reasons(payload))
    try:
        from bithumb_bot.research.artifact_contract import diagnostic_artifact_rejection_reasons
    except ImportError:
        diagnostic_reasons: tuple[str, ...] = ()
    else:
        diagnostic_reasons = diagnostic_artifact_rejection_reasons(payload)
    reasons.extend(diagnostic_reasons)
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
