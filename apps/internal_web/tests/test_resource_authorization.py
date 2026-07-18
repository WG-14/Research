from __future__ import annotations

import uuid

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from portal.authorization import jobs_visible_to, manifests_visible_to
from portal.jobs import enqueue_research_job
from portal.models import ResearchJob, ResourceAccessGrant


pytestmark = pytest.mark.django_db


def _grant(
    *,
    principal_user=None,
    principal_group=None,
    grantor,
    resource_type: str,
    resource_id: str,
    access: str,
) -> ResourceAccessGrant:
    return ResourceAccessGrant.objects.create(
        principal_user=principal_user,
        principal_group=principal_group,
        resource_type=resource_type,
        resource_id=resource_id,
        access=access,
        granted_by=grantor,
        rationale="approved object access fixture",
    )


def test_user_manifest_grant_controls_html_and_querysets(
    client,
    runner_user,
    manifest_record,
) -> None:
    viewer = get_user_model().objects.create_user(
        username=f"viewer-{uuid.uuid4().hex}", password="test-password"
    )
    viewer.groups.add(Group.objects.get(name="research_viewer"))
    job = enqueue_research_job(
        owner=runner_user,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.PREFLIGHT,
        idempotency_key=str(uuid.uuid4()),
    ).job
    client.force_login(viewer)
    assert (
        client.get(
            reverse("portal:manifest-detail", args=(manifest_record.pk,))
        ).status_code
        == 404
    )
    assert client.get(reverse("portal:job-detail", args=(job.pk,))).status_code == 404

    _grant(
        principal_user=viewer,
        grantor=runner_user,
        resource_type=ResourceAccessGrant.ResourceType.MANIFEST,
        resource_id=str(manifest_record.pk),
        access=ResourceAccessGrant.Access.VIEW,
    )

    assert list(manifests_visible_to(viewer)) == [manifest_record]
    assert list(jobs_visible_to(viewer)) == [job]
    manifest_page = client.get(
        reverse("portal:manifest-detail", args=(manifest_record.pk,))
    )
    assert manifest_page.status_code == 200
    assert "사전 점검 시작" not in manifest_page.content.decode("utf-8")
    assert client.get(reverse("portal:job-detail", args=(job.pk,))).status_code == 200


def test_group_strategy_submit_grant_uses_the_real_enqueue_service(
    manifest_record,
    runner_user,
) -> None:
    delegated = get_user_model().objects.create_user(
        username=f"delegated-{uuid.uuid4().hex}", password="test-password"
    )
    runner_group = Group.objects.get(name="research_runner")
    delegated.groups.add(runner_group)
    _grant(
        principal_group=runner_group,
        grantor=runner_user,
        resource_type=ResourceAccessGrant.ResourceType.STRATEGY,
        resource_id=manifest_record.strategy_name,
        access=ResourceAccessGrant.Access.SUBMIT,
    )

    enqueued = enqueue_research_job(
        owner=delegated,
        manifest=manifest_record,
        capability_id=ResearchJob.Capability.PREFLIGHT,
        idempotency_key=str(uuid.uuid4()),
    )

    assert enqueued.created
    assert enqueued.job.owner == delegated
    assert enqueued.job.manifest == manifest_record


def test_resource_grant_is_immutable_constrained_and_not_admin_mutable(
    runner_user,
    manifest_record,
) -> None:
    grant = _grant(
        principal_user=runner_user,
        grantor=runner_user,
        resource_type=ResourceAccessGrant.ResourceType.EXPERIMENT,
        resource_id=manifest_record.experiment_id,
        access=ResourceAccessGrant.Access.VIEW,
    )
    grant.rationale = "changed"
    with pytest.raises(ValidationError, match="resource_access_grant_is_immutable"):
        grant.save()
    with pytest.raises(ValidationError, match="resource_access_grant_is_immutable"):
        grant.delete()
    assert not admin.site.is_registered(ResourceAccessGrant)

    with pytest.raises(ValidationError):
        ResourceAccessGrant.objects.create(
            principal_user=runner_user,
            principal_group=Group.objects.get(name="research_runner"),
            resource_type=ResourceAccessGrant.ResourceType.MANIFEST,
            resource_id=str(manifest_record.pk),
            access=ResourceAccessGrant.Access.VIEW,
            granted_by=runner_user,
            rationale="two principals are forbidden",
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        ResourceAccessGrant.objects.bulk_create(
            [
                ResourceAccessGrant(
                    principal_user=runner_user,
                    principal_group=Group.objects.get(name="research_runner"),
                    resource_type=ResourceAccessGrant.ResourceType.MANIFEST,
                    resource_id=str(uuid.uuid4()),
                    access=ResourceAccessGrant.Access.VIEW,
                    granted_by=runner_user,
                    rationale="database constraint fixture",
                )
            ]
        )
