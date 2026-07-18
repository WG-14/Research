from __future__ import annotations

import json
import uuid

import pytest
from django.test import Client
from django.urls import reverse

from portal.api_contract import (
    API_VERSION,
    ApiErrorEnvelope,
    JobListResponse,
    JobResource,
    build_openapi_document,
    build_persisted_schema_document,
)
from portal.models import ManifestUpload, ResearchJob


pytestmark = pytest.mark.django_db


def _submit(
    client: Client,
    manifest_id: uuid.UUID,
    *,
    key: uuid.UUID,
    capability: str = ResearchJob.Capability.PREFLIGHT,
    source_preflight_job_id: str | None = None,
):
    payload: dict[str, str] = {"capability_id": capability}
    if source_preflight_job_id is not None:
        payload["source_preflight_job_id"] = source_preflight_job_id
    return client.post(
        reverse("portal:api-job-submit", args=(manifest_id,)),
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_IDEMPOTENCY_KEY=str(key),
    )


def test_generated_openapi_is_versioned_and_uses_runtime_models(
    client: Client,
    runner_user,
) -> None:
    unauthenticated = client.get(reverse("portal:api-openapi"))
    assert unauthenticated.status_code == 401
    assert ApiErrorEnvelope.model_validate(unauthenticated.json()).error.code == (
        "AUTHENTICATION_REQUIRED"
    )

    client.force_login(runner_user)
    response = client.get(reverse("portal:api-openapi"))
    document = response.json()

    assert response.status_code == 200
    assert document == build_openapi_document()
    assert document["openapi"] == "3.1.0"
    assert document["info"]["version"] == API_VERSION
    assert (
        document["paths"]["/api/v1/manifests/{manifest_id}/jobs/"]["post"][
            "parameters"
        ][1]["name"]
        == "Idempotency-Key"
    )
    assert "JobSubmissionRequest" in document["components"]["schemas"]
    assert "ApiErrorEnvelope" in document["components"]["schemas"]


def test_persisted_schema_is_introspected_from_actual_models() -> None:
    document = build_persisted_schema_document()
    models = document["models"]
    grant = models["portal.resourceaccessgrant"]
    job = models["portal.researchjob"]

    assert document["generated_from"] == "django.apps[portal].model._meta"
    assert {field["name"] for field in grant["fields"]} >= {
        "principal_user",
        "principal_group",
        "resource_type",
        "resource_id",
        "access",
    }
    assert {item["name"] for item in grant["constraints"]} >= {
        "portal_resource_grant_one_principal",
        "portal_resource_user_grant_uniq",
        "portal_resource_group_grant_uniq",
    }
    assert {item["name"] for item in job["constraints"]} >= {
        "portal_job_owner_idempotency_uniq",
        "portal_job_one_active_uniq",
    }


def test_submission_is_idempotent_and_conflicting_reuse_is_actionable(
    client: Client,
    runner_user,
    manifest_record,
) -> None:
    client.force_login(runner_user)
    key = uuid.uuid4()

    created = _submit(client, manifest_record.pk, key=key)
    replay = _submit(client, manifest_record.pk, key=key)

    assert created.status_code == 201
    assert replay.status_code == 200
    created_resource = JobResource.model_validate(created.json())
    replay_resource = JobResource.model_validate(replay.json())
    assert replay_resource == created_resource
    assert ResearchJob.objects.count() == 1

    second_manifest = ManifestUpload.objects.create(
        owner=runner_user,
        display_name="second.json",
        storage_ref=f"data:_internal_web/manifests/{uuid.uuid4()}.json",
        content_hash=f"sha256:{'3' * 64}",
        manifest_hash=f"sha256:{'4' * 64}",
        size_bytes=128,
        experiment_id=f"experiment-{uuid.uuid4().hex}",
        strategy_name="sma_with_filter",
    )
    conflict = _submit(
        client,
        second_manifest.pk,
        key=key,
    )
    error = ApiErrorEnvelope.model_validate(conflict.json()).error
    assert conflict.status_code == 409
    assert error.code == "IDEMPOTENCY_CONFLICT"
    assert "새 UUID" in error.action
    assert error.correlation_id


