"""Fail-closed object authorization for internal-web research resources."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from django.db.models import Q, QuerySet

from .models import ManifestUpload, ResearchJob, ResourceAccessGrant


_STABLE_RESEARCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_PACKAGE_REF_FIELDS = frozenset({"authority", "logical_id", "version", "content_hash"})


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


def can_access_dataset(
    user: Any,
    dataset_id: str,
    *,
    access: str = ResourceAccessGrant.Access.VIEW,
) -> bool:
    """Resolve an exact dataset grant without wildcard or prefix semantics."""

    normalized = str(dataset_id or "").strip()
    if not normalized or not getattr(user, "is_authenticated", False):
        return False
    if access == ResourceAccessGrant.Access.VIEW and user.has_perm(
        "portal.view_all_research_datasets"
    ):
        return True
    return (
        _principal_grants(user, access=access)
        .filter(
            resource_type=ResourceAccessGrant.ResourceType.DATASET,
            resource_id=normalized,
        )
        .exists()
    )


def datasets_visible_to(
    user: Any,
    records: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Filter external-registry projections through immutable dataset grants."""

    values = tuple(records)
    if getattr(user, "is_authenticated", False) and user.has_perm(
        "portal.view_all_research_datasets"
    ):
        return values
    granted = frozenset(
        _principal_grants(user, access=ResourceAccessGrant.Access.VIEW)
        .filter(resource_type=ResourceAccessGrant.ResourceType.DATASET)
        .values_list("resource_id", flat=True)
    )
    return tuple(
        record for record in values if str(record.get("logical_id") or "") in granted
    )


def research_package_dataset_ids(record: Mapping[str, Any]) -> frozenset[str]:
    """Return every dataset identity carried by a package projection.

    Summary and technical projections intentionally repeat this authority so
    callers can authorize either detail level.  Treat malformed values as
    absent; a package without at least one usable identity is denied by the
    helpers below.
    """

    identities: set[str] = set()

    def add_identity(value: Any) -> None:
        if isinstance(value, str) and value and value == value.strip():
            identities.add(value)

    summary = record.get("summary")
    if isinstance(summary, Mapping):
        add_identity(summary.get("dataset_id"))
        snapshot_ref = summary.get("dataset_snapshot_ref")
        if isinstance(snapshot_ref, Mapping):
            add_identity(snapshot_ref.get("logical_id"))

    technical = record.get("technical")
    if isinstance(technical, Mapping):
        evidence_refs = technical.get("evidence_refs")
        if isinstance(evidence_refs, Mapping):
            snapshot_ref = evidence_refs.get("dataset_snapshot")
            if isinstance(snapshot_ref, Mapping):
                add_identity(snapshot_ref.get("logical_id"))

    return frozenset(identities)


def can_access_research_package(user: Any, record: Mapping[str, Any]) -> bool:
    """Require authorization for the package's one immutable dataset binding."""

    dataset_ids = research_package_dataset_ids(record)
    return len(dataset_ids) == 1 and all(
        can_access_dataset(user, dataset_id) for dataset_id in dataset_ids
    )


def research_packages_visible_to(
    user: Any,
    records: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Filter package projections without exposing ungranted dataset usage."""

    values = tuple(records)
    if not getattr(user, "is_authenticated", False):
        return ()
    broadly_authorized = user.has_perm("portal.view_all_research_datasets")
    granted = (
        frozenset()
        if broadly_authorized
        else frozenset(
            _principal_grants(user, access=ResourceAccessGrant.Access.VIEW)
            .filter(resource_type=ResourceAccessGrant.ResourceType.DATASET)
            .values_list("resource_id", flat=True)
        )
    )
    visible: list[dict[str, Any]] = []
    for record in values:
        dataset_ids = research_package_dataset_ids(record)
        if len(dataset_ids) == 1 and (
            broadly_authorized or dataset_ids.issubset(granted)
        ):
            visible.append(record)
    return tuple(visible)


def can_access_research_package_lineage(
    user: Any,
    payload: Mapping[str, Any],
    *,
    root_id: str,
    root_version: str,
    load_record: Callable[[str, str], Mapping[str, Any]],
) -> bool:
    """Authorize the root and every package reference exposed by lineage."""

    package_ref = payload.get("package_ref")
    supersedes_chain = payload.get("supersedes_chain")
    direct_descendants = payload.get("direct_descendants")
    if (
        not isinstance(package_ref, Mapping)
        or not isinstance(supersedes_chain, (list, tuple))
        or not isinstance(direct_descendants, (list, tuple))
    ):
        return False

    raw_refs = (package_ref, *supersedes_chain, *direct_descendants)
    refs: list[tuple[str, str, str]] = []
    for raw_ref in raw_refs:
        if (
            not isinstance(raw_ref, Mapping)
            or frozenset(raw_ref) != _PACKAGE_REF_FIELDS
        ):
            return False
        if raw_ref.get("authority") != "research_package_registry":
            return False
        logical_id = raw_ref.get("logical_id")
        version = raw_ref.get("version")
        content_hash = raw_ref.get("content_hash")
        if not (
            isinstance(logical_id, str)
            and _STABLE_RESEARCH_ID.fullmatch(logical_id)
            and isinstance(version, str)
            and _STABLE_RESEARCH_ID.fullmatch(version)
            and isinstance(content_hash, str)
            and _SHA256.fullmatch(content_hash)
        ):
            return False
        refs.append((logical_id, version, content_hash))

    if not refs or refs[0][:2] != (root_id, root_version):
        return False
    identities = tuple((logical_id, version) for logical_id, version, _ in refs)
    if len(set(identities)) != len(identities):
        return False

    for logical_id, version, content_hash in refs:
        try:
            record = load_record(logical_id, version)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return False
        if not isinstance(record, Mapping):
            return False
        summary = record.get("summary")
        if not (
            record.get("logical_id") == logical_id
            and record.get("version") == version
            and isinstance(summary, Mapping)
            and summary.get("content_hash") == content_hash
            and can_access_research_package(user, record)
        ):
            return False
    return True


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
