"""Typed verification evidence shared by all dataset adapters."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Any


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    DECLARED_ONLY = "DECLARED_ONLY"
    DERIVED_FROM_SNAPSHOT = "DERIVED_FROM_SNAPSHOT"
    UNAVAILABLE = "UNAVAILABLE"
    MISMATCH = "MISMATCH"


@dataclass(frozen=True)
class DatasetVerificationResult:
    overall_status: VerificationStatus
    content_status: VerificationStatus
    expected_content_hash: str | None
    actual_content_hash: str | None
    content_method: str
    schema_status: VerificationStatus
    expected_schema_hash: str | None
    actual_schema_hash: str | None
    locator_status: VerificationStatus
    locator_type: str | None
    scope_status: VerificationStatus
    declared_scope: dict[str, Any] | None
    actual_scope: dict[str, Any] | None
    adapter_name: str
    adapter_version: str

    def __post_init__(self) -> None:
        statuses = (self.overall_status, self.content_status, self.schema_status,
                    self.locator_status, self.scope_status)
        if not all(isinstance(value, VerificationStatus) for value in statuses):
            raise ValueError("dataset_verification_unknown_status")
        for status, expected, actual, label in (
            (self.content_status, self.expected_content_hash, self.actual_content_hash, "content"),
            (self.schema_status, self.expected_schema_hash, self.actual_schema_hash, "schema"),
        ):
            if status in {VerificationStatus.VERIFIED, VerificationStatus.MISMATCH}:
                if not expected or not actual:
                    raise ValueError(f"dataset_verification_{label}_hashes_required")
                if not self.content_method:
                    raise ValueError("dataset_verification_method_required")
                if status is VerificationStatus.VERIFIED and expected != actual:
                    raise ValueError(f"dataset_verification_{label}_verified_hash_mismatch")
                if status is VerificationStatus.MISMATCH and expected == actual:
                    raise ValueError(f"dataset_verification_{label}_mismatch_hash_equal")
        components = (self.content_status, self.schema_status, self.locator_status, self.scope_status)
        if any(value is VerificationStatus.MISMATCH for value in components) and self.overall_status is not VerificationStatus.MISMATCH:
            raise ValueError("dataset_verification_component_mismatch_requires_overall_mismatch")
        if self.overall_status is VerificationStatus.VERIFIED and any(
            value is not VerificationStatus.VERIFIED for value in components
        ):
            raise ValueError("dataset_verification_verified_components_required")
        if self.overall_status is VerificationStatus.MISMATCH and not any(
            value is VerificationStatus.MISMATCH for value in components
        ):
            raise ValueError("dataset_verification_mismatch_component_required")

    def as_dict(self) -> dict[str, Any]:
        return {key: (value.value if isinstance(value, VerificationStatus) else value)
                for key, value in self.__dict__.items()}

    def require_verified(self) -> None:
        if self.overall_status is not VerificationStatus.VERIFIED:
            raise ValueError(f"dataset_verification_not_verified:{self.overall_status.value}")


def verification_allowed(*, classification: str, result: DatasetVerificationResult) -> bool:
    if result.overall_status is VerificationStatus.MISMATCH:
        return False
    # Chosen policy A: validated candidates require complete source evidence.
    if classification == "validated_candidate":
        return result.overall_status is VerificationStatus.VERIFIED
    return result.overall_status in {VerificationStatus.VERIFIED, VerificationStatus.DECLARED_ONLY,
                                     VerificationStatus.DERIVED_FROM_SNAPSHOT, VerificationStatus.UNAVAILABLE}
