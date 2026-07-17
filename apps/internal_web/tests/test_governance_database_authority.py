from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction

from market_research.application import GovernanceSubjectRef
from market_research.research.governance import GovernanceError
from portal.governance import (
    approve_job_candidate,
    load_review_context,
    record_job_review,
)
from portal.models import (
    GovernanceDecision,
    GovernanceDutyClaim,
    GovernanceSubjectState,
    ResearchJob,
    WebAuditEvent,
)


pytestmark = pytest.mark.django_db


@pytest.fixture
def approver_user(db):
    user = get_user_model().objects.create_user(
        username=f"authority-approver-{uuid.uuid4().hex}",
        password="test-password",
    )
    user.groups.add(Group.objects.get(name="research_approver"))
    return user


@pytest.fixture
def passed_job(runner_user, manifest_record) -> ResearchJob:
    return ResearchJob.objects.create(
        owner=runner_user,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.VALIDATE,
        status=ResearchJob.Status.SUCCEEDED,
        request_payload={"fixture": "governance-db-authority"},
        request_hash="sha256:" + "3" * 64,
        idempotency_key=str(uuid.uuid4()),
        actor_id=str(runner_user.pk),
        actor_roles=["research_runner"],
        actor_permissions=["research.execute", "research.view"],
        run_id=f"run-{uuid.uuid4().hex}",
        result_ref="report:_internal_web/governance/result.json",
        result_hash="sha256:" + "4" * 64,
        research_outcome=ResearchJob.ResearchOutcome.PASS,
    )


def _subject() -> GovernanceSubjectRef:
    return GovernanceSubjectRef(
        subject_type="strategy_candidate",
        subject_id="candidate-db-authority",
        subject_version="1",
    )


def _context(*, state: str = "OUT_OF_SAMPLE_PASSED") -> dict[str, object]:
    return {
        "report": {},
        "subject": _subject(),
        "prior_reviews": (),
        "candidate_state": state,
        "approval_ready": state == "OUT_OF_SAMPLE_PASSED",
    }


def _install_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "portal.governance.load_review_context",
        lambda _job: _context(),
    )


