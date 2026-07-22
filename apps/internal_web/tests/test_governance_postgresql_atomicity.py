from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection, connections

from market_research.application import GovernanceSubjectRef
from market_research.research.governance import GovernanceError
from portal.governance import approve_job_candidate
from portal.models import (
    GovernanceDecision,
    GovernanceSubjectState,
    ResearchJob,
    WebAuditEvent,
)


pytestmark = [
    pytest.mark.django_db(transaction=True, serialized_rollback=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="requires live PostgreSQL row-level locks",
    ),
]


@pytest.fixture
def approval_fixture(runner_user, manifest_record):
    approver = get_user_model().objects.create_user(
        username=f"pg-governance-approver-{uuid.uuid4().hex}",
        password="test-password",
    )
    approver.groups.add(Group.objects.get(name="research_approver"))
    job = ResearchJob.objects.create(
        owner=runner_user,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.VALIDATE,
        status=ResearchJob.Status.SUCCEEDED,
        request_payload={"fixture": "postgresql-governance-atomicity"},
        request_hash="sha256:" + "a" * 64,
        idempotency_key=str(uuid.uuid4()),
        actor_id=str(runner_user.pk),
        actor_roles=["research_runner"],
        actor_permissions=["research.execute", "research.view"],
        run_id=f"run-{uuid.uuid4().hex}",
        result_ref="report:_internal_web/postgresql-governance/result.json",
        result_hash="sha256:" + "b" * 64,
        research_outcome=ResearchJob.ResearchOutcome.PASS,
    )
    subject = GovernanceSubjectRef(
        subject_type="strategy_candidate",
        subject_id=f"pg-candidate-{uuid.uuid4().hex}",
        subject_version="1",
    )
    GovernanceSubjectState.objects.create(
        source_job=job,
        subject_type=subject.subject_type,
        subject_id=subject.subject_id,
        subject_version=subject.subject_version,
        reviewed_artifact_hash=job.result_hash,
    )
    return approver.pk, job.pk, subject


def _install_service_doubles(
    *,
    monkeypatch: pytest.MonkeyPatch,
    subject: GovernanceSubjectRef,
    source: Path,
    call_count: list[int],
    count_lock: threading.Lock,
) -> None:
    monkeypatch.setattr(
        "portal.governance.load_review_context",
        lambda _job: {
            "report": {},
            "subject": subject,
            "prior_reviews": (),
            "candidate_state": "OUT_OF_SAMPLE_PASSED",
            "approval_ready": True,
        },
    )
    monkeypatch.setattr(
        "portal.governance.resolve_artifact_ref",
        lambda _ref: source,
    )

    def approve(_service, request):
        with count_lock:
            call_count[0] += 1
        return SimpleNamespace(
            content_hash="sha256:" + "c" * 64,
            review_row_hash="sha256:" + "d" * 64,
            transition_row_hash="sha256:" + "e" * 64,
            subject=subject,
        )

    monkeypatch.setattr(
        "portal.governance.ResearchGovernanceApplicationService.approve_candidate",
        approve,
    )


def _contend(
    *,
    approver_id: int,
    job_id: uuid.UUID,
    request_id: str,
    barrier: threading.Barrier,
) -> tuple[int, str, dict[str, Any] | None]:
    close_old_connections()
    try:
        user = get_user_model().objects.get(pk=approver_id)
        job = ResearchJob.objects.get(pk=job_id)
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            backend_pid = int(cursor.fetchone()[0])
        barrier.wait(timeout=10)
        try:
            result = approve_job_candidate(
                user=user,
                job=job,
                cleaned_data={
                    "approval_request_id": request_id,
                    "rationale": "concurrent PostgreSQL approval",
                    "resolved_requirement_ids": (),
                    "verification_id": "web-verification",
                    "verification_version": "1",
                    "verification_hash": "sha256:" + "9" * 64,
                },
                correlation_id=str(uuid.uuid4()),
            )
        except GovernanceError as exc:
            return backend_pid, str(exc), None
        return backend_pid, "SUCCEEDED", result
    finally:
        connections.close_all()


def test_postgresql_row_lock_allows_only_one_distinct_final_approval(
    approval_fixture,
    tmp_path: Path,
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approver_id, job_id, subject = approval_fixture
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "audit.jsonl"
    call_count = [0]
    count_lock = threading.Lock()
    _install_service_doubles(
        monkeypatch=monkeypatch,
        subject=subject,
        source=source,
        call_count=call_count,
        count_lock=count_lock,
    )
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda request_id: _contend(
                    approver_id=approver_id,
                    job_id=job_id,
                    request_id=request_id,
                    barrier=barrier,
                ),
                (str(uuid.uuid4()), str(uuid.uuid4())),
            )
        )

    assert len({backend_pid for backend_pid, _status, _result in results}) == 2
    assert sorted(status for _pid, status, _result in results) == [
        "SUCCEEDED",
        "strategy_candidate_already_approved",
    ]
    assert call_count == [1]
    assert GovernanceDecision.objects.filter(action="APPROVAL").count() == 1
    assert WebAuditEvent.objects.count() == 1
    state = GovernanceSubjectState.objects.get()
    assert state.lifecycle_state == "RESEARCH_APPROVED"
    assert state.lifecycle_version == 1


def test_postgresql_same_request_retry_converges_to_one_result(
    approval_fixture,
    tmp_path: Path,
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approver_id, job_id, subject = approval_fixture
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    settings.INTERNAL_WEB_AUDIT_PATH = tmp_path / "audit.jsonl"
    call_count = [0]
    count_lock = threading.Lock()
    _install_service_doubles(
        monkeypatch=monkeypatch,
        subject=subject,
        source=source,
        call_count=call_count,
        count_lock=count_lock,
    )
    barrier = threading.Barrier(2)
    request_id = str(uuid.uuid4())

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: _contend(
                    approver_id=approver_id,
                    job_id=job_id,
                    request_id=request_id,
                    barrier=barrier,
                ),
                range(2),
            )
        )

    assert len({backend_pid for backend_pid, _status, _result in results}) == 2
    assert [status for _pid, status, _result in results] == [
        "SUCCEEDED",
        "SUCCEEDED",
    ]
    projected_results = [result for _pid, _status, result in results]
    assert projected_results[0] == projected_results[1]
    assert call_count == [1]
    assert GovernanceDecision.objects.filter(action="APPROVAL").count() == 1
    assert WebAuditEvent.objects.count() == 1
