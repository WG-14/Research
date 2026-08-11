from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from market_research.research.retention_policy import (
    ResearchRetentionClass,
    RetentionDecision,
    RetentionLifecycle,
    RetentionPolicyError,
    RetentionSubject,
    authorize_retention_deletion,
    evaluate_research_retention,
    standard_research_retention_policy,
    validate_retention_deletion_authorization,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _subject(
    *,
    retention_class: ResearchRetentionClass = ResearchRetentionClass.FAILED_RUN,
    lifecycle: RetentionLifecycle = RetentionLifecycle.ARCHIVED,
    active_reference_ids: tuple[str, ...] = (),
    legal_hold_ids: tuple[str, ...] = (),
) -> RetentionSubject:
    return RetentionSubject(
        subject_id="run-1",
        subject_version="1",
        project_id="project-1",
        retention_class=retention_class,
        lifecycle=lifecycle,
        terminal_at=(
            None if lifecycle is RetentionLifecycle.ACTIVE else "2025-01-01T00:00:00Z"
        ),
        artifact_hashes=(HASH_A, HASH_B),
        active_reference_ids=active_reference_ids,
        legal_hold_ids=legal_hold_ids,
    )


def test_standard_policy_covers_every_class_and_is_deterministic() -> None:
    first = standard_research_retention_policy()
    second = standard_research_retention_policy()

    assert first.content_hash == second.content_hash
    assert {rule.retention_class for rule in first.rules} == set(ResearchRetentionClass)
    assert first.legal_hold_enforcement
    assert first.two_person_deletion_approval
    with pytest.raises(FrozenInstanceError):
        first.version = "2"  # type: ignore[misc]


def test_official_release_and_audit_evidence_are_permanent() -> None:
    policy = standard_research_retention_policy()
    for retention_class in (
        ResearchRetentionClass.OFFICIAL_RELEASE,
        ResearchRetentionClass.AUDIT_EVIDENCE,
        ResearchRetentionClass.DATASET_INPUT,
    ):
        evaluation = evaluate_research_retention(
            subject=_subject(retention_class=retention_class),
            policy=policy,
            evaluated_at="2100-01-01T00:00:00Z",
        )
        assert evaluation.decision is RetentionDecision.KEEP_PERMANENT
        assert not evaluation.deletion_eligible


def test_legal_hold_and_active_lineage_block_age_eligible_evidence() -> None:
    policy = standard_research_retention_policy()
    held = evaluate_research_retention(
        subject=_subject(legal_hold_ids=("legal-hold-42",)),
        policy=policy,
        evaluated_at="2030-01-01T00:00:00Z",
    )
    referenced = evaluate_research_retention(
        subject=_subject(active_reference_ids=("official-package-7",)),
        policy=policy,
        evaluated_at="2030-01-01T00:00:00Z",
    )

    assert held.decision is RetentionDecision.BLOCKED_LEGAL_HOLD
    assert held.blocking_ids == ("legal-hold-42",)
    assert referenced.decision is RetentionDecision.BLOCKED_ACTIVE_REFERENCE
    assert referenced.blocking_ids == ("official-package-7",)


def test_failed_research_requires_archive_and_minimum_age() -> None:
    policy = standard_research_retention_policy()
    not_archived = evaluate_research_retention(
        subject=_subject(lifecycle=RetentionLifecycle.FAILED),
        policy=policy,
        evaluated_at="2030-01-01T00:00:00Z",
    )
    too_young = evaluate_research_retention(
        subject=_subject(),
        policy=policy,
        evaluated_at="2025-06-01T00:00:00Z",
    )
    eligible = evaluate_research_retention(
        subject=_subject(),
        policy=policy,
        evaluated_at="2026-01-02T00:00:00Z",
    )

    assert not_archived.decision is RetentionDecision.BLOCKED_LIFECYCLE
    assert too_young.decision is RetentionDecision.KEEP_MINIMUM_AGE
    assert too_young.eligible_at == "2026-01-01T00:00:00.000000Z"
    assert eligible.deletion_eligible


def _authorization():
    subject = _subject()
    policy = standard_research_retention_policy()
    evaluation = evaluate_research_retention(
        subject=subject,
        policy=policy,
        evaluated_at="2026-01-02T00:00:00Z",
    )
    authorization = authorize_retention_deletion(
        operation_id="retention-delete-1",
        subject=subject,
        evaluation=evaluation,
        policy=policy,
        requester_id="data-owner",
        requester_assertion_hash=HASH_B,
        reviewer_id="security-reviewer",
        reviewer_assertion_hash=HASH_C,
        reason_code="expired_failed_run",
        authorized_at="2026-01-02T00:01:00Z",
        expires_at="2026-01-02T00:31:00Z",
    )
    return subject, policy, evaluation, authorization


def test_two_person_authorization_is_exact_hash_bound_and_time_bounded() -> None:
    subject, policy, evaluation, authorization = _authorization()

    validate_retention_deletion_authorization(
        authorization=authorization,
        subject=subject,
        evaluation=evaluation,
        policy=policy,
        now="2026-01-02T00:15:00Z",
    )
    assert authorization.artifact_hashes == subject.artifact_hashes
    assert authorization.content_hash.startswith("sha256:")

    with pytest.raises(
        RetentionPolicyError,
        match="retention_authorization_not_current",
    ):
        validate_retention_deletion_authorization(
            authorization=authorization,
            subject=subject,
            evaluation=evaluation,
            policy=policy,
            now="2026-01-02T00:31:01Z",
        )


def test_authorization_rejects_same_actor_blocked_subject_and_changed_hashes() -> None:
    subject, policy, evaluation, authorization = _authorization()
    with pytest.raises(
        RetentionPolicyError,
        match="two_person_separation_required",
    ):
        authorize_retention_deletion(
            operation_id="retention-delete-2",
            subject=subject,
            evaluation=evaluation,
            policy=policy,
            requester_id="same-actor",
            requester_assertion_hash=HASH_B,
            reviewer_id="same-actor",
            reviewer_assertion_hash=HASH_C,
            reason_code="expired_failed_run",
            authorized_at="2026-01-02T00:01:00Z",
            expires_at="2026-01-02T00:31:00Z",
        )

    held_subject = _subject(legal_hold_ids=("legal-hold-42",))
    held_evaluation = evaluate_research_retention(
        subject=held_subject,
        policy=policy,
        evaluated_at="2030-01-01T00:00:00Z",
    )
    with pytest.raises(
        RetentionPolicyError,
        match="retention_authorization_subject_not_eligible",
    ):
        authorize_retention_deletion(
            operation_id="retention-delete-3",
            subject=held_subject,
            evaluation=held_evaluation,
            policy=policy,
            requester_id="data-owner",
            requester_assertion_hash=HASH_B,
            reviewer_id="security-reviewer",
            reviewer_assertion_hash=HASH_C,
            reason_code="expired_failed_run",
            authorized_at="2030-01-01T00:01:00Z",
            expires_at="2030-01-01T00:31:00Z",
        )

    changed_subject = replace(subject, artifact_hashes=(HASH_A, HASH_C))
    with pytest.raises(
        RetentionPolicyError,
        match="retention_authorization_binding_invalid",
    ):
        validate_retention_deletion_authorization(
            authorization=authorization,
            subject=changed_subject,
            evaluation=evaluation,
            policy=policy,
            now="2026-01-02T00:15:00Z",
        )


def test_authorization_window_cannot_exceed_policy() -> None:
    subject = _subject()
    policy = standard_research_retention_policy()
    evaluation = evaluate_research_retention(
        subject=subject,
        policy=policy,
        evaluated_at="2026-01-02T00:00:00Z",
    )
    with pytest.raises(
        RetentionPolicyError,
        match="retention_authorization_window_exceeds_policy",
    ):
        authorize_retention_deletion(
            operation_id="retention-delete-long-window",
            subject=subject,
            evaluation=evaluation,
            policy=policy,
            requester_id="data-owner",
            requester_assertion_hash=HASH_B,
            reviewer_id="security-reviewer",
            reviewer_assertion_hash=HASH_C,
            reason_code="expired_failed_run",
            authorized_at="2026-01-02T00:01:00Z",
            expires_at="2026-01-02T02:01:00Z",
        )
