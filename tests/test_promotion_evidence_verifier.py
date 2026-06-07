from __future__ import annotations

from pathlib import Path

from bithumb_bot.promotion_evidence_verifier import verify_promotion_candidate_execution_evidence


def _valid_evidence(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "compatibility_fallback": False,
        "research_compatibility_execution_fallback": False,
        "promotion_grade": True,
        "artifact_grade": "promotion_candidate",
        "authority_plane": "typed_execution_plan_bundle",
        "typed_execution_summary_present": True,
        "typed_submit_plan": True,
        "execution_plan_bundle_present": True,
        "recommended_next_action": "none",
    }
    payload.update(overrides)
    return payload


def test_promotion_verifier_accepts_typed_non_fallback_execution_evidence() -> None:
    result = verify_promotion_candidate_execution_evidence(_valid_evidence())

    assert result.accepted is True
    assert result.reason_codes == ()


def test_promotion_verifier_rejects_research_compatibility_fallback() -> None:
    result = verify_promotion_candidate_execution_evidence(
        _valid_evidence(
            compatibility_fallback=True,
            research_compatibility_execution_fallback=True,
            promotion_grade=False,
            recommended_next_action="none",
        )
    )

    assert result.accepted is False
    assert "promotion_evidence_compatibility_fallback" in result.reason_codes
    assert "promotion_evidence_research_compatibility_execution_fallback" in result.reason_codes
    assert "promotion_evidence_fallback_requires_regeneration_action" in result.reason_codes


def test_promotion_verifier_rejects_diagnostic_only_artifact_grade() -> None:
    result = verify_promotion_candidate_execution_evidence(
        _valid_evidence(
            artifact_grade="diagnostic_only",
            authority_plane="diagnostic_research_compatibility_only",
        )
    )

    assert "promotion_evidence_diagnostic_only_artifact_grade" in result.reason_codes
    assert "promotion_evidence_diagnostic_authority_plane" in result.reason_codes


def test_promotion_verifier_rejects_missing_typed_execution_summary() -> None:
    result = verify_promotion_candidate_execution_evidence(
        _valid_evidence(typed_execution_summary_present=False)
    )

    assert result.reason_codes == ("promotion_evidence_typed_execution_summary_missing",)


def test_promotion_verifier_rejects_missing_typed_submit_plan() -> None:
    result = verify_promotion_candidate_execution_evidence(
        _valid_evidence(typed_submit_plan=False)
    )

    assert result.reason_codes == ("promotion_evidence_typed_submit_plan_missing",)


def test_promotion_artifact_provenance_uses_single_execution_evidence_verifier() -> None:
    source = Path("src/bithumb_bot/promotion_provenance.py").read_text(encoding="utf-8")

    assert "verify_promotion_candidate_execution_evidence" in source
