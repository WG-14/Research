from __future__ import annotations

from bithumb_bot.evidence_safety import smoke_only_evidence_rejection_reasons


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
