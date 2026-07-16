from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.urls import reverse

from market_research.application import GovernanceSubjectRef
from market_research.research.governance import GovernanceError
from portal.forms import CandidateApprovalForm, HumanReviewForm
from portal.governance import (
    _approval_ready_subject,
    approve_job_candidate,
    record_job_review,
)
from portal.models import ResearchJob


pytestmark = pytest.mark.django_db


@pytest.fixture
def approver_user(db):
    user = get_user_model().objects.create_user(
        username=f"approver-{uuid.uuid4().hex}",
        password="test-password",
    )
    user.groups.add(Group.objects.get(name="research_approver"))
    return user


@pytest.fixture
def passed_validation_job(runner_user, manifest_record) -> ResearchJob:
    return ResearchJob.objects.create(
        owner=runner_user,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.VALIDATE,
        status=ResearchJob.Status.SUCCEEDED,
        request_payload={"fixture": "review-workflow"},
        request_hash="sha256:" + "1" * 64,
        idempotency_key=str(uuid.uuid4()),
        actor_id=str(runner_user.pk),
        actor_roles=["research_runner"],
        actor_permissions=["research.execute", "research.view"],
        run_id=f"run-{uuid.uuid4().hex}",
        result_ref="report:_internal_web/review-workflow/result.json",
        result_hash="sha256:" + "2" * 64,
        research_outcome=ResearchJob.ResearchOutcome.PASS,
    )


def _context(*, prior_reviews: tuple[dict[str, object], ...] = ()) -> dict[str, object]:
    return {
        "report": {},
        "subject": GovernanceSubjectRef(
            subject_type="strategy_candidate",
            subject_id="candidate-1",
            subject_version="1",
        ),
        "prior_reviews": prior_reviews,
    }


def _approval_payload(
    *,
    password: str = "test-password",
    approval_request_id: str | None = None,
) -> dict[str, object]:
    return {
        "approval_request_id": approval_request_id or str(uuid.uuid4()),
        "rationale": "independent evidence review passed",
        "resolved_requirement_ids": "",
        "password": password,
        "confirm": "on",
    }


def _clear_permission_cache(user: object) -> None:
    for attribute in ("_perm_cache", "_group_perm_cache", "_user_perm_cache"):
        vars(user).pop(attribute, None)


def test_review_routes_deny_users_without_review_or_approval_permission(
    client,
    runner_user,
    passed_validation_job,
) -> None:
    client.force_login(runner_user)

    assert client.get(reverse("portal:review-queue")).status_code == 403
    assert client.post(
        reverse("portal:review-record", args=(passed_validation_job.pk,)),
        {"decision": "REJECTED", "rationale": "not supported"},
    ).status_code == 403
    assert client.post(
        reverse("portal:review-approve", args=(passed_validation_job.pk,)),
        _approval_payload(),
    ).status_code == 403


