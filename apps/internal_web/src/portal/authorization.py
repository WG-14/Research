"""Fail-closed object authorization for internal-web research resources."""

from __future__ import annotations

import uuid
from typing import Any

from django.db.models import Q, QuerySet

from .models import ManifestUpload, ResearchJob, ResourceAccessGrant


def _principal_grants(user: Any, *, access: str) -> QuerySet[ResourceAccessGrant]:
    if not getattr(user, "is_authenticated", False):
        return ResourceAccessGrant.objects.none()
    accepted = (
        ResourceAccessGrant.Access.values
        if access == ResourceAccessGrant.Access.VIEW
        else (access,)
    )
    return ResourceAccessGrant.objects.filter(
        Q(principal_user_id=user.pk)
        | Q(principal_group_id__in=user.groups.values_list("pk", flat=True)),
        access__in=accepted,
    )


def _manifest_grant_filter(user: Any, *, access: str) -> Q:
    grants = _principal_grants(user, access=access)
    manifest_ids: list[uuid.UUID] = []
    experiment_ids: list[str] = []
    strategy_names: list[str] = []
    for resource_type, resource_id in grants.values_list(
        "resource_type", "resource_id"
    ):
        if resource_type == ResourceAccessGrant.ResourceType.MANIFEST:
            try:
                manifest_ids.append(uuid.UUID(resource_id))
            except ValueError:
                # A malformed externally provisioned grant must grant nothing.
                continue
        elif resource_type == ResourceAccessGrant.ResourceType.EXPERIMENT:
            experiment_ids.append(resource_id)
        elif resource_type == ResourceAccessGrant.ResourceType.STRATEGY:
            strategy_names.append(resource_id)
    return (
        Q(pk__in=manifest_ids)
        | Q(experiment_id__in=experiment_ids)
        | Q(strategy_name__in=strategy_names)
    )


def manifests_visible_to(
    user: Any,
    *,
    access: str = ResourceAccessGrant.Access.VIEW,
) -> QuerySet[ManifestUpload]:
    """Return exactly the manifests the actor may use for ``access``.

    Model-level Django permissions remain mandatory at the HTTP/application
    boundary.  This queryset adds the independent object decision: ownership,
    an explicit governed grant, or a deliberately broad role permission.
    """

    queryset = ManifestUpload.objects.select_related("owner")
    if not getattr(user, "is_authenticated", False):
        return queryset.none()
    if access == ResourceAccessGrant.Access.VIEW and user.has_perm(
        "portal.view_all_research_manifests"
    ):
        return queryset
    if access == ResourceAccessGrant.Access.REVIEW and (
        user.has_perm("portal.record_research_review")
        or user.has_perm("portal.approve_research_candidate")
    ):
        return queryset
    if access == ResourceAccessGrant.Access.SUBMIT and user.has_perm(
        "portal.manage_research_web"
    ):
        return queryset

    authorization = _manifest_grant_filter(user, access=access)
    if access in {
        ResourceAccessGrant.Access.VIEW,
        ResourceAccessGrant.Access.SUBMIT,
    }:
        authorization |= Q(owner_id=user.pk)
    return queryset.filter(authorization).distinct()


def can_access_manifest(
    user: Any,
    manifest: ManifestUpload,
    *,
    access: str,
) -> bool:
    if getattr(manifest, "pk", None) is None:
        return False
    return manifests_visible_to(user, access=access).filter(pk=manifest.pk).exists()


def jobs_visible_to(
    user: Any,
    *,
    access: str = ResourceAccessGrant.Access.VIEW,
) -> QuerySet[ResearchJob]:
    queryset = ResearchJob.objects.select_related("owner", "manifest")
    if not getattr(user, "is_authenticated", False):
        return queryset.none()
    if access == ResourceAccessGrant.Access.VIEW and user.has_perm(
        "portal.view_all_research_jobs"
    ):
        return queryset
    if access == ResourceAccessGrant.Access.REVIEW and (
        user.has_perm("portal.record_research_review")
        or user.has_perm("portal.approve_research_candidate")
    ):
        return queryset
    manifest_ids = manifests_visible_to(user, access=access).values_list(
        "pk", flat=True
    )
    authorization = Q(manifest_id__in=manifest_ids)
    if access == ResourceAccessGrant.Access.VIEW:
        authorization |= Q(owner_id=user.pk)
    return queryset.filter(authorization).distinct()
