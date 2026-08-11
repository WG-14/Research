from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.governance import (
    GovernanceSubject,
    GovernanceSubjectType,
    append_lifecycle_transition,
    validate_governance_registry,
)
from market_research.research.governance_policy import (
    GovernancePolicyError,
    GovernancePolicyId,
    GovernanceRole,
    require_current_policy_reference,
    standard_research_governance_policy,
)
from market_research.settings import ResearchSettings


def _manager(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=None,
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def test_standard_policy_is_complete_hash_bound_and_installed() -> None:
    policy = standard_research_governance_policy()

    assert {item.policy_id for item in policy.policies} == set(GovernancePolicyId)
    assert {item.role for item in policy.roles} == set(GovernanceRole)
    assert policy.content_hash.startswith("sha256:")
    assert policy == standard_research_governance_policy()
    policy.validate_installed_enforcement()


def test_policy_inventory_and_reference_fail_closed() -> None:
    policy = standard_research_governance_policy()

    with pytest.raises(
        GovernancePolicyError,
        match="governance_policy_inventory_incomplete",
    ):
        replace(policy, policies=policy.policies[:-1])

    drifted_reference = {
        **policy.reference(),
        "content_hash": "sha256:" + "f" * 64,
    }
    with pytest.raises(
        GovernancePolicyError,
        match="governance_policy_reference_mismatch",
    ):
        require_current_policy_reference(drifted_reference)


def test_policy_effective_date_and_role_separation_fail_closed() -> None:
    policy = standard_research_governance_policy()

    with pytest.raises(
        GovernancePolicyError,
        match="governance_policy_schema_unsupported",
    ):
        replace(policy, schema_version=True)

    with pytest.raises(
        GovernancePolicyError,
        match="governance_policy_set_version_invalid",
    ):
        replace(policy, version=1.5)

    first_definition = policy.policies[0]
    with pytest.raises(
        GovernancePolicyError,
        match="governance_policy_version_invalid",
    ):
        replace(first_definition, version=True)

    with pytest.raises(
        GovernancePolicyError,
        match="governance_policy_effective_from_invalid",
    ):
        replace(policy, effective_from="09-08-2026")

    roles = list(policy.roles)
    researcher_index = next(
        index
        for index, responsibility in enumerate(roles)
        if responsibility.role is GovernanceRole.RESEARCHER
    )
    roles[researcher_index] = replace(
        roles[researcher_index],
        prohibited_combined_roles=(GovernanceRole.RESEARCH_REVIEWER,),
    )
    with pytest.raises(
        GovernancePolicyError,
        match="governance_role_separation_must_be_symmetric",
    ):
        replace(policy, roles=tuple(roles))


def test_new_lifecycle_events_bind_the_current_policy(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    row = append_lifecycle_transition(
        manager=manager,
        subject=GovernanceSubject(
            GovernanceSubjectType.HYPOTHESIS,
            "policy-bound-hypothesis",
            "1",
        ),
        from_state=None,
        to_state="IDEA",
        actor_id="researcher-a",
        reason="Register under the current policy authority.",
        evidence_hashes={
            "hypothesis_semantic_fingerprint": "sha256:" + "a" * 64,
        },
        recorded_at="2026-08-09T00:00:00+00:00",
    )

    assert row["governance_policy"] == (
        standard_research_governance_policy().reference()
    )
    assert validate_governance_registry(manager)["status"] == "PASS"
