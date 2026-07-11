from __future__ import annotations

from typing import Mapping


SMOKE_ONLY_EVIDENCE_SCOPE = "smoke_only_not_manifest_backed"
SMOKE_EVIDENCE_RESEARCHER_NEXT_ACTION = "use_manifest_backed_research_validation"
DIAGNOSTIC_FEATURE_MINING_SCOPE = "diagnostic_feature_mining"
DIAGNOSTIC_FEATURE_MINING_RESEARCHER_NEXT_ACTION = "run_research_validate_from_fixed_manifest"
DIAGNOSTIC_FEATURE_MINING_FORBIDDEN_USES = (
    "final_candidate_selection",
    "validation_pass_claim",
)


def diagnostic_feature_mining_taxonomy(
    *,
    researcher_next_action: str = DIAGNOSTIC_FEATURE_MINING_RESEARCHER_NEXT_ACTION,
) -> dict[str, object]:
    return {
        "artifact_role": "diagnostic",
        "diagnostic_only": True,
        "validation_evidence": False,
        "candidate_selection_eligible": False,
        "evidence_scope": DIAGNOSTIC_FEATURE_MINING_SCOPE,
        "forbidden_uses": list(DIAGNOSTIC_FEATURE_MINING_FORBIDDEN_USES),
        "researcher_next_action": researcher_next_action,
    }


def smoke_only_evidence_rejection_reasons(payload: Mapping[str, object] | None) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    reasons: list[str] = []
    if payload.get("evidence_scope") == SMOKE_ONLY_EVIDENCE_SCOPE:
        reasons.append("standalone_backtest_not_full_validation")
    if payload.get("evidence_scope") == DIAGNOSTIC_FEATURE_MINING_SCOPE:
        reasons.extend(
            (
                "diagnostic_artifact_not_validation_evidence",
                "diagnostic_artifact_not_candidate_selection_evidence",
            )
        )
    if payload.get("diagnostic_only") is True:
        reasons.append("diagnostic_artifact_not_validation_evidence")
    return tuple(sorted(set(reasons)))


def evidence_rejection_reasons(payload: Mapping[str, object] | None) -> tuple[str, ...]:
    reasons = list(smoke_only_evidence_rejection_reasons(payload))
    from .artifact_contract import diagnostic_artifact_rejection_reasons

    reasons.extend(diagnostic_artifact_rejection_reasons(payload))
    return tuple(sorted(set(reasons)))


def smoke_only_evidence_rejection_context(payload: Mapping[str, object] | None) -> dict[str, object]:
    reasons = smoke_only_evidence_rejection_reasons(payload)
    return {
        "accepted_for_validation": not reasons,
        "accepted_for_candidate_selection": not reasons,
        "reason_codes": list(reasons),
        "researcher_next_action": SMOKE_EVIDENCE_RESEARCHER_NEXT_ACTION if reasons else "none",
        "recommended_command": (
            "uv run bithumb-research research-validate --manifest <path>" if reasons else "none"
        ),
    }