def test_reviewer_and_approver_endpoints_remain_separate(
    client,
    reviewer_user,
    approver_user,
    passed_validation_job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = Mock(return_value={"row_hash": "sha256:" + "3" * 64})
    approve = Mock(return_value={"approval_hash": "sha256:" + "4" * 64})
    monkeypatch.setattr("portal.views.record_job_review", record)
    monkeypatch.setattr("portal.views.approve_job_candidate", approve)

    client.force_login(reviewer_user)
    response = client.post(
        reverse("portal:review-record", args=(passed_validation_job.pk,)),
        {"decision": "REJECTED", "rationale": "evidence is insufficient"},
    )
    assert response.status_code == 302
    record.assert_called_once()
    assert client.post(
        reverse("portal:review-approve", args=(passed_validation_job.pk,)),
        _approval_payload(),
    ).status_code == 403
    approve.assert_not_called()

    client.force_login(approver_user)
    assert client.post(
        reverse("portal:review-record", args=(passed_validation_job.pk,)),
        {"decision": "REJECTED", "rationale": "must remain separate"},
    ).status_code == 403
    assert record.call_count == 1


def test_human_review_form_cannot_submit_final_approval() -> None:
    form = HumanReviewForm(
        data={
            "decision": "APPROVED",
            "rationale": "attempt to bypass approval workflow",
        }
    )

    assert form.is_valid() is False
    assert "decision" in form.errors


def test_candidate_approval_form_requires_hidden_idempotency_uuid() -> None:
    missing = _approval_payload()
    missing.pop("approval_request_id")
    invalid = CandidateApprovalForm(data=missing)
    valid = CandidateApprovalForm(
        data=_approval_payload(approval_request_id=str(uuid.uuid4()))
    )

    assert invalid.is_valid() is False
    assert "approval_request_id" in invalid.errors
    assert valid.is_valid() is True
    assert valid.fields["approval_request_id"].widget.is_hidden is True


def test_candidate_approval_requires_current_password_reauthentication(
    client,
    approver_user,
    passed_validation_job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approve = Mock(return_value={"approval_hash": "sha256:" + "4" * 64})
    monkeypatch.setattr("portal.views.approve_job_candidate", approve)
    client.force_login(approver_user)

    denied = client.post(
        reverse("portal:review-approve", args=(passed_validation_job.pk,)),
        _approval_payload(password="wrong-password"),
    )

    assert denied.status_code == 302
    approve.assert_not_called()

    accepted = client.post(
        reverse("portal:review-approve", args=(passed_validation_job.pk,)),
        _approval_payload(),
    )

    assert accepted.status_code == 302
    approve.assert_called_once()


@pytest.mark.parametrize("account_kind", ("research_admin", "superuser"))
def test_approval_form_requires_explicit_approver_duty_role(
    client,
    passed_validation_job,
    account_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = get_user_model().objects.create_user(
        username=f"{account_kind}-{uuid.uuid4().hex}",
        password="test-password",
        is_staff=account_kind == "superuser",
        is_superuser=account_kind == "superuser",
    )
    if account_kind == "research_admin":
        user.groups.add(Group.objects.get(name="research_admin"))
    monkeypatch.setattr(
        "portal.views.load_review_context",
        lambda job: _context(),
    )
    client.force_login(user)

    response = client.get(
        reverse("portal:review-detail", args=(passed_validation_job.pk,))
    )

    assert response.status_code == 200
    assert response.context["can_approve"] is False
    assert reverse(
        "portal:review-approve",
        args=(passed_validation_job.pk,),
    ) not in response.content.decode("utf-8")
    assert client.post(
        reverse("portal:review-approve", args=(passed_validation_job.pk,)),
        _approval_payload(),
    ).status_code == 403

    user.groups.add(Group.objects.get(name="research_approver"))
    _clear_permission_cache(user)
    response = client.get(
        reverse("portal:review-detail", args=(passed_validation_job.pk,))
    )
    assert response.context["can_approve"] is True
    assert reverse(
        "portal:review-approve",
        args=(passed_validation_job.pk,),
    ) in response.content.decode("utf-8")


def test_originator_cannot_review_or_approve_own_result(
    runner_user,
    passed_validation_job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "portal.governance.load_review_context",
        lambda job: _context(),
    )
    source_path = tmp_path / "source-report.json"
    source_path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(
        "portal.governance.resolve_artifact_ref",
        lambda value: source_path,
    )

    runner_user.groups.add(Group.objects.get(name="research_reviewer"))
    _clear_permission_cache(runner_user)
    with pytest.raises(
        GovernanceError,
        match="governance_separation_of_duties_violation",
    ):
        record_job_review(
            user=runner_user,
            job=passed_validation_job,
            cleaned_data={
                "decision": "REJECTED",
                "rationale": "self review must fail closed",
            },
            correlation_id=str(uuid.uuid4()),
        )

    runner_user.groups.add(Group.objects.get(name="research_approver"))
    _clear_permission_cache(runner_user)
    with pytest.raises(
        GovernanceError,
        match="governance_separation_of_duties_violation",
    ):
        approve_job_candidate(
            user=runner_user,
            job=passed_validation_job,
            cleaned_data={
                "rationale": "self approval must fail closed",
                "resolved_requirement_ids": (),
            },
            correlation_id=str(uuid.uuid4()),
        )


def test_prior_reviewer_cannot_be_final_approver(
    approver_user,
    passed_validation_job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "portal.governance.load_review_context",
        lambda job: _context(
            prior_reviews=({"reviewer_id": str(approver_user.pk)},)
        ),
    )
    source_path = tmp_path / "source-report.json"
    source_path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(
        "portal.governance.resolve_artifact_ref",
        lambda value: source_path,
    )

    with pytest.raises(
        GovernanceError,
        match="governance_separation_of_duties_violation",
    ):
        approve_job_candidate(
            user=approver_user,
            job=passed_validation_job,
            cleaned_data={
                "rationale": "the reviewer cannot approve the same result",
                "resolved_requirement_ids": (),
            },
            correlation_id=str(uuid.uuid4()),
        )


def test_approval_permission_without_approver_role_fails_closed(
    passed_validation_job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permission_only_user = get_user_model().objects.create_user(
        username=f"permission-only-{uuid.uuid4().hex}",
        password="test-password",
    )
    permission_only_user.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="portal",
            codename="approve_research_candidate",
        )
    )
    monkeypatch.setattr(
        "portal.governance.load_review_context",
        lambda job: _context(),
    )
    source_path = tmp_path / "source-report.json"
    source_path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(
        "portal.governance.resolve_artifact_ref",
        lambda value: source_path,
    )

    with pytest.raises(
        GovernanceError,
        match="strategy_approval_actor_role_required",
    ):
        approve_job_candidate(
            user=permission_only_user,
            job=passed_validation_job,
            cleaned_data={
                "rationale": "permission alone must not imply the duty role",
                "resolved_requirement_ids": (),
            },
            correlation_id=str(uuid.uuid4()),
        )


def test_approver_group_reaches_hash_bound_approval_validation(
    approver_user,
    passed_validation_job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "portal.governance.load_review_context",
        lambda job: _context(),
    )
    source_path = tmp_path / "source-report.json"
    source_path.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(
        "portal.governance.resolve_artifact_ref",
        lambda value: source_path,
    )

    with pytest.raises(
        GovernanceError,
        match="strategy_approval_source_report_content_hash_mismatch",
    ):
        approve_job_candidate(
            user=approver_user,
            job=passed_validation_job,
            cleaned_data={
                "rationale": "valid approver must still satisfy evidence binding",
                "resolved_requirement_ids": (),
            },
            correlation_id=str(uuid.uuid4()),
        )


def test_approved_subject_resolves_for_exact_replay_but_ambiguous_version_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_hash = "sha256:" + "5" * 64
    rows = [
        {
            "event_type": "lifecycle_transition",
            "subject_type": "strategy_candidate",
            "subject_id": "candidate-1",
            "subject_version": "1",
            "to_state": "RESEARCH_APPROVED",
        },
        {
            "event_type": "human_review_decision",
            "decision": "APPROVED",
            "subject_type": "strategy_candidate",
            "subject_id": "candidate-1",
            "subject_version": "1",
            "reviewed_artifact_hash": result_hash,
        },
    ]
    monkeypatch.setattr(
        "portal.governance.validate_governance_registry",
        lambda _paths: {"status": "PASS"},
    )
    monkeypatch.setattr(
        "portal.governance.load_governance_rows",
        lambda _path: list(rows),
    )

    subject, _loaded, state = _approval_ready_subject(
        {"selected_candidate_id": "candidate-1"},
        reviewed_artifact_hash=result_hash,
    )

    assert subject.subject_version == "1"
    assert state == "RESEARCH_APPROVED"
    rows.append(
        {
            "event_type": "lifecycle_transition",
            "subject_type": "strategy_candidate",
            "subject_id": "candidate-1",
            "subject_version": "2",
            "to_state": "OUT_OF_SAMPLE_PASSED",
        }
    )
    with pytest.raises(
        ValidationError,
        match="governance_candidate_not_uniquely_approval_ready",
    ):
        _approval_ready_subject(
            {"selected_candidate_id": "candidate-1"},
            reviewed_artifact_hash=result_hash,
        )


def test_prior_approved_actor_is_not_added_to_replay_prohibition_set(
    approver_user,
    passed_validation_job,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "portal.governance.load_review_context",
        lambda _job: _context(
            prior_reviews=(
                {
                    "decision": "APPROVED",
                    "reviewer_id": str(approver_user.pk),
                },
            )
        ),
    )
    source_path = tmp_path / "source-report.json"
    source_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "portal.governance.resolve_artifact_ref",
        lambda _value: source_path,
    )
    captured = []

    def approve(_service, request):
        captured.append(request)
        return SimpleNamespace(
            content_hash="sha256:" + "4" * 64,
            review_row_hash="sha256:" + "5" * 64,
            transition_row_hash="sha256:" + "6" * 64,
            subject=_context()["subject"],
        )

    monkeypatch.setattr(
        "portal.governance.ResearchGovernanceApplicationService.approve_candidate",
        approve,
    )
    monkeypatch.setattr(
        "portal.governance.record_web_audit_event",
        lambda **_kwargs: {},
    )
    request_id = uuid.uuid4()

    approve_job_candidate(
        user=approver_user,
        job=passed_validation_job,
        cleaned_data={
            "approval_request_id": request_id,
            "rationale": "exact replay",
            "resolved_requirement_ids": (),
        },
        correlation_id=str(uuid.uuid4()),
    )

    assert captured[0].idempotency_key == str(request_id)
    assert str(approver_user.pk) not in captured[0].prohibited_actor_ids


def test_approved_detail_hides_new_review_and_approval_forms(
    client,
    approver_user,
    passed_validation_job,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "portal.views.load_review_context",
        lambda _job: {
            **_context(),
            "candidate_state": "RESEARCH_APPROVED",
            "approval_ready": False,
        },
    )
    client.force_login(approver_user)

    response = client.get(
        reverse("portal:review-detail", args=(passed_validation_job.pk,))
    )

    content = response.content.decode("utf-8")
    assert response.status_code == 200
    assert "최종 승인이 기록되었습니다" in content
    assert reverse(
        "portal:review-approve",
        args=(passed_validation_job.pk,),
    ) not in content
    assert reverse(
        "portal:review-record",
        args=(passed_validation_job.pk,),
    ) not in content
