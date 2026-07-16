from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

import portal.audit as audit_module
from market_research.application.capabilities import get_capability
from market_research.research.hashing import content_hash_payload, sha256_prefixed

from portal.execution import ResearchJobDispatcher
from portal.jobs import (
    ActiveJobConflict,
    IdempotencyConflict,
    JobCancellationRequested,
    JobExecutionResult,
    claim_next_job,
    complete_job_success,
    enqueue_research_job,
    jobs_visible_to,
    request_job_cancellation,
    update_job_progress,
)
from portal.models import ManifestUpload, ResearchJob, WebAuditEvent
from portal.storage import make_artifact_ref, resolve_artifact_ref
from portal.worker import run_worker_once


def enqueue(runner_user, manifest_record, *, key: str | None = None):
    return enqueue_research_job(
        owner=runner_user,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.PREFLIGHT,
        idempotency_key=key or str(uuid.uuid4()),
        correlation_id=uuid.uuid4(),
    )


def complete_preflight(
    runner_user,
    manifest_record,
    settings,
    *,
    outcome: str = ResearchJob.ResearchOutcome.PASS,
    result_overrides: dict | None = None,
) -> ResearchJob:
    queued = enqueue(runner_user, manifest_record).job
    claimed = claim_next_job(worker_id="preflight-worker")
    assert claimed is not None and claimed.pk == queued.pk
    payload = {
        "schema_version": 1,
        "report_kind": "internal_web_preflight",
        "capability_id": "research-preflight",
        "request_hash": claimed.request_hash,
        "manifest_hash": manifest_record.manifest_hash,
        "manifest_content_hash": manifest_record.content_hash,
        "status": outcome,
        "readiness": {},
        "workload": {},
        **(result_overrides or {}),
    }
    result_hash = sha256_prefixed(content_hash_payload(payload))
    payload["content_hash"] = result_hash
    path = settings.RESEARCH_PATHS.report_path(
        "_internal_web",
        str(claimed.pk),
        "preflight-result.json",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return complete_job_success(
        job_id=claimed.pk,
        lease_token=claimed.lease_token,
        result=JobExecutionResult(
            result_ref=make_artifact_ref("report", path),
            result_hash=result_hash,
            research_outcome=outcome,
        ),
    )


def test_enqueue_is_idempotent_and_deduplicates_active_request(
    runner_user,
    manifest_record,
) -> None:
    key = str(uuid.uuid4())
    first = enqueue(runner_user, manifest_record, key=key)
    repeated = enqueue(runner_user, manifest_record, key=key)
    deduplicated = enqueue(runner_user, manifest_record)

    assert first.created is True
    assert repeated.created is False
    assert deduplicated.created is False
    assert repeated.job.pk == deduplicated.job.pk == first.job.pk
    assert "portal.submit_research_job" in first.job.actor_permissions

    with pytest.raises(IdempotencyConflict):
        enqueue_research_job(
            owner=runner_user,
            manifest=manifest_record,
            capability_id=ResearchJob.Capability.PREFLIGHT,
            idempotency_key=key,
            options={"request_variant": "different"},
        )

    with pytest.raises(
        ValidationError,
        match="research_job_capability_not_supported_by_web",
    ):
        enqueue_research_job(
            owner=runner_user,
            manifest=manifest_record,
            capability_id="research-backtest",
            idempotency_key=str(uuid.uuid4()),
        )


@pytest.mark.parametrize(
    "active_status",
    (
        ResearchJob.Status.QUEUED,
        ResearchJob.Status.RUNNING,
        ResearchJob.Status.CANCEL_REQUESTED,
    ),
)
def test_enqueue_rejects_a_different_second_active_job_for_owner(
    runner_user,
    manifest_record,
    active_status: str,
) -> None:
    first = enqueue(runner_user, manifest_record).job
    ResearchJob.objects.filter(pk=first.pk).update(status=active_status)
    first.refresh_from_db()

    with pytest.raises(ActiveJobConflict) as captured:
        enqueue_research_job(
            owner=runner_user,
            manifest=manifest_record,
            capability_id=ResearchJob.Capability.PREFLIGHT,
            idempotency_key=str(uuid.uuid4()),
            options={"request_variant": "different"},
        )

    assert captured.value.code == "research_job_owner_active_conflict"
    assert captured.value.existing_job.pk == first.pk
    assert captured.value.job.pk == first.pk
    assert captured.value.existing_job.owner_id == runner_user.pk
    assert ResearchJob.objects.filter(owner=runner_user).count() == 1


def test_validation_enqueue_requires_hash_bound_passed_preflight(
    runner_user,
    manifest_record,
    settings,
) -> None:
    with pytest.raises(ValidationError, match="validation_source_preflight_required"):
        enqueue_research_job(
            owner=runner_user,
            manifest=manifest_record,
            capability_id=ResearchJob.Capability.VALIDATE,
            idempotency_key=str(uuid.uuid4()),
        )

    failed_source = complete_preflight(
        runner_user,
        manifest_record,
        settings,
        outcome=ResearchJob.ResearchOutcome.FAIL,
    )
    with pytest.raises(ValidationError, match="validation_source_preflight_not_pass"):
        enqueue_research_job(
            owner=runner_user,
            manifest=manifest_record,
            capability_id=ResearchJob.Capability.VALIDATE,
            idempotency_key=str(uuid.uuid4()),
            source_preflight_job=failed_source,
        )

    wrong_manifest_binding = complete_preflight(
        runner_user,
        manifest_record,
        settings,
        result_overrides={"manifest_hash": f"sha256:{'9' * 64}"},
    )
    with pytest.raises(
        ValidationError,
        match="validation_source_preflight_result_binding_invalid",
    ):
        enqueue_research_job(
            owner=runner_user,
            manifest=manifest_record,
            capability_id=ResearchJob.Capability.VALIDATE,
            idempotency_key=str(uuid.uuid4()),
            source_preflight_job=wrong_manifest_binding,
        )

    tampered_source = complete_preflight(runner_user, manifest_record, settings)
    tampered_path = resolve_artifact_ref(tampered_source.result_ref)
    tampered_payload = json.loads(tampered_path.read_text(encoding="utf-8"))
    tampered_payload["workload"] = {"tampered": True}
    tampered_path.write_text(json.dumps(tampered_payload), encoding="utf-8")
    with pytest.raises(
        ValidationError,
        match="result_artifact_content_hash_mismatch",
    ):
        enqueue_research_job(
            owner=runner_user,
            manifest=manifest_record,
            capability_id=ResearchJob.Capability.VALIDATE,
            idempotency_key=str(uuid.uuid4()),
            source_preflight_job=tampered_source,
        )


def test_validation_source_must_match_manifest_and_canonical_request(
    runner_user,
    manifest_record,
    settings,
) -> None:
    source = complete_preflight(runner_user, manifest_record, settings)
    other_manifest = ManifestUpload.objects.create(
        owner=runner_user,
        display_name="other-manifest.json",
        storage_ref=f"data:_internal_web/manifests/{uuid.uuid4().hex}.json",
        content_hash=f"sha256:{'3' * 64}",
        manifest_hash=f"sha256:{'4' * 64}",
        size_bytes=128,
        experiment_id=f"other-{uuid.uuid4().hex}",
        strategy_name="noop_baseline",
    )
    with pytest.raises(
        ValidationError,
        match="validation_source_preflight_manifest_mismatch",
    ):
        enqueue_research_job(
            owner=runner_user,
            manifest=other_manifest,
            capability_id=ResearchJob.Capability.VALIDATE,
            idempotency_key=str(uuid.uuid4()),
            source_preflight_job=source,
        )

    ResearchJob.objects.filter(pk=source.pk).update(
        request_hash=f"sha256:{'5' * 64}"
    )
    source.refresh_from_db()
    with pytest.raises(
        ValidationError,
        match="validation_source_preflight_request_binding_invalid",
    ):
        enqueue_research_job(
            owner=runner_user,
            manifest=manifest_record,
            capability_id=ResearchJob.Capability.VALIDATE,
            idempotency_key=str(uuid.uuid4()),
            source_preflight_job=source,
        )


def test_validation_request_binds_source_job_request_and_result_hashes(
    runner_user,
    manifest_record,
    settings,
) -> None:
    source = complete_preflight(runner_user, manifest_record, settings)
    validation = enqueue_research_job(
        owner=runner_user,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.VALIDATE,
        idempotency_key=str(uuid.uuid4()),
        source_preflight_job=source,
    ).job

    assert validation.source_preflight_job_id == source.pk
    assert validation.request_payload["source_preflight_job_id"] == str(source.pk)
    assert validation.request_payload["source_preflight_request_hash"] == source.request_hash
    assert validation.request_payload["source_preflight_result_hash"] == source.result_hash


def test_dispatcher_rechecks_preflight_gate_before_opening_manifest(
    runner_user,
    manifest_record,
    settings,
) -> None:
    source = complete_preflight(runner_user, manifest_record, settings)
    validation = enqueue_research_job(
        owner=runner_user,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.VALIDATE,
        idempotency_key=str(uuid.uuid4()),
        source_preflight_job=source,
    ).job
    ResearchJob.objects.filter(pk=source.pk).update(
        research_outcome=ResearchJob.ResearchOutcome.FAIL
    )

    completed = run_worker_once(ResearchJobDispatcher(), worker_id="gate-worker")

    assert completed is not None and completed.pk == validation.pk
    assert completed.status == ResearchJob.Status.FAILED
    assert completed.error_code == "PREFLIGHT_GATE_INVALID"


def test_capability_contract_mismatch_fails_enqueue_and_dispatch(
    runner_user,
    manifest_record,
    monkeypatch,
) -> None:
    queued = enqueue(runner_user, manifest_record).job
    specification = get_capability(ResearchJob.Capability.PREFLIGHT)
    mismatched = specification.model_copy(
        update={"service_id": "ResearchApplicationService.wrong"}
    )
    monkeypatch.setattr("portal.jobs.get_capability", lambda _capability_id: mismatched)

    with pytest.raises(
        ValidationError,
        match="research_job_capability_contract_mismatch",
    ):
        enqueue(runner_user, manifest_record)

    completed = run_worker_once(ResearchJobDispatcher(), worker_id="contract-worker")

    assert completed is not None and completed.pk == queued.pk
    assert completed.status == ResearchJob.Status.FAILED
    assert completed.error_code == "CAPABILITY_CONTRACT_INVALID"


def test_claim_progress_and_success_require_the_current_lease(
    runner_user,
    manifest_record,
    settings,
) -> None:
    queued = enqueue(runner_user, manifest_record).job
    claimed = claim_next_job(worker_id="worker-1")

    assert claimed is not None
    assert claimed.pk == queued.pk
    assert claimed.status == ResearchJob.Status.RUNNING
    assert claimed.lease_token is not None
    assert claim_next_job(worker_id="worker-2") is None

    progressed = update_job_progress(
        job_id=claimed.pk,
        lease_token=claimed.lease_token,
        stage="validating",
        details={"fold": 1},
    )
    assert progressed.progress_details == {"fold": 1}

    payload = {"schema_version": 1, "artifact_type": "web-test"}
    result_hash = sha256_prefixed(content_hash_payload(payload))
    payload["content_hash"] = result_hash
    path = settings.RESEARCH_PATHS.report_path(
        "_internal_web", str(claimed.pk), "result.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = JobExecutionResult(
        result_ref=make_artifact_ref("report", path),
        result_hash=result_hash,
        run_id="run-test",
        research_outcome=ResearchJob.ResearchOutcome.PASS,
    )
    completed = complete_job_success(
        job_id=claimed.pk,
        lease_token=claimed.lease_token,
        result=result,
    )
    assert completed.status == ResearchJob.Status.SUCCEEDED
    assert completed.result_hash == result_hash
    assert completed.lease_token is None


def test_cancel_queued_and_running_jobs_at_safe_boundary(
    runner_user,
    manifest_record,
) -> None:
    queued = enqueue(runner_user, manifest_record).job
    cancelled = request_job_cancellation(actor=runner_user, job_id=queued.pk)
    assert cancelled.status == ResearchJob.Status.CANCELLED

    running = enqueue(runner_user, manifest_record).job
    claimed = claim_next_job(worker_id="worker")
    assert claimed is not None and claimed.pk == running.pk
    requested = request_job_cancellation(actor=runner_user, job_id=running.pk)
    assert requested.status == ResearchJob.Status.CANCEL_REQUESTED
    with pytest.raises(JobCancellationRequested):
        update_job_progress(
            job_id=claimed.pk,
            lease_token=claimed.lease_token,
            stage="boundary",
        )


def test_worker_does_not_mutate_an_expired_running_job(
    runner_user,
    manifest_record,
) -> None:
    job = enqueue(runner_user, manifest_record).job
    claimed = claim_next_job(worker_id="worker")
    assert claimed is not None and claimed.pk == job.pk
    original_status = claimed.status
    original_lease_token = claimed.lease_token
    ResearchJob.objects.filter(pk=job.pk).update(
        lease_expires_at=datetime(2000, 1, 1, tzinfo=timezone.utc)
    )

    class UnexpectedDispatcher:
        def execute(self, job, progress):
            raise AssertionError("expired running job must not be claimed or repaired")

    assert run_worker_once(UnexpectedDispatcher(), worker_id="other-worker") is None
    job.refresh_from_db()

    assert job.status == original_status == ResearchJob.Status.RUNNING
    assert job.lease_token == original_lease_token
    assert job.finished_at is None
    assert job.error_code == ""


def test_visibility_is_owner_scoped_unless_reviewer_has_global_permission(
    runner_user,
    reviewer_user,
    manifest_record,
) -> None:
    job = enqueue(runner_user, manifest_record).job

    assert list(jobs_visible_to(runner_user)) == [job]
    assert list(jobs_visible_to(reviewer_user)) == [job]


def test_non_owner_without_management_permission_cannot_cancel(
    runner_user,
    reviewer_user,
    manifest_record,
) -> None:
    job = enqueue(runner_user, manifest_record).job
    with pytest.raises(PermissionDenied):
        request_job_cancellation(actor=reviewer_user, job_id=job.pk)


def test_worker_executes_one_job_through_direct_dispatcher(
    runner_user,
    manifest_record,
    settings,
) -> None:
    queued = enqueue(runner_user, manifest_record).job

    class Dispatcher:
        def execute(self, job, progress):
            progress({"stage": "fixture", "completed": 1})
            payload = {"schema_version": 1, "artifact_type": "worker-test"}
            result_hash = sha256_prefixed(content_hash_payload(payload))
            payload["content_hash"] = result_hash
            path = settings.RESEARCH_PATHS.report_path(
                "_internal_web", str(job.pk), "worker-result.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            return JobExecutionResult(
                result_ref=make_artifact_ref("report", path),
                result_hash=result_hash,
                research_outcome=ResearchJob.ResearchOutcome.PASS,
            )

    result = run_worker_once(Dispatcher(), worker_id="worker-direct")

    assert result is not None and result.pk == queued.pk
    assert result.status == ResearchJob.Status.SUCCEEDED
    assert result.progress_stage == "complete"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_success_audit_failure_never_reclassifies_the_terminal_job(
    runner_user,
    manifest_record,
    settings,
    monkeypatch,
) -> None:
    queued = enqueue(runner_user, manifest_record).job

    class Dispatcher:
        def execute(self, job, progress):
            payload = {"schema_version": 1, "artifact_type": "audit-failure-test"}
            result_hash = sha256_prefixed(content_hash_payload(payload))
            payload["content_hash"] = result_hash
            path = settings.RESEARCH_PATHS.report_path(
                "_internal_web", str(job.pk), "audit-failure-result.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            return JobExecutionResult(
                result_ref=make_artifact_ref("report", path),
                result_hash=result_hash,
                research_outcome=ResearchJob.ResearchOutcome.PASS,
            )

    original_append = audit_module._append_payload

    def fail_success_audit(payload):
        if payload.get("action") == "research_job_succeeded":
            raise OSError("simulated append-only audit outage")
        return original_append(payload)

    monkeypatch.setattr("portal.audit._append_payload", fail_success_audit)

    with pytest.raises(OSError, match="audit outage"):
        run_worker_once(Dispatcher(), worker_id="worker-audit-failure")

    queued.refresh_from_db()
    assert queued.status == ResearchJob.Status.SUCCEEDED
    assert queued.progress_stage == "complete"
    assert queued.lease_token is None
    assert queued.error_code == ""
    pending = [
        event
        for event in WebAuditEvent.objects.all()
        if event.payload.get("action") == "research_job_succeeded"
    ]
    assert len(pending) == 1
    assert pending[0].projected_at is None
