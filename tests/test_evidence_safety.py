from __future__ import annotations

from bithumb_bot.evidence_safety import evidence_rejection_reasons, smoke_only_evidence_rejection_reasons


def test_diagnostic_only_payload_is_rejected_as_promotion_evidence() -> None:
    reasons = smoke_only_evidence_rejection_reasons({"diagnostic_only": True})

    assert "diagnostic_only_evidence_artifact" in reasons


def test_diagnostic_feature_mining_scope_is_rejected() -> None:
    reasons = smoke_only_evidence_rejection_reasons(
        {
            "diagnostic_only": True,
            "evidence_scope": "diagnostic_feature_mining",
            "non_promotable": True,
        }
    )

    assert "diagnostic_feature_mining_not_promotable" in reasons
    assert "diagnostic_only_evidence_artifact" in reasons


def test_non_promotable_payload_is_rejected() -> None:
    reasons = smoke_only_evidence_rejection_reasons({"non_promotable": True})

    assert "non_promotable_evidence_artifact" in reasons


def test_diagnostic_feature_mining_artifact_rejected_as_promotion_evidence() -> None:
    reasons = evidence_rejection_reasons(
        {
            "artifact_type": "forward_return_diagnostic_report",
            "diagnostic_only": True,
            "promotion_evidence": False,
            "approved_profile_evidence": False,
            "live_readiness_evidence": False,
            "capital_allocation_evidence": False,
            "evidence_scope": "diagnostic_feature_mining",
            "promotion_eligible": False,
            "promotion_grade": False,
            "non_promotable": True,
            "forbidden_uses": [
                "strategy_promotion",
                "approved_profile",
                "live_readiness",
                "capital_allocation",
            ],
            "operator_next_action": "run_research_validate_from_fixed_manifest",
        }
    )

    assert "diagnostic_feature_mining_not_promotable" in reasons
    assert "forbidden_use:strategy_promotion" in reasons
