from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from market_research.research.hashing import content_hash_payload, sha256_prefixed
from portal.execution import ResearchJobDispatcher
from portal.forms import ManifestUploadForm
from portal.jobs import enqueue_research_job
from portal.models import ManifestUpload, ResearchJob
from portal.presenters import load_safe_result
from portal.worker import run_worker_once


pytestmark = pytest.mark.django_db


def _upload_manifest(runner_user, manifest_path: Path) -> ManifestUpload:
    form = ManifestUploadForm(
        files={
            "manifest_file": SimpleUploadedFile(
                "noop-manifest.json",
                manifest_path.read_bytes(),
                content_type="application/json",
            )
        }
    )
    record, _created = form.save(
        owner=runner_user,
        correlation_id=uuid.uuid4(),
    )
    return record


def test_login_dashboard_upload_and_preflight_submission_flow(
    client,
    runner_user,
    manifest_bytes: bytes,
) -> None:
    assert client.get(reverse("portal:dashboard")).status_code == 302
    client.force_login(runner_user)
    dashboard = client.get(reverse("portal:dashboard"))
    assert dashboard.status_code == 200
    dashboard_body = dashboard.content.decode("utf-8")
    assert "대시보드" in dashboard_body
    assert "새 연구 시작" in dashboard_body
    assert dashboard["Cache-Control"] == "private, no-store"

    upload = client.post(
        reverse("portal:manifest-upload"),
        {
            "display_name": "장기 검증 연구",
            "manifest_file": SimpleUploadedFile(
                "manifest.json",
                manifest_bytes,
                content_type="application/json",
            ),
        },
    )
    assert upload.status_code == 302
    record = ManifestUpload.objects.get(owner=runner_user)
    assert record.display_name == "장기 검증 연구"

    detail = client.get(reverse("portal:manifest-detail", args=(record.pk,)))
    assert detail.status_code == 200
    assert record.experiment_id in detail.content.decode("utf-8")

    queued = client.post(
        reverse("portal:manifest-preflight", args=(record.pk,)),
        {"idempotency_key": str(uuid.uuid4())},
    )
    assert queued.status_code == 302
    job = ResearchJob.objects.get(owner=runner_user)
    assert job.status == ResearchJob.Status.QUEUED
    assert queued.url == reverse("portal:job-detail", args=(job.pk,))

    job_page = client.get(queued.url)
    body = job_page.content.decode("utf-8")
    assert job_page.status_code == 200
    assert "대기 중" in body
    assert str(settings.RESEARCH_PATHS.data_root) not in body


def test_manifest_upload_conflict_response_exposes_no_owner_or_storage_details(
    client,
    runner_user,
    manifest_bytes: bytes,
) -> None:
    first_form = ManifestUploadForm(
        files={
            "manifest_file": SimpleUploadedFile(
                "first.json",
                manifest_bytes,
                content_type="application/json",
            )
        }
    )
    first, _created = first_form.save(
        owner=runner_user,
        correlation_id=uuid.uuid4(),
    )

    other = get_user_model().objects.create_user(
        username=f"runner-{uuid.uuid4().hex}",
        password="test-password",
    )
    other.groups.add(Group.objects.get(name="research_runner"))
    payload = json.loads(manifest_bytes)
    payload["hypothesis"] = "a conflicting manifest owned by another user"
    client.force_login(other)

    response = client.post(
        reverse("portal:manifest-upload"),
        {
            "manifest_file": SimpleUploadedFile(
                "conflict.json",
                json.dumps(payload, sort_keys=True).encode("utf-8"),
                content_type="application/json",
            )
        },
    )

    body = response.content.decode("utf-8")
    assert response.status_code == 200
    assert "동일한 연구 식별값이 이미 등록되어 있습니다" in body
    assert first.storage_ref not in body
    assert runner_user.username not in body
    assert ManifestUpload.objects.count() == 1


def test_job_and_manifest_object_access_is_owner_scoped(
    client,
    runner_user,
    reviewer_user,
    manifest_record,
) -> None:
    job = enqueue_research_job(
        owner=runner_user,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.PREFLIGHT,
        idempotency_key=str(uuid.uuid4()),
    ).job
    other = type(runner_user).objects.create_user(
        username=f"viewer-{uuid.uuid4().hex}",
        password="test-password",
    )
    other.groups.add(Group.objects.get(name="research_viewer"))
    client.force_login(other)
    assert client.get(reverse("portal:manifest-detail", args=(manifest_record.pk,))).status_code == 404
    assert client.get(reverse("portal:job-detail", args=(job.pk,))).status_code == 404

    client.force_login(reviewer_user)
    assert client.get(reverse("portal:manifest-detail", args=(manifest_record.pk,))).status_code == 200
    assert client.get(reverse("portal:job-detail", args=(job.pk,))).status_code == 200
    review_queue = client.get(reverse("portal:review-queue"))
    assert review_queue.status_code == 200
    dashboard_body = client.get(reverse("portal:dashboard")).content.decode("utf-8")
    assert "검토함" in dashboard_body
    assert "새 연구" not in dashboard_body
    manifest_body = client.get(
        reverse("portal:manifest-detail", args=(manifest_record.pk,))
    ).content.decode("utf-8")
    assert "사전 점검 시작" not in manifest_body