def test_review_state_and_outbox_intent_commit_in_one_database_transaction(
    reviewer_user,
    passed_job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_context(monkeypatch)
    row_hash = "sha256:" + "5" * 64
    monkeypatch.setattr(
        "portal.governance.ResearchGovernanceApplicationService.record_review",
        lambda _service, request: SimpleNamespace(
            decision=request.decision,
            reviewer_role="research_reviewer",
            row_hash=row_hash,
            subject=request.subject,
        ),
    )

    result = record_job_review(
        user=reviewer_user,
        job=passed_job,
        cleaned_data={
            "decision": "REJECTED",
            "rationale": "evidence does not meet the reviewed contract",
        },
        correlation_id=str(uuid.uuid4()),
    )

    state = GovernanceSubjectState.objects.get()
    decision = GovernanceDecision.objects.get()
    audit = WebAuditEvent.objects.get()
    assert result == {
        "decision": "REJECTED",
        "row_hash": row_hash,
        "subject": _subject(),
    }
    assert state.lifecycle_state == "OUT_OF_SAMPLE_PASSED"
    assert decision.subject == state
    assert decision.action == GovernanceDecision.Action.REVIEW
    assert decision.review_row_hash == row_hash
    assert set(GovernanceDutyClaim.objects.values_list("duty", flat=True)) == {
        "ORIGINATOR",
        "REVIEWER",
    }
    assert audit.payload["object_type"] == "governance_decision"
    assert audit.payload["object_id"] == str(decision.pk)
    assert audit.projected_at is None


def test_audit_intent_failure_rolls_back_authoritative_review_state(
    reviewer_user,
    passed_job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_context(monkeypatch)
    monkeypatch.setattr(
        "portal.governance.ResearchGovernanceApplicationService.record_review",
        lambda _service, request: SimpleNamespace(
            decision=request.decision,
            reviewer_role="research_reviewer",
            row_hash="sha256:" + "5" * 64,
            subject=request.subject,
        ),
    )
    monkeypatch.setattr(
        "portal.governance.record_web_audit_event",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        record_job_review(
            user=reviewer_user,
            job=passed_job,
            cleaned_data={
                "decision": "REJECTED",
                "rationale": "this transaction must be rolled back",
            },
            correlation_id=str(uuid.uuid4()),
        )

    assert not GovernanceSubjectState.objects.exists()
    assert not GovernanceDutyClaim.objects.exists()
    assert not GovernanceDecision.objects.exists()
    assert not WebAuditEvent.objects.exists()


def test_database_unique_claim_enforces_separation_of_duties(
    passed_job,
) -> None:
    state = GovernanceSubjectState.objects.create(
        source_job=passed_job,
        subject_type="strategy_candidate",
        subject_id="direct-constraint-candidate",
        subject_version="1",
        reviewed_artifact_hash=passed_job.result_hash,
    )
    actor_id = str(passed_job.owner_id)
    GovernanceDutyClaim.objects.create(
        subject=state,
        actor_id=actor_id,
        duty=GovernanceDutyClaim.Duty.ORIGINATOR,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        GovernanceDutyClaim.objects.create(
            subject=state,
            actor_id=actor_id,
            duty=GovernanceDutyClaim.Duty.APPROVER,
        )

    assert list(GovernanceDutyClaim.objects.values_list("duty", flat=True)) == [
        "ORIGINATOR"
    ]


def test_review_context_uses_database_decisions_after_authority_is_established(
    passed_job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = _subject()
    state = GovernanceSubjectState.objects.create(
        source_job=passed_job,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        subject_version=subject.subject_version,
        reviewed_artifact_hash=passed_job.result_hash,
    )
    GovernanceDecision.objects.create(
        subject=state,
        operation_id=str(uuid.uuid4()),
        operation_payload_hash="sha256:" + "a" * 64,
        action=GovernanceDecision.Action.REVIEW,
        decision=GovernanceDecision.Decision.REJECTED,
        actor_id="database-reviewer",
        actor_role="research_reviewer",
        rationale="database authority",
        reviewed_artifact_hash=passed_job.result_hash,
        content_hash="sha256:" + "b" * 64,
        review_row_hash="sha256:" + "b" * 64,
    )
    monkeypatch.setattr(
        "portal.governance._verified_pass_report",
        lambda _job: {},
    )
    monkeypatch.setattr(
        "portal.governance._approval_ready_subject",
        lambda _report, reviewed_artifact_hash: (
            subject,
            [
                {
                    "event_type": "human_review_decision",
                    "decision": "REJECTED",
                    "reviewer_id": "filesystem-only-reviewer",
                    "subject_type": subject.subject_type,
                    "subject_id": subject.subject_id,
                    "subject_version": subject.subject_version,
                    "reviewed_artifact_hash": reviewed_artifact_hash,
                }
            ],
            "OUT_OF_SAMPLE_PASSED",
        ),
    )

    context = load_review_context(passed_job)

    assert context["candidate_state"] == "OUT_OF_SAMPLE_PASSED"
    assert [row["reviewer_id"] for row in context["prior_reviews"]] == [
        "database-reviewer"
    ]


def test_approval_is_atomic_idempotent_and_compare_and_swap_guarded(
    approver_user,
    passed_job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_context(monkeypatch)
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "portal.governance.resolve_artifact_ref",
        lambda _ref: source,
    )
    calls: list[object] = []

    def approve(_service, request):
        calls.append(request)
        return SimpleNamespace(
            content_hash="sha256:" + "6" * 64,
            review_row_hash="sha256:" + "7" * 64,
            transition_row_hash="sha256:" + "8" * 64,
            subject=request.actor and _subject(),
        )

    monkeypatch.setattr(
        "portal.governance.ResearchGovernanceApplicationService.approve_candidate",
        approve,
    )
    request_id = str(uuid.uuid4())
    payload = {
        "approval_request_id": request_id,
        "rationale": "independent approval evidence is complete",
        "resolved_requirement_ids": (),
    }

    first = approve_job_candidate(
        user=approver_user,
        job=passed_job,
        cleaned_data=payload,
        correlation_id=str(uuid.uuid4()),
    )
    second = approve_job_candidate(
        user=approver_user,
        job=passed_job,
        cleaned_data=payload,
        correlation_id=str(uuid.uuid4()),
    )

    state = GovernanceSubjectState.objects.get()
    decision = GovernanceDecision.objects.get()
    assert first == second
    assert len(calls) == 1
    assert state.lifecycle_state == "RESEARCH_APPROVED"
    assert state.lifecycle_version == 1
    assert state.approved_by == str(approver_user.pk)
    assert state.approved_at is not None
    assert decision.action == GovernanceDecision.Action.APPROVAL
    assert decision.review_row_hash == "sha256:" + "7" * 64
    assert decision.transition_row_hash == "sha256:" + "8" * 64
    assert WebAuditEvent.objects.count() == 1

    with pytest.raises(
        GovernanceError,
        match="governance_idempotency_payload_conflict",
    ):
        approve_job_candidate(
            user=approver_user,
            job=passed_job,
            cleaned_data={
                **payload,
                "rationale": "same key with a different payload must fail",
            },
            correlation_id=str(uuid.uuid4()),
        )

    with pytest.raises(GovernanceError, match="strategy_candidate_already_approved"):
        approve_job_candidate(
            user=approver_user,
            job=passed_job,
            cleaned_data={
                **payload,
                "approval_request_id": str(uuid.uuid4()),
            },
            correlation_id=str(uuid.uuid4()),
        )
    assert GovernanceDecision.objects.count() == 1
    assert WebAuditEvent.objects.count() == 1


def test_approval_and_lifecycle_transition_roll_back_when_outbox_write_fails(
    approver_user,
    passed_job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_context(monkeypatch)
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "portal.governance.resolve_artifact_ref",
        lambda _ref: source,
    )
    monkeypatch.setattr(
        "portal.governance.ResearchGovernanceApplicationService.approve_candidate",
        lambda _service, request: SimpleNamespace(
            content_hash="sha256:" + "6" * 64,
            review_row_hash="sha256:" + "7" * 64,
            transition_row_hash="sha256:" + "8" * 64,
            subject=request.actor and _subject(),
        ),
    )
    monkeypatch.setattr(
        "portal.governance.record_web_audit_event",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )

    with pytest.raises(RuntimeError, match="audit unavailable"):
        approve_job_candidate(
            user=approver_user,
            job=passed_job,
            cleaned_data={
                "approval_request_id": str(uuid.uuid4()),
                "rationale": "approval must share the outbox transaction",
                "resolved_requirement_ids": (),
            },
            correlation_id=str(uuid.uuid4()),
        )

    assert not GovernanceSubjectState.objects.exists()
    assert not GovernanceDutyClaim.objects.exists()
    assert not GovernanceDecision.objects.exists()
    assert not WebAuditEvent.objects.exists()


def test_exact_retry_reconciles_file_approval_that_preceded_database_commit(
    approver_user,
    passed_job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "portal.governance.load_review_context",
        lambda _job: _context(state="RESEARCH_APPROVED"),
    )
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "portal.governance.resolve_artifact_ref",
        lambda _ref: source,
    )
    monkeypatch.setattr(
        "portal.governance.ResearchGovernanceApplicationService.approve_candidate",
        lambda _service, request: SimpleNamespace(
            content_hash="sha256:" + "6" * 64,
            review_row_hash="sha256:" + "7" * 64,
            transition_row_hash="sha256:" + "8" * 64,
            subject=request.actor and _subject(),
        ),
    )

    result = approve_job_candidate(
        user=approver_user,
        job=passed_job,
        cleaned_data={
            "approval_request_id": str(uuid.uuid4()),
            "rationale": "reconcile the exact filesystem publication retry",
            "resolved_requirement_ids": (),
        },
        correlation_id=str(uuid.uuid4()),
    )

    assert result["approval_hash"] == "sha256:" + "6" * 64
    state = GovernanceSubjectState.objects.get()
    assert state.lifecycle_state == "RESEARCH_APPROVED"
    assert state.lifecycle_version == 1
    assert GovernanceDecision.objects.filter(action="APPROVAL").count() == 1
    assert WebAuditEvent.objects.count() == 1


def test_database_reviewer_claim_blocks_later_approval_even_if_file_context_omits_it(
    reviewer_user,
    passed_job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_context(monkeypatch)
    reviewer_user.groups.add(Group.objects.get(name="research_approver"))
    for attribute in ("_perm_cache", "_group_perm_cache", "_user_perm_cache"):
        vars(reviewer_user).pop(attribute, None)
    monkeypatch.setattr(
        "portal.governance.ResearchGovernanceApplicationService.record_review",
        lambda _service, request: SimpleNamespace(
            decision=request.decision,
            reviewer_role="research_reviewer",
            row_hash="sha256:" + "9" * 64,
            subject=request.subject,
        ),
    )
    record_job_review(
        user=reviewer_user,
        job=passed_job,
        cleaned_data={
            "decision": "REJECTED",
            "rationale": "review duty is claimed in the database",
        },
        correlation_id=str(uuid.uuid4()),
    )
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "portal.governance.resolve_artifact_ref",
        lambda _ref: source,
    )

    with pytest.raises(
        GovernanceError,
        match="governance_separation_of_duties_violation",
    ):
        approve_job_candidate(
            user=reviewer_user,
            job=passed_job,
            cleaned_data={
                "approval_request_id": str(uuid.uuid4()),
                "rationale": "reviewer must not become approver",
                "resolved_requirement_ids": (),
            },
            correlation_id=str(uuid.uuid4()),
        )

    assert GovernanceDecision.objects.filter(action="APPROVAL").count() == 0