def test_job_api_has_stable_pagination_filter_sort_and_durable_status(
    client: Client,
    runner_user,
    manifest_record,
) -> None:
    jobs: list[ResearchJob] = []
    for index, status in enumerate(
        (
            ResearchJob.Status.FAILED,
            ResearchJob.Status.CANCELLED,
            ResearchJob.Status.FAILED,
        )
    ):
        jobs.append(
            ResearchJob.objects.create(
                owner=runner_user,
                manifest=manifest_record,
                capability_id=ResearchJob.Capability.PREFLIGHT,
                status=status,
                request_payload={"fixture": index},
                request_hash=f"sha256:{index + 1:064x}",
                idempotency_key=str(uuid.uuid4()),
                actor_id=str(runner_user.pk),
                actor_roles=["research_runner"],
                actor_permissions=["portal.submit_research_job"],
                error_code=(
                    "RESEARCH_INPUT_UNAVAILABLE"
                    if status == "FAILED"
                    else "CANCELLED_BY_REQUEST"
                ),
            )
        )
    client.force_login(runner_user)

    response = client.get(
        reverse("portal:api-job-list"),
        {"status": "FAILED", "limit": 1, "offset": 0, "sort": "created_at"},
    )
    page = JobListResponse.model_validate(response.json())

    assert response.status_code == 200
    assert page.page.count == 2
    assert page.page.limit == 1
    assert page.page.filters == {"status": "FAILED"}
    assert page.page.next is not None and "offset=1" in page.page.next
    assert len(page.items) == 1
    assert page.items[0].id == str(jobs[0].pk)
    assert page.items[0].progress.percent == 100
    assert page.items[0].retry_allowed
    assert page.items[0].error is not None
    assert "데이터셋" in page.items[0].error.action
    assert page.items[0].updated_at.tzinfo is not None

    invalid = client.get(reverse("portal:api-job-list"), {"limit": 101})
    assert invalid.status_code == 400
    assert ApiErrorEnvelope.model_validate(invalid.json()).error.code == (
        "PAGINATION_INVALID"
    )


def test_job_cancel_is_state_idempotent_and_object_access_does_not_leak(
    client: Client,
    runner_user,
    manifest_record,
) -> None:
    key = uuid.uuid4()
    client.force_login(runner_user)
    submitted = _submit(client, manifest_record.pk, key=key)
    job_id = uuid.UUID(submitted.json()["id"])

    first = client.post(reverse("portal:api-job-cancel", args=(job_id,)))
    second = client.post(reverse("portal:api-job-cancel", args=(job_id,)))

    assert first.status_code == second.status_code == 200
    assert JobResource.model_validate(first.json()).status == "CANCELLED"
    assert JobResource.model_validate(second.json()).status == "CANCELLED"

    outsider = type(runner_user).objects.create_user(
        username=f"outsider-{uuid.uuid4().hex}",
        password="test-password",
    )
    from django.contrib.auth.models import Group

    outsider.groups.add(Group.objects.get(name="research_viewer"))
    client.force_login(outsider)
    hidden = client.get(reverse("portal:api-job-detail", args=(job_id,)))
    assert hidden.status_code == 404
    assert ApiErrorEnvelope.model_validate(hidden.json()).error.code == "JOB_NOT_FOUND"


def test_mutating_api_csrf_failure_uses_the_documented_error_envelope(
    runner_user,
    manifest_record,
) -> None:
    client = Client(enforce_csrf_checks=True)
    client.force_login(runner_user)

    response = _submit(client, manifest_record.pk, key=uuid.uuid4())

    assert response.status_code == 403
    error = ApiErrorEnvelope.model_validate(response.json()).error
    assert error.code == "CSRF_VERIFICATION_FAILED"
    assert "X-CSRFToken" in error.action
