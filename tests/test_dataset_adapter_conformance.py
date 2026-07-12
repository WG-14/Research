from __future__ import annotations
from market_research.research.datasets.verification import DatasetVerificationResult, VerificationStatus, verification_allowed


def test_verification_policy_is_status_based_and_fail_closed() -> None:
    result = DatasetVerificationResult(VerificationStatus.MISMATCH, VerificationStatus.MISMATCH, "sha256:"+"a"*64, "sha256:"+"b"*64, "scan", VerificationStatus.VERIFIED, "sha256:"+"a"*64, "sha256:"+"a"*64, VerificationStatus.VERIFIED, "content_addressed_local", VerificationStatus.VERIFIED, {}, {}, "adapter", "1")
    assert not verification_allowed(classification="research_only", result=result)
    assert not verification_allowed(classification="validated_candidate", result=result)