def test_review_queue_contains_only_passed_validation_outcomes(
    client,
    runner_user,
    reviewer_user,
    manifest_record,
) -> None:
    jobs = {}
    for outcome in ResearchJob.ResearchOutcome.values:
        digest = "a" if outcome == ResearchJob.ResearchOutcome.PASS else "b"
        jobs[outcome] = ResearchJob.objects.create(
            owner=runner_user,
            manifest=manifest_record,
            capability_id=ResearchJob.Capability.VALIDATE,
            status=ResearchJob.Status.SUCCEEDED,
            request_payload={"fixture": outcome},
            request_hash=f"sha256:{digest * 64}",
            idempotency_key=str(uuid.uuid4()),
            actor_id=str(runner_user.pk),
            actor_roles=["research_runner"],
            actor_permissions=["portal.submit_research_job"],
            result_ref=f"report:_internal_web/{outcome.lower()}.json",
            result_hash=f"sha256:{digest * 64}",
            research_outcome=outcome,
        )

    client.force_login(reviewer_user)
    response = client.get(reverse("portal:review-queue"))

    assert response.status_code == 200
    visible_ids = {job.pk for job in response.context["jobs"]}
    assert visible_ids == {jobs[ResearchJob.ResearchOutcome.PASS].pk}
    assert "실행자와 검토·승인자는 서로 달라야 합니다" in response.content.decode("utf-8")
    dashboard = client.get(reverse("portal:dashboard"))
    assert dashboard.context["metrics"]["review"] == 1


def test_mutating_routes_require_csrf(runner_user, manifest_record) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(runner_user)
    response = client.post(
        reverse("portal:manifest-preflight", args=(manifest_record.pk,)),
        {"idempotency_key": str(uuid.uuid4())},
    )
    assert response.status_code == 403
    assert ResearchJob.objects.count() == 0


def test_real_engine_preflight_runs_through_persistent_worker(
    runner_user,
    noop_research_fixture,
) -> None:
    paths, manifest_path = noop_research_fixture
    record = _upload_manifest(runner_user, manifest_path)
    queued = enqueue_research_job(
        owner=runner_user,
        manifest=record,
        capability_id=ResearchJob.Capability.PREFLIGHT,
        idempotency_key=str(uuid.uuid4()),
    ).job

    completed = run_worker_once(ResearchJobDispatcher())
    assert completed is not None
    assert completed.pk == queued.pk
    assert completed.status == ResearchJob.Status.SUCCEEDED, completed.error_code
    assert completed.research_outcome in ResearchJob.ResearchOutcome.values
    summary, safe_payload = load_safe_result(completed)
    assert safe_payload["report_kind"] == "internal_web_preflight"
    assert safe_payload["manifest_hash"] == record.manifest_hash
    assert summary["final_status"] in {"실행 가능", "보완 필요"}
    serialized = json.dumps(safe_payload, ensure_ascii=False)
    assert str(paths.data_root) not in serialized
    assert str(paths.db_path) not in serialized


def test_real_validation_engine_writes_hash_verified_web_result(
    runner_user,
    noop_research_fixture,
) -> None:
    paths, manifest_path = noop_research_fixture
    record = _upload_manifest(runner_user, manifest_path)
    preflight_job = enqueue_research_job(
        owner=runner_user,
        manifest=record,
        capability_id=ResearchJob.Capability.PREFLIGHT,
        idempotency_key=str(uuid.uuid4()),
    ).job
    preflight = run_worker_once(ResearchJobDispatcher())
    assert preflight is not None and preflight.pk == preflight_job.pk
    assert preflight.research_outcome == ResearchJob.ResearchOutcome.PASS
    queued = enqueue_research_job(
        owner=runner_user,
        manifest=record,
        capability_id=ResearchJob.Capability.VALIDATE,
        idempotency_key=str(uuid.uuid4()),
        source_preflight_job=preflight,
    ).job

    completed = run_worker_once(ResearchJobDispatcher())
    assert completed is not None
    assert completed.pk == queued.pk
    assert completed.status == ResearchJob.Status.SUCCEEDED, completed.error_code
    assert completed.source_preflight_job_id == preflight.pk
    assert completed.research_outcome in ResearchJob.ResearchOutcome.values
    assert completed.run_id.startswith("RUN-")
    assert completed.result_hash.startswith("sha256:")
    summary, safe_payload = load_safe_result(completed)
    assert safe_payload["content_hash"] == completed.result_hash
    assert summary["final_status"] in {"PASS", "FAIL"}
    rendered = json.dumps(safe_payload, ensure_ascii=False)
    assert str(paths.data_root) not in rendered
    assert str(paths.db_path) not in rendered

    client = Client()
    client.force_login(runner_user)
    download = client.get(reverse("portal:job-download", args=(completed.pk,)))
    assert download.status_code == 200
    projection = download.json()
    assert projection["source_result_hash"] == completed.result_hash
    without_hash = {
        key: value for key, value in projection.items() if key != "content_hash"
    }
    assert projection["content_hash"] == sha256_prefixed(
        content_hash_payload(without_hash)
    )


def test_status_endpoint_exposes_no_server_path(client, runner_user, manifest_record) -> None:
    job = enqueue_research_job(
        owner=runner_user,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.PREFLIGHT,
        idempotency_key=str(uuid.uuid4()),
    ).job
    ResearchJob.objects.filter(pk=job.pk).update(
        progress_stage="readiness_scan",
        progress_details={"split": "train"},
    )
    client.force_login(runner_user)
    response = client.get(reverse("portal:job-status", args=(job.pk,)))
    assert response.status_code == 200
    assert set(response.json()) == {
        "status",
        "status_label",
        "stage",
        "message",
        "updated_at",
        "terminal",
        "version",
    }
    assert str(settings.RESEARCH_PATHS.data_root) not in response.content.decode("utf-8")
