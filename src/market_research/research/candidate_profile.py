"""Hashable research-candidate descriptions without declaration authority."""

from __future__ import annotations

from typing import Any

from .hashing import logical_evidence_hash_payload


def build_candidate_profile(candidate: dict[str, Any]) -> dict[str, Any]:
    """Build the path- and runtime-invariant logical candidate profile."""

    profile = logical_evidence_hash_payload(candidate)
    if not isinstance(profile, dict):  # pragma: no cover - input contract guard
        raise TypeError("candidate_profile_must_be_object")
    return profile


def build_candidate_behavior_profile(
    candidate: dict[str, Any], *, base_profile: dict[str, Any] | None = None
) -> dict[str, Any]:
    profile = dict(base_profile or build_candidate_profile(candidate))
    return {
        key: profile.get(key)
        for key in (
            "strategy_name",
            "strategy_spec_hash",
            "effective_strategy_parameters_hash",
            "dataset_content_hash",
            "execution_contract_hash",
            "behavior_hash",
            "composite_behavior_hash",
        )
        if profile.get(key) is not None
    }
