from __future__ import annotations
from market_research.research.datasets.verification import DatasetVerificationResult, VerificationStatus, verification_allowed
import pytest


def test_verification_policy_is_status_based_and_fail_closed() -> None:
    result = DatasetVerificationResult(VerificationStatus.MISMATCH, VerificationStatus.MISMATCH, "sha256:"+"a"*64, "sha256:"+"b"*64, "scan", VerificationStatus.VERIFIED, "sha256:"+"a"*64, "sha256:"+"a"*64, VerificationStatus.VERIFIED, "content_addressed_local", VerificationStatus.VERIFIED, {}, {}, "adapter", "1")
    assert not verification_allowed(classification="research_only", result=result)
    assert not verification_allowed(classification="validated_candidate", result=result)


def test_verified_requires_all_verified_components() -> None:
    with pytest.raises(ValueError, match="components"):
        DatasetVerificationResult(VerificationStatus.VERIFIED, VerificationStatus.VERIFIED, "sha256:"+"a"*64, "sha256:"+"a"*64, "scan", VerificationStatus.VERIFIED, "sha256:"+"a"*64, "sha256:"+"a"*64, VerificationStatus.UNAVAILABLE, "local", VerificationStatus.VERIFIED, {}, {}, "adapter", "1")


def test_any_component_mismatch_requires_overall_mismatch() -> None:
    with pytest.raises(ValueError, match="component_mismatch"):
        DatasetVerificationResult(VerificationStatus.DECLARED_ONLY, VerificationStatus.MISMATCH, "sha256:"+"a"*64, "sha256:"+"b"*64, "scan", VerificationStatus.DECLARED_ONLY, None, None, VerificationStatus.UNAVAILABLE, None, VerificationStatus.DERIVED_FROM_SNAPSHOT, None, None, "adapter", "1")


@pytest.mark.parametrize(
    ("status", "expected", "actual", "message"),
    (
        (VerificationStatus.VERIFIED, "sha256:"+"a"*64, "sha256:"+"b"*64, "verified_hash_mismatch"),
        (VerificationStatus.MISMATCH, "sha256:"+"a"*64, "sha256:"+"a"*64, "mismatch_hash_equal"),
    ),
)
def test_verification_hash_relationships_are_constructor_enforced(status, expected, actual, message) -> None:
    with pytest.raises(ValueError, match=message):
        DatasetVerificationResult(VerificationStatus.MISMATCH, status, expected, actual, "scan", VerificationStatus.VERIFIED, "sha256:"+"a"*64, "sha256:"+"a"*64, VerificationStatus.VERIFIED, "local", VerificationStatus.VERIFIED, {}, {}, "adapter", "1")
