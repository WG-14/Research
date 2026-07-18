from __future__ import annotations

import uuid

import pytest
from django.urls import reverse

from portal.models import ResearchJob


pytestmark = pytest.mark.django_db


def test_desktop_workflow_exposes_keyboard_labels_text_status_and_sorting(
    client,
    runner_user,
    manifest_record,
) -> None:
    ResearchJob.objects.create(
        owner=runner_user,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.PREFLIGHT,
        status=ResearchJob.Status.FAILED,
        request_payload={"fixture": "accessibility"},
        request_hash=f"sha256:{'a' * 64}",
        idempotency_key=str(uuid.uuid4()),
        actor_id=str(runner_user.pk),
        actor_roles=["research_runner"],
        actor_permissions=["portal.submit_research_job"],
        error_code="RESEARCH_INPUT_UNAVAILABLE",
    )
    client.force_login(runner_user)

    dashboard = client.get(reverse("portal:dashboard")).content.decode("utf-8")
    jobs = client.get(reverse("portal:job-list")).content.decode("utf-8")
    upload = client.get(reverse("portal:manifest-upload")).content.decode("utf-8")

    assert 'href="#main-content"' in dashboard
    assert 'aria-current="page"' in dashboard
    assert "확인할 실패" in dashboard
    assert "실패" in dashboard
    assert "<caption" in jobs
    assert 'scope="col"' in jobs
    assert 'name="status"' in jobs and 'name="sort"' in jobs
    assert "필터·정렬 적용" in jobs
    assert "KST" in jobs
    assert "data-file-picker" in upload
    assert 'tabindex="0"' in upload
    assert 'aria-live="polite"' in upload


def test_job_detail_uses_progressive_disclosure_and_actionable_failure(
    client,
    runner_user,
    manifest_record,
) -> None:
    job = ResearchJob.objects.create(
        owner=runner_user,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.PREFLIGHT,
        status=ResearchJob.Status.FAILED,
        request_payload={"fixture": "failure"},
        request_hash=f"sha256:{'b' * 64}",
        idempotency_key=str(uuid.uuid4()),
        actor_id=str(runner_user.pk),
        actor_roles=["research_runner"],
        actor_permissions=["portal.submit_research_job"],
        error_code="RESEARCH_INPUT_UNAVAILABLE",
    )
    client.force_login(runner_user)

    response = client.get(reverse("portal:job-detail", args=(job.pk,)))
    body = response.content.decode("utf-8")

    assert response.status_code == 200
    assert 'role="status" aria-live="polite" aria-atomic="true"' in body
    assert "데이터셋과 읽기 권한을 확인하세요" in body
    assert "오류 코드" in body and str(job.correlation_id) in body
    assert "사전 점검 다시 실행" in body
    assert "고급 근거 보기" not in body  # failed jobs have no result evidence
