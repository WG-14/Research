"""Authoritative project/workspace aggregate for offline investment research.

The registry in this module is the root ownership and lineage authority for a
research project.  It deliberately remains framework-neutral: adapters supply
authenticated actor identities, while Core enforces project membership,
role-separated permissions, immutable object references, and lifecycle rules.
All persisted state is written through :class:`ResearchPathManager` to
repository-external roots.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from market_research.paths import ResearchPathError, ResearchPathManager
from market_research.storage_io import ensure_directory

from .hash_chain import (
    HashChainSnapshot,
    mutate_hash_chained_jsonl_atomic,
    read_hash_chained_jsonl_snapshot,
)
from .hashing import canonical_json_bytes, sha256_prefixed


RESEARCH_PROJECT_SCHEMA_VERSION = 2
RESEARCH_PROJECT_REGISTRY_SCHEMA_VERSION = 1
RESEARCH_PROJECT_REGISTRY_HASH_LABEL = "research_project_registry"
RESEARCH_PROJECT_ARTIFACT_TYPE = "research_project"

_SHA256_PREFIX_LENGTH = len("sha256:") + 64
_IDENTIFIER_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


class ResearchProjectError(ValueError):
    """A project contract, lifecycle, or registry invariant was violated."""


class ResearchProjectNotFoundError(ResearchProjectError):
    """The requested project does not exist in the authoritative registry."""


class ResearchProjectConflictError(ResearchProjectError):
    """An immutable identity, version, reference, or event conflicts."""


class ResearchProjectAuthorizationError(PermissionError):
    """An actor is not authorized by the project-scoped membership authority."""


class ResearchProjectIntegrityError(ResearchProjectError):
    """The physical hash chain or semantic project history is invalid."""


class ResearchProjectStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    CHALLENGED = "CHALLENGED"
    SUPERSEDED = "SUPERSEDED"
    DEPRECATED = "DEPRECATED"
    REJECTED = "REJECTED"
    ARCHIVED = "ARCHIVED"


class ResearchProjectRole(StrEnum):
    OWNER = "OWNER"
    RESEARCHER = "RESEARCHER"
    DATA_STEWARD = "DATA_STEWARD"
    VALIDATOR = "VALIDATOR"
    REVIEWER = "REVIEWER"
    PUBLISHER = "PUBLISHER"
    VIEWER = "VIEWER"


class ResearchProjectPermission(StrEnum):
    VIEW = "project.view"
    MANAGE_MEMBERS = "project.members.manage"
    REVISE = "project.revise"
    TRANSITION = "project.transition"
    USE_COMPUTE = "project.compute.use"
    LINK_HYPOTHESIS = "project.link.hypothesis"
    LINK_DATASET = "project.link.dataset"
    LINK_CODE = "project.link.code"
    LINK_EXPERIMENT = "project.link.experiment"
    LINK_RESULT = "project.link.result"
    LINK_VERIFICATION = "project.link.verification"
    LINK_REVIEW = "project.link.review"
    LINK_PACKAGE = "project.link.package"


class ResearchProjectObjectKind(StrEnum):
    HYPOTHESIS = "HYPOTHESIS"
    DATASET = "DATASET"
    CODE = "CODE"
    EXPERIMENT = "EXPERIMENT"
    RESULT = "RESULT"
    VERIFICATION = "VERIFICATION"
    REVIEW = "REVIEW"
    PACKAGE = "PACKAGE"


class ResearchProjectEventType(StrEnum):
    CREATED = "CREATED"
    MEMBERS_REPLACED = "MEMBERS_REPLACED"
    PROJECT_REVISED = "PROJECT_REVISED"
    REFERENCE_ATTACHED = "REFERENCE_ATTACHED"
    STATUS_TRANSITIONED = "STATUS_TRANSITIONED"


_ROLE_PERMISSIONS: Mapping[
    ResearchProjectRole, frozenset[ResearchProjectPermission]
] = {
    ResearchProjectRole.OWNER: frozenset(
        {
            ResearchProjectPermission.VIEW,
            ResearchProjectPermission.MANAGE_MEMBERS,
            ResearchProjectPermission.REVISE,
            ResearchProjectPermission.TRANSITION,
            ResearchProjectPermission.USE_COMPUTE,
        }
    ),
    ResearchProjectRole.RESEARCHER: frozenset(
        {
            ResearchProjectPermission.VIEW,
            ResearchProjectPermission.USE_COMPUTE,
            ResearchProjectPermission.LINK_HYPOTHESIS,
            ResearchProjectPermission.LINK_CODE,
            ResearchProjectPermission.LINK_EXPERIMENT,
            ResearchProjectPermission.LINK_RESULT,
        }
    ),
    ResearchProjectRole.DATA_STEWARD: frozenset(
        {
            ResearchProjectPermission.VIEW,
            ResearchProjectPermission.LINK_DATASET,
        }
    ),
    ResearchProjectRole.VALIDATOR: frozenset(
        {
            ResearchProjectPermission.VIEW,
            ResearchProjectPermission.LINK_VERIFICATION,
        }
    ),
    ResearchProjectRole.REVIEWER: frozenset(
        {
            ResearchProjectPermission.VIEW,
            ResearchProjectPermission.TRANSITION,
            ResearchProjectPermission.LINK_REVIEW,
        }
    ),
    ResearchProjectRole.PUBLISHER: frozenset(
        {
            ResearchProjectPermission.VIEW,
            ResearchProjectPermission.LINK_PACKAGE,
        }
    ),
    ResearchProjectRole.VIEWER: frozenset({ResearchProjectPermission.VIEW}),
}

_REFERENCE_PERMISSION: Mapping[ResearchProjectObjectKind, ResearchProjectPermission] = {
    ResearchProjectObjectKind.HYPOTHESIS: (ResearchProjectPermission.LINK_HYPOTHESIS),
    ResearchProjectObjectKind.DATASET: ResearchProjectPermission.LINK_DATASET,
    ResearchProjectObjectKind.CODE: ResearchProjectPermission.LINK_CODE,
    ResearchProjectObjectKind.EXPERIMENT: (ResearchProjectPermission.LINK_EXPERIMENT),
    ResearchProjectObjectKind.RESULT: ResearchProjectPermission.LINK_RESULT,
    ResearchProjectObjectKind.VERIFICATION: (
        ResearchProjectPermission.LINK_VERIFICATION
    ),
    ResearchProjectObjectKind.REVIEW: ResearchProjectPermission.LINK_REVIEW,
    ResearchProjectObjectKind.PACKAGE: ResearchProjectPermission.LINK_PACKAGE,
}

_ALLOWED_TRANSITIONS: Mapping[
    ResearchProjectStatus, frozenset[ResearchProjectStatus]
] = {
    ResearchProjectStatus.DRAFT: frozenset(
        {
            ResearchProjectStatus.ACTIVE,
            ResearchProjectStatus.REJECTED,
            ResearchProjectStatus.ARCHIVED,
        }
    ),
    ResearchProjectStatus.ACTIVE: frozenset(
        {
            ResearchProjectStatus.CHALLENGED,
            ResearchProjectStatus.SUPERSEDED,
            ResearchProjectStatus.DEPRECATED,
            ResearchProjectStatus.REJECTED,
            ResearchProjectStatus.ARCHIVED,
        }
    ),
    ResearchProjectStatus.CHALLENGED: frozenset(
        {
            ResearchProjectStatus.ACTIVE,
            ResearchProjectStatus.SUPERSEDED,
            ResearchProjectStatus.DEPRECATED,
            ResearchProjectStatus.REJECTED,
            ResearchProjectStatus.ARCHIVED,
        }
    ),
    ResearchProjectStatus.SUPERSEDED: frozenset({ResearchProjectStatus.ARCHIVED}),
    ResearchProjectStatus.DEPRECATED: frozenset({ResearchProjectStatus.ARCHIVED}),
    ResearchProjectStatus.REJECTED: frozenset({ResearchProjectStatus.ARCHIVED}),
    ResearchProjectStatus.ARCHIVED: frozenset(),
}

_MUTABLE_STATUSES = frozenset(
    {
        ResearchProjectStatus.DRAFT,
        ResearchProjectStatus.ACTIVE,
        ResearchProjectStatus.CHALLENGED,
    }
)


@dataclass(frozen=True, slots=True)
class ResearchProjectMember:
    actor_id: str
    role: ResearchProjectRole

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "actor_id",
            _identifier(self.actor_id, "research_project_member_actor_id"),
        )
        object.__setattr__(self, "role", ResearchProjectRole(self.role))

    def as_dict(self) -> dict[str, str]:
        return {"actor_id": self.actor_id, "role": self.role.value}

    @classmethod
    def from_dict(cls, value: object) -> "ResearchProjectMember":
        payload = _strict_mapping(
            value,
            expected={"actor_id", "role"},
            label="research_project_member",
        )
        return cls(
            actor_id=_string(payload["actor_id"], "research_project_member_actor_id"),
            role=ResearchProjectRole(
                _string(payload["role"], "research_project_member_role")
            ),
        )


@dataclass(frozen=True, slots=True)
class ResearchProjectObjectRef:
    project_id: str
    kind: ResearchProjectObjectKind
    object_id: str
    version: str
    content_hash: str
    artifact_uri: str
    artifact_file_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "project_id",
            _identifier(self.project_id, "research_project_ref_project_id"),
        )
        object.__setattr__(self, "kind", ResearchProjectObjectKind(self.kind))
        object.__setattr__(
            self,
            "object_id",
            _identifier(self.object_id, "research_project_ref_object_id"),
        )
        object.__setattr__(
            self,
            "version",
            _identifier(self.version, "research_project_ref_version"),
        )
        object.__setattr__(
            self,
            "content_hash",
            _sha256(self.content_hash, "research_project_ref_content_hash"),
        )
        artifact_path = Path(self.artifact_uri).expanduser()
        if not artifact_path.is_absolute():
            raise ResearchProjectError(
                "research_project_ref_artifact_uri_must_be_absolute"
            )
        object.__setattr__(self, "artifact_uri", str(artifact_path))
        object.__setattr__(
            self,
            "artifact_file_hash",
            _sha256(
                self.artifact_file_hash,
                "research_project_ref_artifact_file_hash",
            ),
        )

    @property
    def identity_key(self) -> tuple[str, str, str]:
        return (self.kind.value, self.object_id, self.version)

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return (*self.identity_key, self.content_hash)

    def as_dict(self) -> dict[str, str]:
        return {
            "project_id": self.project_id,
            "kind": self.kind.value,
            "object_id": self.object_id,
            "version": self.version,
            "content_hash": self.content_hash,
            "artifact_uri": self.artifact_uri,
            "artifact_file_hash": self.artifact_file_hash,
        }

    @classmethod
    def from_dict(cls, value: object) -> "ResearchProjectObjectRef":
        payload = _strict_mapping(
            value,
            expected={
                "project_id",
                "kind",
                "object_id",
                "version",
                "content_hash",
                "artifact_uri",
                "artifact_file_hash",
            },
            label="research_project_object_ref",
        )
        return cls(
            project_id=_string(
                payload["project_id"], "research_project_ref_project_id"
            ),
            kind=ResearchProjectObjectKind(
                _string(payload["kind"], "research_project_ref_kind")
            ),
            object_id=_string(payload["object_id"], "research_project_ref_object_id"),
            version=_string(payload["version"], "research_project_ref_version"),
            content_hash=_string(
                payload["content_hash"], "research_project_ref_content_hash"
            ),
            artifact_uri=_string(
                payload["artifact_uri"], "research_project_ref_artifact_uri"
            ),
            artifact_file_hash=_string(
                payload["artifact_file_hash"],
                "research_project_ref_artifact_file_hash",
            ),
        )


@dataclass(frozen=True, slots=True)
class ResearchProject:
    schema_version: int
    project_id: str
    title: str
    research_question: str
    owner_id: str
    status: ResearchProjectStatus
    version: int
    asset_classes: tuple[str, ...]
    markets: tuple[str, ...]
    investment_horizon: str
    expected_phenomenon: str
    economic_explanation: str
    prior_research_relationship: str
    required_data: tuple[str, ...]
    expected_challenges: tuple[str, ...]
    similar_research_assessment: str
    similar_research_refs: tuple[ResearchProjectObjectRef, ...]
    members: tuple[ResearchProjectMember, ...]
    object_refs: tuple[ResearchProjectObjectRef, ...]
    status_reason: str
    superseded_by_project_id: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if self.schema_version != RESEARCH_PROJECT_SCHEMA_VERSION:
            raise ResearchProjectError("research_project_schema_version_unsupported")
        object.__setattr__(
            self,
            "project_id",
            _identifier(self.project_id, "research_project_id"),
        )
        object.__setattr__(
            self,
            "title",
            _required_text(self.title, "research_project_title", maximum=500),
        )
        object.__setattr__(
            self,
            "research_question",
            _required_text(
                self.research_question,
                "research_project_question",
                maximum=10_000,
            ),
        )
        object.__setattr__(
            self,
            "owner_id",
            _identifier(self.owner_id, "research_project_owner_id"),
        )
        object.__setattr__(self, "status", ResearchProjectStatus(self.status))
        if isinstance(self.version, bool) or self.version < 1:
            raise ResearchProjectError("research_project_version_invalid")
        object.__setattr__(
            self,
            "asset_classes",
            _normalized_scope(
                self.asset_classes,
                label="research_project_asset_classes",
            ),
        )
        object.__setattr__(
            self,
            "markets",
            _normalized_scope(self.markets, label="research_project_markets"),
        )
        for field_name, maximum in (
            ("investment_horizon", 2_000),
            ("expected_phenomenon", 10_000),
            ("economic_explanation", 10_000),
            ("prior_research_relationship", 10_000),
            ("similar_research_assessment", 4_000),
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(
                    cast(str, getattr(self, field_name)),
                    f"research_project_{field_name}",
                    maximum=maximum,
                ),
            )
        object.__setattr__(
            self,
            "required_data",
            _normalized_scope(
                self.required_data,
                label="research_project_required_data",
            ),
        )
        object.__setattr__(
            self,
            "expected_challenges",
            _normalized_scope(
                self.expected_challenges,
                label="research_project_expected_challenges",
            ),
        )
        similar_refs = tuple(
            sorted(self.similar_research_refs, key=lambda item: item.sort_key)
        )
        if any(ref.project_id != self.project_id for ref in similar_refs):
            raise ResearchProjectError(
                "research_project_cross_project_similar_ref_forbidden"
            )
        if len({ref.identity_key for ref in similar_refs}) != len(similar_refs):
            raise ResearchProjectError("research_project_similar_ref_duplicate")
        assessment = self.similar_research_assessment.casefold()
        if assessment == "none_identified" and similar_refs:
            raise ResearchProjectError(
                "research_project_similar_ref_conflicts_with_assessment"
            )
        if assessment != "none_identified" and not similar_refs:
            raise ResearchProjectError("research_project_similar_ref_required")
        object.__setattr__(self, "similar_research_refs", similar_refs)
        members = tuple(sorted(self.members, key=lambda item: item.actor_id))
        if len({member.actor_id for member in members}) != len(members):
            raise ResearchProjectError("research_project_member_actor_duplicate")
        owners = [
            member for member in members if member.role is ResearchProjectRole.OWNER
        ]
        if len(owners) != 1 or owners[0].actor_id != self.owner_id or not members:
            raise ResearchProjectError("research_project_owner_membership_invalid")
        object.__setattr__(self, "members", members)
        object_refs = tuple(sorted(self.object_refs, key=lambda item: item.sort_key))
        if any(ref.project_id != self.project_id for ref in object_refs):
            raise ResearchProjectError("research_project_cross_project_ref_forbidden")
        identity_keys = [ref.identity_key for ref in object_refs]
        if len(set(identity_keys)) != len(identity_keys):
            raise ResearchProjectError("research_project_ref_identity_duplicate")
        object.__setattr__(self, "object_refs", object_refs)
        object.__setattr__(
            self,
            "status_reason",
            _required_text(
                self.status_reason,
                "research_project_status_reason",
                maximum=4_000,
            ),
        )
        if self.superseded_by_project_id is not None:
            object.__setattr__(
                self,
                "superseded_by_project_id",
                _identifier(
                    self.superseded_by_project_id,
                    "research_project_superseded_by_project_id",
                ),
            )
        if self.status is ResearchProjectStatus.SUPERSEDED:
            if (
                self.superseded_by_project_id is None
                or self.superseded_by_project_id == self.project_id
            ):
                raise ResearchProjectError(
                    "research_project_superseded_target_required"
                )
        elif self.superseded_by_project_id is not None:
            raise ResearchProjectError("research_project_superseded_target_not_allowed")
        created = _timestamp(self.created_at, "research_project_created_at")
        updated = _timestamp(self.updated_at, "research_project_updated_at")
        if updated < created:
            raise ResearchProjectError("research_project_timestamp_order_invalid")

    @property
    def content_hash(self) -> str:
        return sha256_prefixed(
            self._content_material(),
            label=RESEARCH_PROJECT_ARTIFACT_TYPE,
        )

    def _content_material(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": RESEARCH_PROJECT_ARTIFACT_TYPE,
            "project_id": self.project_id,
            "title": self.title,
            "research_question": self.research_question,
            "owner_id": self.owner_id,
            "status": self.status.value,
            "version": self.version,
            "asset_classes": list(self.asset_classes),
            "markets": list(self.markets),
            "investment_horizon": self.investment_horizon,
            "expected_phenomenon": self.expected_phenomenon,
            "economic_explanation": self.economic_explanation,
            "prior_research_relationship": self.prior_research_relationship,
            "required_data": list(self.required_data),
            "expected_challenges": list(self.expected_challenges),
            "similar_research_assessment": self.similar_research_assessment,
            "similar_research_refs": [
                ref.as_dict() for ref in self.similar_research_refs
            ],
            "members": [member.as_dict() for member in self.members],
            "object_refs": [ref.as_dict() for ref in self.object_refs],
            "status_reason": self.status_reason,
            "superseded_by_project_id": self.superseded_by_project_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self._content_material(), "content_hash": self.content_hash}

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        title: str,
        research_question: str,
        owner_id: str,
        asset_classes: tuple[str, ...],
        markets: tuple[str, ...],
        investment_horizon: str,
        expected_phenomenon: str,
        economic_explanation: str,
        prior_research_relationship: str,
        required_data: tuple[str, ...],
        expected_challenges: tuple[str, ...],
        similar_research_assessment: str,
        similar_research_refs: tuple[ResearchProjectObjectRef, ...],
        members: tuple[ResearchProjectMember, ...],
        recorded_at: str,
        status_reason: str,
    ) -> "ResearchProject":
        return cls(
            schema_version=RESEARCH_PROJECT_SCHEMA_VERSION,
            project_id=project_id,
            title=title,
            research_question=research_question,
            owner_id=owner_id,
            status=ResearchProjectStatus.DRAFT,
            version=1,
            asset_classes=asset_classes,
            markets=markets,
            investment_horizon=investment_horizon,
            expected_phenomenon=expected_phenomenon,
            economic_explanation=economic_explanation,
            prior_research_relationship=prior_research_relationship,
            required_data=required_data,
            expected_challenges=expected_challenges,
            similar_research_assessment=similar_research_assessment,
            similar_research_refs=similar_research_refs,
            members=members,
            object_refs=(),
            status_reason=status_reason,
            superseded_by_project_id=None,
            created_at=recorded_at,
            updated_at=recorded_at,
        )

    @classmethod
    def from_dict(cls, value: object) -> "ResearchProject":
        expected = {
            "schema_version",
            "artifact_type",
            "project_id",
            "title",
            "research_question",
            "owner_id",
            "status",
            "version",
            "asset_classes",
            "markets",
            "investment_horizon",
            "expected_phenomenon",
            "economic_explanation",
            "prior_research_relationship",
            "required_data",
            "expected_challenges",
            "similar_research_assessment",
            "similar_research_refs",
            "members",
            "object_refs",
            "status_reason",
            "superseded_by_project_id",
            "created_at",
            "updated_at",
            "content_hash",
        }
        payload = _strict_mapping(
            value,
            expected=expected,
            label="research_project",
        )
        if payload["artifact_type"] != RESEARCH_PROJECT_ARTIFACT_TYPE:
            raise ResearchProjectError("research_project_artifact_type_invalid")
        members_value = _list(payload["members"], "research_project_members")
        refs_value = _list(payload["object_refs"], "research_project_object_refs")
        similar_refs_value = _list(
            payload["similar_research_refs"],
            "research_project_similar_research_refs",
        )
        asset_classes = _string_tuple(
            payload["asset_classes"], "research_project_asset_classes"
        )
        markets = _string_tuple(payload["markets"], "research_project_markets")
        version = payload["version"]
        schema_version = payload["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ResearchProjectError("research_project_version_invalid")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ResearchProjectError("research_project_schema_version_invalid")
        superseded_value = payload["superseded_by_project_id"]
        if superseded_value is not None and not isinstance(superseded_value, str):
            raise ResearchProjectError(
                "research_project_superseded_by_project_id_invalid"
            )
        project = cls(
            schema_version=schema_version,
            project_id=_string(payload["project_id"], "research_project_id"),
            title=_string(payload["title"], "research_project_title"),
            research_question=_string(
                payload["research_question"], "research_project_question"
            ),
            owner_id=_string(payload["owner_id"], "research_project_owner_id"),
            status=ResearchProjectStatus(
                _string(payload["status"], "research_project_status")
            ),
            version=version,
            asset_classes=asset_classes,
            markets=markets,
            investment_horizon=_string(
                payload["investment_horizon"],
                "research_project_investment_horizon",
            ),
            expected_phenomenon=_string(
                payload["expected_phenomenon"],
                "research_project_expected_phenomenon",
            ),
            economic_explanation=_string(
                payload["economic_explanation"],
                "research_project_economic_explanation",
            ),
            prior_research_relationship=_string(
                payload["prior_research_relationship"],
                "research_project_prior_research_relationship",
            ),
            required_data=_string_tuple(
                payload["required_data"], "research_project_required_data"
            ),
            expected_challenges=_string_tuple(
                payload["expected_challenges"],
                "research_project_expected_challenges",
            ),
            similar_research_assessment=_string(
                payload["similar_research_assessment"],
                "research_project_similar_research_assessment",
            ),
            similar_research_refs=tuple(
                ResearchProjectObjectRef.from_dict(ref) for ref in similar_refs_value
            ),
            members=tuple(
                ResearchProjectMember.from_dict(member) for member in members_value
            ),
            object_refs=tuple(
                ResearchProjectObjectRef.from_dict(ref) for ref in refs_value
            ),
            status_reason=_string(
                payload["status_reason"], "research_project_status_reason"
            ),
            superseded_by_project_id=superseded_value,
            created_at=_string(payload["created_at"], "research_project_created_at"),
            updated_at=_string(payload["updated_at"], "research_project_updated_at"),
        )
        recorded_hash = _string(
            payload["content_hash"], "research_project_content_hash"
        )
        if recorded_hash != project.content_hash:
            raise ResearchProjectIntegrityError(
                "research_project_content_hash_mismatch"
            )
        return project


@dataclass(frozen=True, slots=True)
class ResearchProjectMutation:
    project: ResearchProject
    registry_path: Path
    registry_row: Mapping[str, Any]
    created: bool


@dataclass(frozen=True, slots=True)
class ResearchProjectNamespaces:
    project_id: str
    compute_root: Path
    cache_root: Path
    artifact_boundary: Path
    cache_boundary: Path

    def ensure(self) -> None:
        ensure_directory(self.artifact_boundary)
        ensure_directory(self.cache_boundary)
        ensure_directory(
            self.compute_root,
            require_shared_mode=True,
            trusted_root=self.artifact_boundary,
        )
        ensure_directory(
            self.cache_root,
            require_shared_mode=True,
            trusted_root=self.cache_boundary,
        )


def research_project_registry_path(manager: ResearchPathManager) -> Path:
    path = manager.artifact_path(
        "reports",
        "research",
        "_registry",
        "research_projects.jsonl",
    )
    _require_external_path(
        manager,
        path,
        "research_project_registry",
        allowed_root=manager.artifact_root,
    )
    return path


def research_project_namespaces(
    manager: ResearchPathManager,
    project_id: str,
) -> ResearchProjectNamespaces:
    safe_project_id = _identifier(project_id, "research_project_id")
    compute = manager.research_project_compute_path(safe_project_id)
    cache = manager.research_project_cache_path(safe_project_id)
    _require_external_path(
        manager,
        compute,
        "research_project_compute",
        allowed_root=manager.artifact_root,
    )
    _require_external_path(
        manager,
        cache,
        "research_project_cache",
        allowed_root=manager.cache_root,
    )
    return ResearchProjectNamespaces(
        project_id=safe_project_id,
        compute_root=compute,
        cache_root=cache,
        artifact_boundary=manager.artifact_root,
        cache_boundary=manager.cache_root,
    )


def research_project_object_content_hash(
    *,
    kind: ResearchProjectObjectKind,
    object_payload: Mapping[str, Any],
) -> str:
    """Derive the authoritative semantic hash for a resolved object."""

    normalized_kind = ResearchProjectObjectKind(kind)
    if normalized_kind is ResearchProjectObjectKind.HYPOTHESIS:
        from .hypothesis_contract import parse_hypothesis_spec

        return parse_hypothesis_spec(dict(object_payload)).contract_hash()
    recorded = object_payload.get("content_hash")
    if isinstance(recorded, str) and recorded.startswith("sha256:"):
        material = dict(object_payload)
        material.pop("content_hash", None)
        generic = sha256_prefixed(
            material,
            label=f"research_project_{normalized_kind.value.casefold()}_object",
        )
        if recorded != generic:
            raise ResearchProjectIntegrityError(
                "research_project_resolved_object_content_hash_invalid"
            )
        return recorded
    return sha256_prefixed(
        dict(object_payload),
        label=f"research_project_{normalized_kind.value.casefold()}_object",
    )


def resolved_research_object_envelope(
    *,
    kind: ResearchProjectObjectKind,
    object_id: str,
    version: str,
    object_payload: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_kind = ResearchProjectObjectKind(kind)
    normalized_object_id = _identifier(object_id, "research_project_resolved_object_id")
    normalized_version = _identifier(
        version, "research_project_resolved_object_version"
    )
    payload = dict(object_payload)
    if normalized_kind is ResearchProjectObjectKind.HYPOTHESIS:
        from .hypothesis_contract import parse_hypothesis_spec

        hypothesis = parse_hypothesis_spec(payload)
        if (
            hypothesis.hypothesis_id != normalized_object_id
            or hypothesis.version != normalized_version
        ):
            raise ResearchProjectIntegrityError(
                "research_project_resolved_object_payload_identity_mismatch"
            )
    elif (
        payload.get("logical_id") != normalized_object_id
        or payload.get("version") != normalized_version
    ):
        raise ResearchProjectIntegrityError(
            "research_project_resolved_object_payload_identity_mismatch"
        )
    return {
        "schema_version": 1,
        "artifact_type": "research_project_resolved_object",
        "kind": normalized_kind.value,
        "object_id": normalized_object_id,
        "version": normalized_version,
        "content_hash": research_project_object_content_hash(
            kind=normalized_kind,
            object_payload=payload,
        ),
        "object_payload": payload,
    }


def research_project_object_file_hash(envelope: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(envelope))).hexdigest()


def resolve_research_project_object(
    *,
    manager: ResearchPathManager,
    reference: ResearchProjectObjectRef,
) -> Mapping[str, Any]:
    """Resolve and verify one repository-external immutable object envelope."""

    path = Path(reference.artifact_uri)
    if not path.is_absolute() or manager.is_within(path, manager.project_root):
        raise ResearchProjectIntegrityError(
            "research_project_ref_artifact_must_be_repository_external"
        )
    root = next(
        (
            candidate
            for candidate in (
                manager.data_root,
                manager.artifact_root,
                manager.report_root,
            )
            if manager.is_within(path, candidate)
        ),
        None,
    )
    if root is None:
        raise ResearchProjectIntegrityError(
            "research_project_ref_artifact_outside_managed_roots"
        )
    _reject_symlink_components(path=path, root=root)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ResearchProjectIntegrityError(
            "research_project_ref_artifact_unreadable"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
        ):
            raise ResearchProjectIntegrityError(
                "research_project_ref_artifact_not_immutable_regular_file"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ResearchProjectIntegrityError(
                "research_project_ref_artifact_changed_during_read"
            )
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    actual_file_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    if actual_file_hash != reference.artifact_file_hash:
        raise ResearchProjectIntegrityError(
            "research_project_ref_artifact_file_hash_mismatch"
        )
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchProjectIntegrityError(
            "research_project_ref_artifact_json_invalid"
        ) from exc
    if not isinstance(envelope, Mapping) or set(envelope) != {
        "schema_version",
        "artifact_type",
        "kind",
        "object_id",
        "version",
        "content_hash",
        "object_payload",
    }:
        raise ResearchProjectIntegrityError(
            "research_project_ref_artifact_envelope_invalid"
        )
    payload = envelope.get("object_payload")
    if not isinstance(payload, Mapping):
        raise ResearchProjectIntegrityError(
            "research_project_ref_artifact_payload_invalid"
        )
    expected = resolved_research_object_envelope(
        kind=reference.kind,
        object_id=reference.object_id,
        version=reference.version,
        object_payload=payload,
    )
    if (
        dict(envelope) != expected
        or envelope.get("content_hash") != reference.content_hash
    ):
        raise ResearchProjectIntegrityError(
            "research_project_ref_artifact_identity_mismatch"
        )
    return dict(payload)


def _reject_symlink_components(*, path: Path, root: Path) -> None:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise ResearchProjectIntegrityError(
            "research_project_ref_artifact_outside_managed_roots"
        ) from exc
    current = root.absolute()
    for part in relative.parts:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise ResearchProjectIntegrityError(
                    "research_project_ref_artifact_symlink_forbidden"
                )
        except FileNotFoundError as exc:
            raise ResearchProjectIntegrityError(
                "research_project_ref_artifact_missing"
            ) from exc


def project_permission_for_reference(
    kind: ResearchProjectObjectKind,
) -> ResearchProjectPermission:
    return _REFERENCE_PERMISSION[ResearchProjectObjectKind(kind)]


def has_research_project_permission(
    project: ResearchProject,
    *,
    actor_id: str,
    permission: ResearchProjectPermission,
) -> bool:
    normalized_actor = _identifier(actor_id, "research_project_actor_id")
    normalized_permission = ResearchProjectPermission(permission)
    member = next(
        (
            candidate
            for candidate in project.members
            if candidate.actor_id == normalized_actor
        ),
        None,
    )
    if member is None:
        return False
    if (
        project.status is ResearchProjectStatus.ARCHIVED
        and normalized_permission is not ResearchProjectPermission.VIEW
    ):
        return False
    return normalized_permission in _ROLE_PERMISSIONS[member.role]


def require_research_project_permission(
    project: ResearchProject,
    *,
    actor_id: str,
    permission: ResearchProjectPermission,
) -> None:
    if not has_research_project_permission(
        project,
        actor_id=actor_id,
        permission=permission,
    ):
        raise ResearchProjectAuthorizationError(
            "research_project_permission_denied:"
            f"{project.project_id}:{ResearchProjectPermission(permission).value}"
        )


def create_or_verify_research_project(
    *,
    manager: ResearchPathManager,
    project: ResearchProject,
    actor_id: str,
    event_id: str,
    recorded_at: str,
    reason: str,
) -> ResearchProjectMutation:
    if project.version != 1 or project.status is not ResearchProjectStatus.DRAFT:
        raise ResearchProjectError("research_project_create_snapshot_invalid")
    if project.created_at != recorded_at or project.updated_at != recorded_at:
        raise ResearchProjectError("research_project_create_timestamp_mismatch")
    for reference in project.similar_research_refs:
        resolve_research_project_object(manager=manager, reference=reference)
    change = {"project": project.as_dict()}
    return _append_project_event(
        manager=manager,
        event_type=ResearchProjectEventType.CREATED,
        project_id=project.project_id,
        actor_id=actor_id,
        event_id=event_id,
        recorded_at=recorded_at,
        reason=reason,
        change=change,
        expected_version=0,
        build=lambda current: _create_project_snapshot(
            current=current,
            project=project,
        ),
    )


def replace_research_project_members(
    *,
    manager: ResearchPathManager,
    project_id: str,
    actor_id: str,
    expected_version: int,
    members: tuple[ResearchProjectMember, ...],
    event_id: str,
    recorded_at: str,
    reason: str,
) -> ResearchProjectMutation:
    change = {
        "expected_version": _positive_version(expected_version),
        "members": [
            member.as_dict()
            for member in sorted(members, key=lambda item: item.actor_id)
        ],
    }

    def build(current: ResearchProject | None) -> ResearchProject:
        project = _required_current(current)
        _require_expected_version(project, expected_version)
        _require_mutable(project)
        require_research_project_permission(
            project,
            actor_id=actor_id,
            permission=ResearchProjectPermission.MANAGE_MEMBERS,
        )
        return replace(
            project,
            version=project.version + 1,
            members=members,
            updated_at=recorded_at,
        )

    return _append_project_event(
        manager=manager,
        event_type=ResearchProjectEventType.MEMBERS_REPLACED,
        project_id=project_id,
        actor_id=actor_id,
        event_id=event_id,
        recorded_at=recorded_at,
        reason=reason,
        change=change,
        expected_version=expected_version,
        build=build,
    )


def revise_research_project(
    *,
    manager: ResearchPathManager,
    project_id: str,
    actor_id: str,
    expected_version: int,
    title: str,
    research_question: str,
    asset_classes: tuple[str, ...],
    markets: tuple[str, ...],
    investment_horizon: str,
    expected_phenomenon: str,
    economic_explanation: str,
    prior_research_relationship: str,
    required_data: tuple[str, ...],
    expected_challenges: tuple[str, ...],
    similar_research_assessment: str,
    similar_research_refs: tuple[ResearchProjectObjectRef, ...],
    event_id: str,
    recorded_at: str,
    reason: str,
) -> ResearchProjectMutation:
    normalized_title = _required_text(title, "research_project_title", maximum=500)
    normalized_question = _required_text(
        research_question,
        "research_project_question",
        maximum=10_000,
    )
    normalized_assets = _normalized_scope(
        asset_classes, label="research_project_asset_classes"
    )
    normalized_markets = _normalized_scope(markets, label="research_project_markets")
    normalized_horizon = _required_text(
        investment_horizon,
        "research_project_investment_horizon",
        maximum=2_000,
    )
    normalized_phenomenon = _required_text(
        expected_phenomenon,
        "research_project_expected_phenomenon",
        maximum=10_000,
    )
    normalized_explanation = _required_text(
        economic_explanation,
        "research_project_economic_explanation",
        maximum=10_000,
    )
    normalized_prior_relationship = _required_text(
        prior_research_relationship,
        "research_project_prior_research_relationship",
        maximum=10_000,
    )
    normalized_required_data = _normalized_scope(
        required_data, label="research_project_required_data"
    )
    normalized_challenges = _normalized_scope(
        expected_challenges, label="research_project_expected_challenges"
    )
    normalized_similar_assessment = _required_text(
        similar_research_assessment,
        "research_project_similar_research_assessment",
        maximum=4_000,
    )
    normalized_similar_refs = tuple(
        sorted(similar_research_refs, key=lambda item: item.sort_key)
    )
    change = {
        "expected_version": _positive_version(expected_version),
        "title": normalized_title,
        "research_question": normalized_question,
        "asset_classes": list(normalized_assets),
        "markets": list(normalized_markets),
        "investment_horizon": normalized_horizon,
        "expected_phenomenon": normalized_phenomenon,
        "economic_explanation": normalized_explanation,
        "prior_research_relationship": normalized_prior_relationship,
        "required_data": list(normalized_required_data),
        "expected_challenges": list(normalized_challenges),
        "similar_research_assessment": normalized_similar_assessment,
        "similar_research_refs": [ref.as_dict() for ref in normalized_similar_refs],
    }

    def build(current: ResearchProject | None) -> ResearchProject:
        project = _required_current(current)
        _require_expected_version(project, expected_version)
        _require_mutable(project)
        require_research_project_permission(
            project,
            actor_id=actor_id,
            permission=ResearchProjectPermission.REVISE,
        )
        for reference in normalized_similar_refs:
            resolve_research_project_object(manager=manager, reference=reference)
        return replace(
            project,
            version=project.version + 1,
            title=normalized_title,
            research_question=normalized_question,
            asset_classes=normalized_assets,
            markets=normalized_markets,
            investment_horizon=normalized_horizon,
            expected_phenomenon=normalized_phenomenon,
            economic_explanation=normalized_explanation,
            prior_research_relationship=normalized_prior_relationship,
            required_data=normalized_required_data,
            expected_challenges=normalized_challenges,
            similar_research_assessment=normalized_similar_assessment,
            similar_research_refs=normalized_similar_refs,
            updated_at=recorded_at,
        )

    return _append_project_event(
        manager=manager,
        event_type=ResearchProjectEventType.PROJECT_REVISED,
        project_id=project_id,
        actor_id=actor_id,
        event_id=event_id,
        recorded_at=recorded_at,
        reason=reason,
        change=change,
        expected_version=expected_version,
        build=build,
    )


def attach_research_project_reference(
    *,
    manager: ResearchPathManager,
    project_id: str,
    actor_id: str,
    expected_version: int,
    reference: ResearchProjectObjectRef,
    event_id: str,
    recorded_at: str,
    reason: str,
) -> ResearchProjectMutation:
    normalized_project_id = _identifier(project_id, "research_project_id")
    if reference.project_id != normalized_project_id:
        raise ResearchProjectAuthorizationError(
            "research_project_cross_project_ref_forbidden"
        )
    change = {
        "expected_version": _positive_version(expected_version),
        "reference": reference.as_dict(),
    }

    def build(current: ResearchProject | None) -> ResearchProject:
        project = _required_current(current)
        _require_expected_version(project, expected_version)
        _require_mutable(project)
        require_research_project_permission(
            project,
            actor_id=actor_id,
            permission=project_permission_for_reference(reference.kind),
        )
        resolve_research_project_object(manager=manager, reference=reference)
        for existing in project.object_refs:
            if existing.identity_key != reference.identity_key:
                continue
            if existing.content_hash == reference.content_hash:
                raise ResearchProjectConflictError(
                    "research_project_reference_duplicate"
                )
            raise ResearchProjectConflictError(
                "research_project_reference_hash_conflict"
            )
        return replace(
            project,
            version=project.version + 1,
            object_refs=(*project.object_refs, reference),
            updated_at=recorded_at,
        )

    return _append_project_event(
        manager=manager,
        event_type=ResearchProjectEventType.REFERENCE_ATTACHED,
        project_id=normalized_project_id,
        actor_id=actor_id,
        event_id=event_id,
        recorded_at=recorded_at,
        reason=reason,
        change=change,
        expected_version=expected_version,
        build=build,
    )


def transition_research_project(
    *,
    manager: ResearchPathManager,
    project_id: str,
    actor_id: str,
    expected_version: int,
    to_status: ResearchProjectStatus,
    event_id: str,
    recorded_at: str,
    reason: str,
    superseded_by_project_id: str | None = None,
) -> ResearchProjectMutation:
    target = ResearchProjectStatus(to_status)
    normalized_superseded = (
        None
        if superseded_by_project_id is None
        else _identifier(
            superseded_by_project_id,
            "research_project_superseded_by_project_id",
        )
    )
    change = {
        "expected_version": _positive_version(expected_version),
        "to_status": target.value,
        "superseded_by_project_id": normalized_superseded,
    }

    def build(current: ResearchProject | None) -> ResearchProject:
        project = _required_current(current)
        _require_expected_version(project, expected_version)
        require_research_project_permission(
            project,
            actor_id=actor_id,
            permission=ResearchProjectPermission.TRANSITION,
        )
        if target not in _ALLOWED_TRANSITIONS[project.status]:
            raise ResearchProjectError(
                "research_project_status_transition_forbidden:"
                f"{project.status.value}->{target.value}"
            )
        if target is ResearchProjectStatus.SUPERSEDED:
            if normalized_superseded in {None, project.project_id}:
                raise ResearchProjectError(
                    "research_project_superseded_target_required"
                )
        elif normalized_superseded is not None:
            raise ResearchProjectError("research_project_superseded_target_not_allowed")
        return replace(
            project,
            version=project.version + 1,
            status=target,
            status_reason=_required_text(
                reason,
                "research_project_status_reason",
                maximum=4_000,
            ),
            superseded_by_project_id=normalized_superseded,
            updated_at=recorded_at,
        )

    return _append_project_event(
        manager=manager,
        event_type=ResearchProjectEventType.STATUS_TRANSITIONED,
        project_id=project_id,
        actor_id=actor_id,
        event_id=event_id,
        recorded_at=recorded_at,
        reason=reason,
        change=change,
        expected_version=expected_version,
        build=build,
    )


def get_research_project(
    manager: ResearchPathManager,
    project_id: str,
) -> ResearchProject:
    projects, _events = _validated_registry(manager)
    normalized = _identifier(project_id, "research_project_id")
    try:
        return projects[normalized]
    except KeyError as exc:
        raise ResearchProjectNotFoundError(
            f"research_project_not_found:{normalized}"
        ) from exc


def search_research_projects(
    manager: ResearchPathManager,
    *,
    query: str | None = None,
    statuses: frozenset[ResearchProjectStatus] | None = None,
    asset_classes: frozenset[str] | None = None,
    markets: frozenset[str] | None = None,
    include_archived: bool = False,
) -> tuple[ResearchProject, ...]:
    projects, _events = _validated_registry(manager)
    normalized_query = (query or "").strip().casefold()
    normalized_statuses = (
        None
        if statuses is None
        else frozenset(ResearchProjectStatus(status) for status in statuses)
    )
    normalized_assets = (
        frozenset()
        if asset_classes is None
        else frozenset(
            _required_text(
                value,
                "research_project_search_asset_class",
                maximum=255,
            ).casefold()
            for value in asset_classes
        )
    )
    normalized_markets = (
        frozenset()
        if markets is None
        else frozenset(
            _required_text(
                value,
                "research_project_search_market",
                maximum=255,
            ).casefold()
            for value in markets
        )
    )
    matches: list[ResearchProject] = []
    for project in projects.values():
        if not include_archived and project.status is ResearchProjectStatus.ARCHIVED:
            continue
        if (
            normalized_statuses is not None
            and project.status not in normalized_statuses
        ):
            continue
        searchable = " ".join(
            (
                project.project_id,
                project.title,
                project.research_question,
                project.investment_horizon,
                project.expected_phenomenon,
                project.economic_explanation,
                project.prior_research_relationship,
                project.similar_research_assessment,
                *project.required_data,
                *project.expected_challenges,
            )
        ).casefold()
        if normalized_query and normalized_query not in searchable:
            continue
        if normalized_assets and not normalized_assets.intersection(
            value.casefold() for value in project.asset_classes
        ):
            continue
        if normalized_markets and not normalized_markets.intersection(
            value.casefold() for value in project.markets
        ):
            continue
        matches.append(project)
    return tuple(sorted(matches, key=lambda item: item.project_id))


def impacted_research_projects(
    manager: ResearchPathManager,
    *,
    kind: ResearchProjectObjectKind,
    object_id: str,
    version: str | None = None,
    content_hash: str | None = None,
    include_archived: bool = True,
) -> tuple[ResearchProject, ...]:
    normalized_kind = ResearchProjectObjectKind(kind)
    normalized_object_id = _identifier(object_id, "research_project_impact_object_id")
    normalized_version = (
        None
        if version is None
        else _identifier(version, "research_project_impact_version")
    )
    normalized_hash = (
        None
        if content_hash is None
        else _sha256(content_hash, "research_project_impact_content_hash")
    )
    projects, _events = _validated_registry(manager)
    matches = []
    for project in projects.values():
        if not include_archived and project.status is ResearchProjectStatus.ARCHIVED:
            continue
        if any(
            ref.kind is normalized_kind
            and ref.object_id == normalized_object_id
            and (normalized_version is None or ref.version == normalized_version)
            and (normalized_hash is None or ref.content_hash == normalized_hash)
            for ref in (*project.object_refs, *project.similar_research_refs)
        ):
            matches.append(project)
    return tuple(sorted(matches, key=lambda item: item.project_id))


def research_project_history(
    manager: ResearchPathManager,
    project_id: str,
) -> tuple[ResearchProject, ...]:
    _projects, events = _validated_registry(manager)
    normalized = _identifier(project_id, "research_project_id")
    history = tuple(
        ResearchProject.from_dict(row["project"])
        for row in events
        if row["project_id"] == normalized
    )
    if not history:
        raise ResearchProjectNotFoundError(f"research_project_not_found:{normalized}")
    return history


def validate_research_project_registry(
    manager: ResearchPathManager,
) -> dict[str, Any]:
    path = research_project_registry_path(manager)
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path,
            label=RESEARCH_PROJECT_REGISTRY_HASH_LABEL,
        )
        if snapshot.status != "PASS":
            return {
                "status": "FAIL",
                "reasons": list(snapshot.reasons),
                "row_count": snapshot.row_count,
                "stream_hash": snapshot.stream_hash,
                "project_count": 0,
                "path": str(path.resolve()),
            }
        projects, _events = _replay_rows(snapshot)
    except (
        OSError,
        ResearchPathError,
        ResearchProjectError,
        RuntimeError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as exc:
        return {
            "status": "FAIL",
            "reasons": [f"research_project_registry_invalid:{exc}"],
            "row_count": 0,
            "stream_hash": None,
            "project_count": 0,
            "path": str(path.resolve()),
        }
    return {
        "status": "PASS",
        "reasons": [],
        "row_count": snapshot.row_count,
        "stream_hash": snapshot.stream_hash,
        "project_count": len(projects),
        "path": str(path.resolve()),
    }


def _append_project_event(
    *,
    manager: ResearchPathManager,
    event_type: ResearchProjectEventType,
    project_id: str,
    actor_id: str,
    event_id: str,
    recorded_at: str,
    reason: str,
    change: dict[str, Any],
    expected_version: int,
    build: Callable[[ResearchProject | None], ResearchProject],
) -> ResearchProjectMutation:
    path = research_project_registry_path(manager)
    normalized_project_id = _identifier(project_id, "research_project_id")
    normalized_actor = _identifier(actor_id, "research_project_actor_id")
    normalized_event_id = _identifier(event_id, "research_project_event_id")
    _timestamp(recorded_at, "research_project_event_recorded_at")
    normalized_reason = _required_text(
        reason,
        "research_project_event_reason",
        maximum=4_000,
    )
    normalized_expected_version = (
        0 if expected_version == 0 else _positive_version(expected_version)
    )
    change_hash = sha256_prefixed(
        change,
        label="research_project_event_change",
    )

    def mutation(
        snapshot: HashChainSnapshot,
        stage: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> tuple[ResearchProject, Mapping[str, Any], bool]:
        projects, events = _replay_rows(snapshot)
        existing = next(
            (row for row in events if row["event_id"] == normalized_event_id),
            None,
        )
        if existing is not None:
            expected_existing = {
                "event_type": event_type.value,
                "project_id": normalized_project_id,
                "actor_id": normalized_actor,
                "recorded_at": recorded_at,
                "reason": normalized_reason,
                "change": change,
                "change_hash": change_hash,
            }
            if any(
                existing.get(key) != value for key, value in expected_existing.items()
            ):
                raise ResearchProjectConflictError("research_project_event_id_conflict")
            return (
                ResearchProject.from_dict(existing["project"]),
                existing,
                False,
            )
        current = projects.get(normalized_project_id)
        if current is None and normalized_expected_version != 0:
            raise ResearchProjectNotFoundError(
                f"research_project_not_found:{normalized_project_id}"
            )
        if (
            current is not None
            and normalized_expected_version != 0
            and normalized_expected_version != current.version
        ):
            raise ResearchProjectConflictError("research_project_version_conflict")
        project = build(current)
        if (
            project.status is ResearchProjectStatus.SUPERSEDED
            and project.superseded_by_project_id not in projects
        ):
            raise ResearchProjectNotFoundError(
                "research_project_superseded_target_not_found:"
                f"{project.superseded_by_project_id}"
            )
        payload = {
            "schema_version": RESEARCH_PROJECT_REGISTRY_SCHEMA_VERSION,
            "event_id": normalized_event_id,
            "event_type": event_type.value,
            "project_id": normalized_project_id,
            "actor_id": normalized_actor,
            "recorded_at": recorded_at,
            "reason": normalized_reason,
            "previous_project_hash": (
                None if current is None else current.content_hash
            ),
            "change": change,
            "change_hash": change_hash,
            "project": project.as_dict(),
        }
        _validate_event(payload, current=current)
        row = stage(payload)
        return project, row, True

    result = mutate_hash_chained_jsonl_atomic(
        path=path,
        label=RESEARCH_PROJECT_REGISTRY_HASH_LABEL,
        mutation=mutation,
    )
    project, row, created = result.value
    return ResearchProjectMutation(
        project=project,
        registry_path=path.resolve(),
        registry_row=row,
        created=created,
    )


def _validated_registry(
    manager: ResearchPathManager,
) -> tuple[dict[str, ResearchProject], tuple[Mapping[str, Any], ...]]:
    path = research_project_registry_path(manager)
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path,
            label=RESEARCH_PROJECT_REGISTRY_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise ResearchProjectIntegrityError(
            f"research_project_registry_invalid:{exc}"
        ) from exc
    if snapshot.status != "PASS":
        raise ResearchProjectIntegrityError(
            "research_project_registry_invalid:" + ",".join(snapshot.reasons)
        )
    return _replay_rows(snapshot)


def _replay_rows(
    snapshot: HashChainSnapshot | Any,
) -> tuple[dict[str, ResearchProject], tuple[Mapping[str, Any], ...]]:
    rows = tuple(snapshot.rows)
    projects: dict[str, ResearchProject] = {}
    event_ids: set[str] = set()
    events: list[Mapping[str, Any]] = []
    for index, row in enumerate(rows):
        try:
            payload = _strict_mapping(
                row,
                expected={
                    "schema_version",
                    "event_id",
                    "event_type",
                    "project_id",
                    "actor_id",
                    "recorded_at",
                    "reason",
                    "previous_project_hash",
                    "change",
                    "change_hash",
                    "project",
                    "sequence",
                    "prior_hash",
                    "row_hash",
                },
                label="research_project_registry_event",
            )
            event_id = _identifier(
                _string(payload["event_id"], "research_project_event_id"),
                "research_project_event_id",
            )
            if event_id in event_ids:
                raise ResearchProjectIntegrityError(
                    "research_project_event_id_duplicate"
                )
            event_ids.add(event_id)
            project_id = _identifier(
                _string(payload["project_id"], "research_project_id"),
                "research_project_id",
            )
            current = projects.get(project_id)
            _validate_event(payload, current=current)
            project = ResearchProject.from_dict(payload["project"])
            if (
                project.status is ResearchProjectStatus.SUPERSEDED
                and project.superseded_by_project_id not in projects
            ):
                raise ResearchProjectIntegrityError(
                    "research_project_superseded_target_not_found"
                )
            projects[project_id] = project
            events.append(payload)
        except (
            ResearchProjectAuthorizationError,
            ResearchProjectError,
            TypeError,
            ValueError,
        ) as exc:
            raise ResearchProjectIntegrityError(
                f"research_project_registry_semantic_invalid:{index}:{exc}"
            ) from exc
    return projects, tuple(events)


def _validate_event(
    row: Mapping[str, Any],
    *,
    current: ResearchProject | None,
) -> None:
    if row.get("schema_version") != RESEARCH_PROJECT_REGISTRY_SCHEMA_VERSION:
        raise ResearchProjectIntegrityError(
            "research_project_registry_schema_version_invalid"
        )
    event_type = ResearchProjectEventType(
        _string(row.get("event_type"), "research_project_event_type")
    )
    project_id = _identifier(
        _string(row.get("project_id"), "research_project_id"),
        "research_project_id",
    )
    actor_id = _identifier(
        _string(row.get("actor_id"), "research_project_actor_id"),
        "research_project_actor_id",
    )
    recorded_at = _string(row.get("recorded_at"), "research_project_event_recorded_at")
    recorded = _timestamp(
        recorded_at,
        "research_project_event_recorded_at",
    )
    _required_text(
        _string(row.get("reason"), "research_project_event_reason"),
        "research_project_event_reason",
        maximum=4_000,
    )
    change = _strict_string_mapping(row.get("change"), "research_project_event_change")
    if row.get("change_hash") != sha256_prefixed(
        change,
        label="research_project_event_change",
    ):
        raise ResearchProjectIntegrityError(
            "research_project_event_change_hash_mismatch"
        )
    project = ResearchProject.from_dict(row.get("project"))
    if project.project_id != project_id or project.updated_at != recorded_at:
        raise ResearchProjectIntegrityError(
            "research_project_event_project_binding_invalid"
        )
    if current is None:
        if (
            event_type is not ResearchProjectEventType.CREATED
            or row.get("previous_project_hash") is not None
            or project.version != 1
            or project.status is not ResearchProjectStatus.DRAFT
            or project.owner_id != actor_id
            or project.created_at != recorded_at
        ):
            raise ResearchProjectIntegrityError("research_project_create_event_invalid")
        expected_create_change = {"project": project.as_dict()}
        if canonical_json_bytes(change) != canonical_json_bytes(expected_create_change):
            raise ResearchProjectIntegrityError(
                "research_project_create_change_invalid"
            )
        return
    if event_type is ResearchProjectEventType.CREATED:
        raise ResearchProjectConflictError("research_project_duplicate")
    if row.get("previous_project_hash") != current.content_hash:
        raise ResearchProjectIntegrityError("research_project_previous_hash_mismatch")
    if project.version != current.version + 1:
        raise ResearchProjectIntegrityError("research_project_event_version_invalid")
    if project.created_at != current.created_at or project.owner_id != current.owner_id:
        raise ResearchProjectIntegrityError(
            "research_project_immutable_identity_changed"
        )
    if recorded < _timestamp(current.updated_at, "research_project_updated_at"):
        raise ResearchProjectIntegrityError("research_project_event_time_regressed")
    expected_version = change.get("expected_version")
    if expected_version != current.version:
        raise ResearchProjectIntegrityError(
            "research_project_change_expected_version_invalid"
        )
    if event_type is ResearchProjectEventType.MEMBERS_REPLACED:
        require_research_project_permission(
            current,
            actor_id=actor_id,
            permission=ResearchProjectPermission.MANAGE_MEMBERS,
        )
        _require_mutable(current)
        expected_member_change: Mapping[str, Any] = {
            "expected_version": current.version,
            "members": [member.as_dict() for member in project.members],
        }
        _assert_change(change, expected_member_change)
        _assert_only_project_fields_changed(
            current,
            project,
            allowed={"version", "members", "updated_at"},
        )
    elif event_type is ResearchProjectEventType.PROJECT_REVISED:
        require_research_project_permission(
            current,
            actor_id=actor_id,
            permission=ResearchProjectPermission.REVISE,
        )
        _require_mutable(current)
        expected_revision_change: Mapping[str, Any] = {
            "expected_version": current.version,
            "title": project.title,
            "research_question": project.research_question,
            "asset_classes": list(project.asset_classes),
            "markets": list(project.markets),
            "investment_horizon": project.investment_horizon,
            "expected_phenomenon": project.expected_phenomenon,
            "economic_explanation": project.economic_explanation,
            "prior_research_relationship": project.prior_research_relationship,
            "required_data": list(project.required_data),
            "expected_challenges": list(project.expected_challenges),
            "similar_research_assessment": project.similar_research_assessment,
            "similar_research_refs": [
                ref.as_dict() for ref in project.similar_research_refs
            ],
        }
        _assert_change(change, expected_revision_change)
        _assert_only_project_fields_changed(
            current,
            project,
            allowed={
                "version",
                "title",
                "research_question",
                "asset_classes",
                "markets",
                "investment_horizon",
                "expected_phenomenon",
                "economic_explanation",
                "prior_research_relationship",
                "required_data",
                "expected_challenges",
                "similar_research_assessment",
                "similar_research_refs",
                "updated_at",
            },
        )
    elif event_type is ResearchProjectEventType.REFERENCE_ATTACHED:
        _require_mutable(current)
        new_refs = [
            ref for ref in project.object_refs if ref not in current.object_refs
        ]
        removed_refs = [
            ref for ref in current.object_refs if ref not in project.object_refs
        ]
        if len(new_refs) != 1 or removed_refs:
            raise ResearchProjectIntegrityError(
                "research_project_reference_delta_invalid"
            )
        reference = new_refs[0]
        require_research_project_permission(
            current,
            actor_id=actor_id,
            permission=project_permission_for_reference(reference.kind),
        )
        expected_reference_change: Mapping[str, Any] = {
            "expected_version": current.version,
            "reference": reference.as_dict(),
        }
        _assert_change(change, expected_reference_change)
        _assert_only_project_fields_changed(
            current,
            project,
            allowed={"version", "object_refs", "updated_at"},
        )
    elif event_type is ResearchProjectEventType.STATUS_TRANSITIONED:
        require_research_project_permission(
            current,
            actor_id=actor_id,
            permission=ResearchProjectPermission.TRANSITION,
        )
        if project.status not in _ALLOWED_TRANSITIONS[current.status]:
            raise ResearchProjectIntegrityError(
                "research_project_status_transition_forbidden"
            )
        expected_transition_change: Mapping[str, Any] = {
            "expected_version": current.version,
            "to_status": project.status.value,
            "superseded_by_project_id": project.superseded_by_project_id,
        }
        _assert_change(change, expected_transition_change)
        _assert_only_project_fields_changed(
            current,
            project,
            allowed={
                "version",
                "status",
                "status_reason",
                "superseded_by_project_id",
                "updated_at",
            },
        )
    else:  # pragma: no cover - StrEnum construction makes this defensive.
        raise ResearchProjectIntegrityError("research_project_event_type_invalid")


def _assert_only_project_fields_changed(
    current: ResearchProject,
    project: ResearchProject,
    *,
    allowed: set[str],
) -> None:
    before = current._content_material()
    after = project._content_material()
    changed = {
        field
        for field in before
        if canonical_json_bytes(before[field]) != canonical_json_bytes(after[field])
    }
    changed.discard("artifact_type")
    if not changed.issubset(allowed):
        raise ResearchProjectIntegrityError(
            "research_project_event_changed_forbidden_fields:"
            + ",".join(sorted(changed - allowed))
        )


def _assert_change(
    actual: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    if canonical_json_bytes(actual) != canonical_json_bytes(expected):
        raise ResearchProjectIntegrityError("research_project_event_change_invalid")


def _create_project_snapshot(
    *,
    current: ResearchProject | None,
    project: ResearchProject,
) -> ResearchProject:
    if current is not None:
        raise ResearchProjectConflictError("research_project_duplicate")
    return project


def _required_current(
    current: ResearchProject | None,
) -> ResearchProject:
    if current is None:
        raise ResearchProjectNotFoundError("research_project_not_found")
    return current


def _require_expected_version(
    project: ResearchProject,
    expected_version: int,
) -> None:
    if project.version != _positive_version(expected_version):
        raise ResearchProjectConflictError("research_project_version_conflict")


def _require_mutable(project: ResearchProject) -> None:
    if project.status not in _MUTABLE_STATUSES:
        raise ResearchProjectConflictError(
            f"research_project_status_immutable:{project.status.value}"
        )


def _positive_version(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResearchProjectError("research_project_expected_version_invalid")
    return value


def _identifier(value: str, label: str) -> str:
    normalized = _required_text(value, label, maximum=255)
    if normalized[0] not in _IDENTIFIER_CHARACTERS or any(
        character not in _IDENTIFIER_CHARACTERS for character in normalized
    ):
        raise ResearchProjectError(f"{label}_invalid")
    return normalized


def _required_text(value: str, label: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ResearchProjectError(f"{label}_must_be_string")
    normalized = value.strip()
    if not normalized or normalized != value or len(normalized) > maximum:
        raise ResearchProjectError(f"{label}_invalid")
    return normalized


def _sha256(value: str, label: str) -> str:
    normalized = _required_text(value, label, maximum=_SHA256_PREFIX_LENGTH)
    if len(normalized) != _SHA256_PREFIX_LENGTH or not normalized.startswith("sha256:"):
        raise ResearchProjectError(f"{label}_invalid")
    try:
        int(normalized.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise ResearchProjectError(f"{label}_invalid") from exc
    return normalized


def _timestamp(value: str, label: str) -> datetime:
    normalized = _required_text(value, label, maximum=64)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchProjectError(f"{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchProjectError(f"{label}_timezone_required")
    return parsed


def _normalized_scope(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    normalized = tuple(_required_text(value, label, maximum=255) for value in values)
    if not normalized or len(set(normalized)) != len(normalized):
        raise ResearchProjectError(f"{label}_invalid")
    return tuple(sorted(normalized))


def _strict_mapping(
    value: object,
    *,
    expected: set[str],
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ResearchProjectError(f"{label}_object_required")
    if set(value) != expected:
        raise ResearchProjectError(f"{label}_fields_invalid")
    return cast(Mapping[str, Any], value)


def _strict_string_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ResearchProjectError(f"{label}_object_required")
    return cast(Mapping[str, Any], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ResearchProjectError(f"{label}_must_be_string")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ResearchProjectError(f"{label}_list_required")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    values = _list(value, label)
    if any(not isinstance(item, str) for item in values):
        raise ResearchProjectError(f"{label}_strings_required")
    return tuple(cast(list[str], values))


def _require_external_path(
    manager: ResearchPathManager,
    path: Path,
    label: str,
    *,
    allowed_root: Path,
) -> None:
    lexical_path = path.absolute()
    lexical_root = allowed_root.absolute()
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise ResearchPathError(f"{label}_outside_configured_root:{path}") from exc
    current = lexical_root
    for part in relative.parts:
        current /= part
        try:
            status = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ResearchPathError(f"{label}_path_unavailable:{current}") from exc
        if stat.S_ISLNK(status.st_mode):
            raise ResearchPathError(f"{label}_symlink_component_forbidden:{current}")
    resolved = path.resolve()
    resolved_root = allowed_root.resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ResearchPathError(f"{label}_outside_configured_root:{resolved}") from exc
    if ResearchPathManager.is_within(resolved, manager.project_root):
        raise ResearchPathError(f"{label}_must_be_repository_external:{resolved}")


__all__ = [
    "RESEARCH_PROJECT_ARTIFACT_TYPE",
    "RESEARCH_PROJECT_REGISTRY_HASH_LABEL",
    "RESEARCH_PROJECT_REGISTRY_SCHEMA_VERSION",
    "RESEARCH_PROJECT_SCHEMA_VERSION",
    "ResearchProject",
    "ResearchProjectAuthorizationError",
    "ResearchProjectConflictError",
    "ResearchProjectError",
    "ResearchProjectEventType",
    "ResearchProjectIntegrityError",
    "ResearchProjectMember",
    "ResearchProjectMutation",
    "ResearchProjectNamespaces",
    "ResearchProjectNotFoundError",
    "ResearchProjectObjectKind",
    "ResearchProjectObjectRef",
    "ResearchProjectPermission",
    "ResearchProjectRole",
    "ResearchProjectStatus",
    "attach_research_project_reference",
    "create_or_verify_research_project",
    "get_research_project",
    "has_research_project_permission",
    "impacted_research_projects",
    "project_permission_for_reference",
    "replace_research_project_members",
    "require_research_project_permission",
    "research_project_history",
    "research_project_namespaces",
    "research_project_registry_path",
    "revise_research_project",
    "search_research_projects",
    "transition_research_project",
    "validate_research_project_registry",
]
