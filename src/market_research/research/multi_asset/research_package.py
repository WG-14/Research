"""Resolvable evidence references and immutable multi-asset run manifests.

This module owns the file boundary for the multi-asset application service.
Evidence references are not trusted merely because they contain plausible
hashes: the resolver opens a bounded, repository-external JSON artifact,
recomputes its byte and schema hashes, and checks its logical identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Sequence, cast
from urllib.parse import unquote, urlsplit

from market_research.paths import ResearchPathManager
from market_research.research.multi_asset.evidence import evidence_hash
from market_research.storage_io import write_json_atomic_create_or_verify


MULTI_ASSET_RUN_MANIFEST_SCHEMA_VERSION = 1
EVIDENCE_ARTIFACT_ENVELOPE_SCHEMA: Mapping[str, object] = {
    "schema_id": "market-research.multi-asset-evidence-artifact",
    "schema_version": 1,
    "required": [
        "schema",
        "role",
        "logical_id",
        "version",
        "quality_flags",
        "payload",
    ],
}
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_QUALITY_FLAG = re.compile(r"^[A-Z][A-Z0-9_:-]{0,127}$")
_ENVELOPE_KEYS = frozenset(
    {"schema", "role", "logical_id", "version", "quality_flags", "payload"}
)


class MultiAssetResearchPackageError(ValueError):
    """A research-package reference or manifest is invalid."""


class EvidenceArtifactRole(StrEnum):
    DATASET = "DATASET"
    PRODUCT_REGISTRY = "PRODUCT_REGISTRY"
    MARKET_STATE = "MARKET_STATE"
    HYPOTHESIS = "HYPOTHESIS"
    POLICY = "POLICY"
    CODE = "CODE"
    ENVIRONMENT = "ENVIRONMENT"
    CONFIGURATION = "CONFIGURATION"


class RunStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def _unique_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise MultiAssetResearchPackageError(
                f"evidence_artifact_duplicate_json_key:{key}"
            )
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise MultiAssetResearchPackageError(
        f"evidence_artifact_nonfinite_json_constant:{value}"
    )


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise MultiAssetResearchPackageError(
            f"{field_name}_must_be_nonempty_and_trimmed"
        )


def _require_stable_id(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise MultiAssetResearchPackageError(f"{field_name}_invalid")


def _require_hash(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise MultiAssetResearchPackageError(f"{field_name}_invalid")


def _timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise MultiAssetResearchPackageError(f"{field_name}_invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise MultiAssetResearchPackageError(f"{field_name}_invalid_timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise MultiAssetResearchPackageError(f"{field_name}_timezone_required")
    return result.astimezone(UTC)


def _quality_flags(
    values: Sequence[str],
    *,
    field_name: str,
) -> tuple[str, ...]:
    result = tuple(values)
    if not result:
        raise MultiAssetResearchPackageError(f"{field_name}_required")
    if result != tuple(sorted(set(result))):
        raise MultiAssetResearchPackageError(f"{field_name}_must_be_sorted_and_unique")
    if any(not _QUALITY_FLAG.fullmatch(value) for value in result):
        raise MultiAssetResearchPackageError(f"{field_name}_invalid")
    return result


def _required_payload_fields(
    payload: Mapping[str, object],
    fields: frozenset[str],
    *,
    role: EvidenceArtifactRole,
) -> None:
    missing = fields.difference(payload)
    if missing:
        raise MultiAssetResearchPackageError(
            f"evidence_artifact_{role.value.lower()}_payload_incomplete"
        )


def _validate_role_payload(
    role: EvidenceArtifactRole,
    payload: Mapping[str, object],
) -> None:
    """Validate the minimum authoritative contract for each evidence role."""

    if role is EvidenceArtifactRole.DATASET:
        _required_payload_fields(
            payload,
            frozenset(
                {
                    "artifact_kind",
                    "data_version",
                    "snapshot_hash",
                    "source_schema_hash",
                    "row_count",
                    "event_start_at",
                    "event_end_at",
                    "knowledge_cutoff_at",
                }
            ),
            role=role,
        )
        if payload["artifact_kind"] != "IMMUTABLE_DATASET_SNAPSHOT":
            raise MultiAssetResearchPackageError(
                "evidence_artifact_dataset_kind_invalid"
            )
        _require_stable_id(
            payload["data_version"],
            "evidence_artifact.dataset.data_version",
        )
        _require_hash(
            payload["snapshot_hash"],
            "evidence_artifact.dataset.snapshot_hash",
        )
        _require_hash(
            payload["source_schema_hash"],
            "evidence_artifact.dataset.source_schema_hash",
        )
        row_count = payload["row_count"]
        if (
            isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count < 0
        ):
            raise MultiAssetResearchPackageError(
                "evidence_artifact_dataset_row_count_invalid"
            )
        event_start = _timestamp(
            payload["event_start_at"],
            "evidence_artifact.dataset.event_start_at",
        )
        event_end = _timestamp(
            payload["event_end_at"],
            "evidence_artifact.dataset.event_end_at",
        )
        knowledge_cutoff = _timestamp(
            payload["knowledge_cutoff_at"],
            "evidence_artifact.dataset.knowledge_cutoff_at",
        )
        if event_end < event_start:
            raise MultiAssetResearchPackageError(
                "evidence_artifact_dataset_event_range_invalid"
            )
        if knowledge_cutoff < event_end:
            raise MultiAssetResearchPackageError(
                "evidence_artifact_dataset_knowledge_before_event_end"
            )
        return

    if role is EvidenceArtifactRole.PRODUCT_REGISTRY:
        _required_payload_fields(
            payload,
            frozenset(
                {
                    "artifact_kind",
                    "registry_hash",
                    "schema_version",
                    "effective_as_of",
                    "knowledge_at",
                }
            ),
            role=role,
        )
        if payload["artifact_kind"] != "PRODUCT_MASTER_SNAPSHOT":
            raise MultiAssetResearchPackageError(
                "evidence_artifact_product_registry_kind_invalid"
            )
        _require_hash(
            payload["registry_hash"],
            "evidence_artifact.product_registry.registry_hash",
        )
        schema_version = payload["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version < 1
        ):
            raise MultiAssetResearchPackageError(
                "evidence_artifact_product_registry_schema_version_invalid"
            )
        _timestamp(
            payload["effective_as_of"],
            "evidence_artifact.product_registry.effective_as_of",
        )
        _timestamp(
            payload["knowledge_at"],
            "evidence_artifact.product_registry.knowledge_at",
        )
        return

    if role is EvidenceArtifactRole.MARKET_STATE:
        _required_payload_fields(
            payload,
            frozenset(
                {
                    "artifact_kind",
                    "market_state_hash",
                    "state_id",
                    "valuation_at",
                    "maximum_knowledge_at",
                    "base_currency",
                    "calendar_ids",
                }
            ),
            role=role,
        )
        if payload["artifact_kind"] != "MARKET_STATE_SNAPSHOT":
            raise MultiAssetResearchPackageError(
                "evidence_artifact_market_state_kind_invalid"
            )
        _require_hash(
            payload["market_state_hash"],
            "evidence_artifact.market_state.market_state_hash",
        )
        _require_stable_id(
            payload["state_id"],
            "evidence_artifact.market_state.state_id",
        )
        valuation_at = _timestamp(
            payload["valuation_at"],
            "evidence_artifact.market_state.valuation_at",
        )
        maximum_knowledge_at = _timestamp(
            payload["maximum_knowledge_at"],
            "evidence_artifact.market_state.maximum_knowledge_at",
        )
        if maximum_knowledge_at > valuation_at:
            raise MultiAssetResearchPackageError(
                "evidence_artifact_market_state_future_knowledge"
            )
        currency = payload["base_currency"]
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            raise MultiAssetResearchPackageError(
                "evidence_artifact_market_state_currency_invalid"
            )
        calendar_ids = payload["calendar_ids"]
        if (
            not isinstance(calendar_ids, list)
            or not calendar_ids
            or any(
                not isinstance(item, str) or not _STABLE_ID.fullmatch(item)
                for item in calendar_ids
            )
            or calendar_ids != sorted(set(calendar_ids))
        ):
            raise MultiAssetResearchPackageError(
                "evidence_artifact_market_state_calendars_invalid"
            )


def evidence_artifact_schema_hash(
    schema: Mapping[str, object] = EVIDENCE_ARTIFACT_ENVELOPE_SCHEMA,
) -> str:
    """Return the canonical hash a reference must bind for its JSON schema."""

    return evidence_hash(schema, label="multi-asset-evidence-artifact-schema")


def encode_evidence_artifact(
    *,
    role: EvidenceArtifactRole,
    logical_id: str,
    version: str,
    quality_flags: Sequence[str],
    payload: Mapping[str, object],
    schema: Mapping[str, object] = EVIDENCE_ARTIFACT_ENVELOPE_SCHEMA,
) -> bytes:
    """Encode one canonical immutable input artifact.

    This is a codec, not an input writer.  Dataset preparation remains outside
    Research; callers may persist these bytes under an approved external root.
    """

    if not isinstance(role, EvidenceArtifactRole):
        raise MultiAssetResearchPackageError("evidence_artifact.role_invalid")
    if dict(schema) != dict(EVIDENCE_ARTIFACT_ENVELOPE_SCHEMA):
        raise MultiAssetResearchPackageError(
            "evidence_artifact_schema_not_authoritative"
        )
    _require_stable_id(logical_id, "evidence_artifact.logical_id")
    _require_stable_id(version, "evidence_artifact.version")
    flags = _quality_flags(
        quality_flags,
        field_name="evidence_artifact.quality_flags",
    )
    _validate_role_payload(role, payload)
    envelope = {
        "schema": dict(schema),
        "role": role.value,
        "logical_id": logical_id,
        "version": version,
        "quality_flags": list(flags),
        "payload": dict(payload),
    }
    return (
        json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def bytes_sha256(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True, slots=True)
class EvidenceArtifactRef:
    """A logical reference whose URI, content, and schema are all bound."""

    role: EvidenceArtifactRole
    logical_id: str
    version: str
    uri: str
    content_hash: str
    schema_hash: str
    byte_length: int

    def __post_init__(self) -> None:
        if not isinstance(self.role, EvidenceArtifactRole):
            raise MultiAssetResearchPackageError("evidence_ref.role_invalid")
        _require_stable_id(self.logical_id, "evidence_ref.logical_id")
        _require_stable_id(self.version, "evidence_ref.version")
        _require_text(self.uri, "evidence_ref.uri")
        _require_hash(self.content_hash, "evidence_ref.content_hash")
        _require_hash(self.schema_hash, "evidence_ref.schema_hash")
        if self.schema_hash != evidence_artifact_schema_hash():
            raise MultiAssetResearchPackageError(
                "evidence_ref_schema_not_authoritative"
            )
        if (
            isinstance(self.byte_length, bool)
            or not isinstance(self.byte_length, int)
            or self.byte_length <= 0
        ):
            raise MultiAssetResearchPackageError("evidence_ref.byte_length_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "logical_id": self.logical_id,
            "version": self.version,
            "uri": self.uri,
            "content_hash": self.content_hash,
            "schema_hash": self.schema_hash,
            "byte_length": self.byte_length,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "EvidenceArtifactRef":
        expected = {
            "role",
            "logical_id",
            "version",
            "uri",
            "content_hash",
            "schema_hash",
            "byte_length",
        }
        if set(payload) != expected:
            raise MultiAssetResearchPackageError("evidence_ref.fields_invalid")
        string_fields = (
            "role",
            "logical_id",
            "version",
            "uri",
            "content_hash",
            "schema_hash",
        )
        if any(not isinstance(payload[field], str) for field in string_fields):
            raise MultiAssetResearchPackageError("evidence_ref.string_fields_invalid")
        strings = {field: cast(str, payload[field]) for field in string_fields}
        try:
            role = EvidenceArtifactRole(strings["role"])
        except ValueError as exc:
            raise MultiAssetResearchPackageError("evidence_ref.role_invalid") from exc
        byte_length = payload["byte_length"]
        if isinstance(byte_length, bool) or not isinstance(byte_length, int):
            raise MultiAssetResearchPackageError("evidence_ref.byte_length_invalid")
        return cls(
            role=role,
            logical_id=strings["logical_id"],
            version=strings["version"],
            uri=strings["uri"],
            content_hash=strings["content_hash"],
            schema_hash=strings["schema_hash"],
            byte_length=byte_length,
        )


@dataclass(frozen=True, slots=True)
class ResolvedEvidenceArtifact:
    reference: EvidenceArtifactRef
    quality_flags: tuple[str, ...]
    payload_json: str
    verified_at: str

    def __post_init__(self) -> None:
        _quality_flags(
            self.quality_flags,
            field_name="resolved_evidence.quality_flags",
        )
        _timestamp(self.verified_at, "resolved_evidence.verified_at")
        try:
            payload = json.loads(self.payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise MultiAssetResearchPackageError(
                "resolved_evidence.payload_invalid"
            ) from exc
        if not isinstance(payload, dict):
            raise MultiAssetResearchPackageError(
                "resolved_evidence.payload_object_required"
            )

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):  # pragma: no cover - guarded at creation.
            raise MultiAssetResearchPackageError(
                "resolved_evidence.payload_object_required"
            )
        return value

    def as_dict(self) -> dict[str, object]:
        return {
            **self.reference.as_dict(),
            "quality_flags": list(self.quality_flags),
            "verified_at": self.verified_at,
        }


@dataclass(frozen=True, slots=True)
class BoundedEvidenceArtifactResolver:
    """Resolve a small immutable set of external JSON files, fail closed."""

    allowed_roots: tuple[Path, ...]
    project_root: Path
    max_artifacts: int = 64
    max_artifact_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if not self.allowed_roots:
            raise MultiAssetResearchPackageError("resolver.allowed_roots_required")
        project_root = self.project_root.expanduser().resolve()
        roots: list[Path] = []
        for root in self.allowed_roots:
            expanded = root.expanduser()
            if not expanded.is_absolute():
                raise MultiAssetResearchPackageError(
                    "resolver.allowed_root_absolute_required"
                )
            resolved = expanded.resolve()
            if ResearchPathManager.is_within(resolved, project_root):
                raise MultiAssetResearchPackageError(
                    "resolver.allowed_root_repository_external_required"
                )
            roots.append(resolved)
        if len(roots) != len(set(roots)):
            raise MultiAssetResearchPackageError("resolver.allowed_roots_duplicate")
        if (
            self.max_artifacts <= 0
            or self.max_artifact_bytes <= 0
            or self.max_total_bytes <= 0
            or self.max_total_bytes < self.max_artifact_bytes
        ):
            raise MultiAssetResearchPackageError("resolver.bounds_invalid")
        object.__setattr__(self, "project_root", project_root)
        object.__setattr__(self, "allowed_roots", tuple(roots))

    @classmethod
    def from_paths(
        cls,
        paths: ResearchPathManager,
        *,
        max_artifacts: int = 64,
        max_artifact_bytes: int = 16 * 1024 * 1024,
        max_total_bytes: int = 64 * 1024 * 1024,
    ) -> "BoundedEvidenceArtifactResolver":
        return cls(
            allowed_roots=(
                paths.data_root,
                paths.artifact_root,
                paths.report_root,
            ),
            project_root=paths.project_root,
            max_artifacts=max_artifacts,
            max_artifact_bytes=max_artifact_bytes,
            max_total_bytes=max_total_bytes,
        )

    def resolve_all(
        self,
        references: Sequence[EvidenceArtifactRef],
        *,
        verified_at: str,
    ) -> tuple[ResolvedEvidenceArtifact, ...]:
        refs = tuple(references)
        if not refs:
            raise MultiAssetResearchPackageError("evidence_refs_required")
        if len(refs) > self.max_artifacts:
            raise MultiAssetResearchPackageError("evidence_ref_limit_exceeded")
        if sum(item.byte_length for item in refs) > self.max_total_bytes:
            raise MultiAssetResearchPackageError("evidence_total_byte_limit_exceeded")
        identities = [(item.role.value, item.logical_id, item.version) for item in refs]
        uris = [item.uri for item in refs]
        if len(identities) != len(set(identities)):
            raise MultiAssetResearchPackageError("evidence_ref_identity_duplicate")
        if len(uris) != len(set(uris)):
            raise MultiAssetResearchPackageError("evidence_ref_uri_duplicate")
        return tuple(self.resolve(item, verified_at=verified_at) for item in refs)

    def resolve(
        self,
        reference: EvidenceArtifactRef,
        *,
        verified_at: str,
    ) -> ResolvedEvidenceArtifact:
        _timestamp(verified_at, "resolver.verified_at")
        path = self._path_for(reference)
        raw = self._read_bounded(path)
        if len(raw) != reference.byte_length:
            raise MultiAssetResearchPackageError(
                "evidence_artifact_byte_length_mismatch"
            )
        if bytes_sha256(raw) != reference.content_hash:
            raise MultiAssetResearchPackageError(
                "evidence_artifact_content_hash_mismatch"
            )
        try:
            envelope = json.loads(
                raw,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MultiAssetResearchPackageError(
                "evidence_artifact_json_invalid"
            ) from exc
        if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_KEYS:
            raise MultiAssetResearchPackageError("evidence_artifact_envelope_invalid")
        schema = envelope["schema"]
        if not isinstance(schema, dict):
            raise MultiAssetResearchPackageError("evidence_artifact_schema_invalid")
        if schema != dict(EVIDENCE_ARTIFACT_ENVELOPE_SCHEMA):
            raise MultiAssetResearchPackageError(
                "evidence_artifact_schema_not_authoritative"
            )
        if evidence_artifact_schema_hash(schema) != reference.schema_hash:
            raise MultiAssetResearchPackageError(
                "evidence_artifact_schema_hash_mismatch"
            )
        if envelope["role"] != reference.role.value:
            raise MultiAssetResearchPackageError("evidence_artifact_role_mismatch")
        if envelope["logical_id"] != reference.logical_id:
            raise MultiAssetResearchPackageError(
                "evidence_artifact_logical_id_mismatch"
            )
        if envelope["version"] != reference.version:
            raise MultiAssetResearchPackageError("evidence_artifact_version_mismatch")
        raw_flags = envelope["quality_flags"]
        if not isinstance(raw_flags, list) or any(
            not isinstance(item, str) for item in raw_flags
        ):
            raise MultiAssetResearchPackageError(
                "evidence_artifact_quality_flags_invalid"
            )
        flags = _quality_flags(
            raw_flags,
            field_name="evidence_artifact.quality_flags",
        )
        payload = envelope["payload"]
        if not isinstance(payload, dict):
            raise MultiAssetResearchPackageError(
                "evidence_artifact_payload_object_required"
            )
        _validate_role_payload(reference.role, payload)
        return ResolvedEvidenceArtifact(
            reference=reference,
            quality_flags=flags,
            payload_json=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            verified_at=verified_at,
        )

    def _path_for(self, reference: EvidenceArtifactRef) -> Path:
        parsed = urlsplit(reference.uri)
        if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
            raise MultiAssetResearchPackageError("evidence_ref_file_uri_required")
        path = Path(unquote(parsed.path))
        if not path.is_absolute():
            raise MultiAssetResearchPackageError("evidence_ref_absolute_uri_required")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise MultiAssetResearchPackageError("evidence_artifact_not_found") from exc
        if resolved != path:
            raise MultiAssetResearchPackageError(
                "evidence_artifact_symlink_or_noncanonical_path"
            )
        if reference.uri != resolved.as_uri():
            raise MultiAssetResearchPackageError("evidence_ref_noncanonical_file_uri")
        if ResearchPathManager.is_within(resolved, self.project_root):
            raise MultiAssetResearchPackageError(
                "evidence_artifact_repository_external_required"
            )
        if not any(
            ResearchPathManager.is_within(resolved, root) for root in self.allowed_roots
        ):
            raise MultiAssetResearchPackageError(
                "evidence_artifact_outside_allowed_roots"
            )
        return resolved

    def _read_bounded(self, path: Path) -> bytes:
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise MultiAssetResearchPackageError(
                "evidence_artifact_no_follow_unavailable"
            )
        try:
            descriptor = os.open(path, os.O_RDONLY | no_follow)
        except OSError as exc:
            raise MultiAssetResearchPackageError(
                "evidence_artifact_open_failed"
            ) from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise MultiAssetResearchPackageError(
                    "evidence_artifact_regular_file_required"
                )
            if before.st_size <= 0 or before.st_size > self.max_artifact_bytes:
                raise MultiAssetResearchPackageError("evidence_artifact_size_invalid")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    raise MultiAssetResearchPackageError("evidence_artifact_short_read")
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if (
                before.st_ino,
                before.st_dev,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_ino,
                after.st_dev,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise MultiAssetResearchPackageError(
                    "evidence_artifact_changed_during_read"
                )
            return b"".join(chunks)
        finally:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class ArtifactChecksum:
    logical_id: str
    uri: str
    content_hash: str
    byte_length: int

    def __post_init__(self) -> None:
        _require_stable_id(self.logical_id, "artifact_checksum.logical_id")
        _require_text(self.uri, "artifact_checksum.uri")
        _require_hash(self.content_hash, "artifact_checksum.content_hash")
        if (
            isinstance(self.byte_length, bool)
            or not isinstance(self.byte_length, int)
            or self.byte_length <= 0
        ):
            raise MultiAssetResearchPackageError(
                "artifact_checksum.byte_length_invalid"
            )

    @classmethod
    def from_path(cls, logical_id: str, path: Path) -> "ArtifactChecksum":
        resolved = path.expanduser().resolve(strict=True)
        raw = resolved.read_bytes()
        return cls(
            logical_id=logical_id,
            uri=resolved.as_uri(),
            content_hash=bytes_sha256(raw),
            byte_length=len(raw),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "logical_id": self.logical_id,
            "uri": self.uri,
            "content_hash": self.content_hash,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True, slots=True)
class RuntimeEnvironment:
    git_commit: str
    dirty_worktree: bool
    working_tree_hash: str
    python_version: str
    python_implementation: str
    platform: str
    dependency_versions: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.git_commit, "runtime.git_commit")
        _require_hash(
            self.working_tree_hash,
            "runtime.working_tree_hash",
        )
        _require_text(self.python_version, "runtime.python_version")
        _require_text(
            self.python_implementation,
            "runtime.python_implementation",
        )
        _require_text(self.platform, "runtime.platform")
        if self.dependency_versions != tuple(sorted(set(self.dependency_versions))):
            raise MultiAssetResearchPackageError(
                "runtime.dependency_versions_must_be_sorted_and_unique"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "git_commit": self.git_commit,
            "dirty_worktree": self.dirty_worktree,
            "working_tree_hash": self.working_tree_hash,
            "python_version": self.python_version,
            "python_implementation": self.python_implementation,
            "platform": self.platform,
            "dependency_versions": list(self.dependency_versions),
        }

    @classmethod
    def basic(
        cls,
        *,
        git_commit: str,
        dirty_worktree: bool,
        working_tree_hash: str,
        dependency_versions: Sequence[str],
    ) -> "RuntimeEnvironment":
        return cls(
            git_commit=git_commit,
            dirty_worktree=dirty_worktree,
            working_tree_hash=working_tree_hash,
            python_version=platform.python_version(),
            python_implementation=platform.python_implementation(),
            platform=platform.platform(),
            dependency_versions=tuple(sorted(set(dependency_versions))),
        )


@dataclass(frozen=True, slots=True)
class MultiAssetRunManifest:
    run_id: str
    experiment_id: str
    experiment_spec_hash: str
    started_at: str
    finished_at: str
    status: RunStatus
    runtime: RuntimeEnvironment
    command: tuple[str, ...]
    evidence_references: tuple[EvidenceArtifactRef, ...]
    evidence_artifacts: tuple[ResolvedEvidenceArtifact, ...]
    artifact_checksums: tuple[ArtifactChecksum, ...]
    study_content_hash: str | None
    failure_code: str | None = None
    failure_message_hash: str | None = None
    content_hash: str = field(init=False)
    schema_version: int = MULTI_ASSET_RUN_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MULTI_ASSET_RUN_MANIFEST_SCHEMA_VERSION:
            raise MultiAssetResearchPackageError("run_manifest.schema_version_invalid")
        _require_stable_id(self.run_id, "run_manifest.run_id")
        _require_stable_id(self.experiment_id, "run_manifest.experiment_id")
        _require_hash(
            self.experiment_spec_hash,
            "run_manifest.experiment_spec_hash",
        )
        if _timestamp(self.finished_at, "run_manifest.finished_at") < _timestamp(
            self.started_at,
            "run_manifest.started_at",
        ):
            raise MultiAssetResearchPackageError("run_manifest.time_order_invalid")
        if not isinstance(self.status, RunStatus):
            raise MultiAssetResearchPackageError("run_manifest.status_invalid")
        if not self.command or any(
            not item or item.strip() != item for item in self.command
        ):
            raise MultiAssetResearchPackageError("run_manifest.command_invalid")
        requested_identities = [
            (item.role.value, item.logical_id, item.version)
            for item in self.evidence_references
        ]
        requested_uris = [item.uri for item in self.evidence_references]
        if (
            not requested_identities
            or len(requested_identities) != len(set(requested_identities))
            or len(requested_uris) != len(set(requested_uris))
        ):
            raise MultiAssetResearchPackageError(
                "run_manifest.evidence_references_invalid"
            )
        resolved_identities = [
            (
                item.reference.role.value,
                item.reference.logical_id,
                item.reference.version,
            )
            for item in self.evidence_artifacts
        ]
        if len(resolved_identities) != len(set(resolved_identities)) or not set(
            resolved_identities
        ).issubset(set(requested_identities)):
            raise MultiAssetResearchPackageError(
                "run_manifest.evidence_artifacts_invalid"
            )
        checksum_ids = [item.logical_id for item in self.artifact_checksums]
        if checksum_ids != sorted(set(checksum_ids)):
            raise MultiAssetResearchPackageError(
                "run_manifest.artifact_checksums_invalid"
            )
        succeeded = self.status is RunStatus.SUCCEEDED
        if succeeded:
            if (
                self.study_content_hash is None
                or self.failure_code is not None
                or self.failure_message_hash is not None
                or not self.artifact_checksums
                or set(resolved_identities) != set(requested_identities)
            ):
                raise MultiAssetResearchPackageError(
                    "run_manifest.success_fields_invalid"
                )
            _require_hash(
                self.study_content_hash,
                "run_manifest.study_content_hash",
            )
        else:
            if (
                self.study_content_hash is not None
                or self.failure_code is None
                or self.failure_message_hash is None
                or self.artifact_checksums
            ):
                raise MultiAssetResearchPackageError(
                    "run_manifest.failure_fields_invalid"
                )
            _require_stable_id(
                self.failure_code,
                "run_manifest.failure_code",
            )
            _require_hash(
                self.failure_message_hash,
                "run_manifest.failure_message_hash",
            )
        object.__setattr__(
            self,
            "content_hash",
            evidence_hash(
                self.identity_payload(),
                label="multi-asset-run-manifest",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "experiment_id": self.experiment_id,
            "experiment_spec_hash": self.experiment_spec_hash,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status.value,
            "runtime": self.runtime.as_dict(),
            "command": list(self.command),
            "evidence_references": [
                item.as_dict() for item in self.evidence_references
            ],
            "evidence_artifacts": [item.as_dict() for item in self.evidence_artifacts],
            "artifact_checksums": [item.as_dict() for item in self.artifact_checksums],
            "study_content_hash": self.study_content_hash,
            "failure_code": self.failure_code,
            "failure_message_hash": self.failure_message_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class PublishedRunManifest:
    path: Path
    content_hash: str
    created: bool


@dataclass(frozen=True, slots=True)
class PublishedRunClaim:
    path: Path
    content_hash: str
    created: bool


def reserve_run_id(
    *,
    run_id: str,
    experiment_id: str,
    experiment_spec_hash: str,
    command: Sequence[str],
    evidence_references: Sequence[EvidenceArtifactRef],
    paths: ResearchPathManager,
) -> PublishedRunClaim:
    """Atomically reserve a unique run id before economic work begins."""

    _require_stable_id(run_id, "run_claim.run_id")
    _require_stable_id(experiment_id, "run_claim.experiment_id")
    _require_hash(experiment_spec_hash, "run_claim.experiment_spec_hash")
    if not command or any(
        not isinstance(item, str) or not item or item.strip() != item
        for item in command
    ):
        raise MultiAssetResearchPackageError("run_claim.command_invalid")
    references = tuple(evidence_references)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "experiment_id": experiment_id,
        "experiment_spec_hash": experiment_spec_hash,
        "command": list(command),
        "evidence_references": [
            item.as_dict()
            for item in sorted(
                references,
                key=lambda item: (
                    item.role.value,
                    item.logical_id,
                    item.version,
                ),
            )
        ],
    }
    content_hash = evidence_hash(
        payload,
        label="multi-asset-run-claim",
    )
    path = paths.research_artifact_path(
        experiment_id,
        f"{run_id}.run_claim.json",
    )
    if not path.is_absolute() or ResearchPathManager.is_within(
        path, paths.project_root
    ):
        raise MultiAssetResearchPackageError(
            "run_claim.repository_external_path_required"
        )
    try:
        created = write_json_atomic_create_or_verify(
            path,
            {**payload, "content_hash": content_hash},
        )
    except ValueError as exc:
        raise MultiAssetResearchPackageError("run_id_reservation_conflict") from exc
    return PublishedRunClaim(
        path=path,
        content_hash=content_hash,
        created=created,
    )


def publish_run_manifest(
    manifest: MultiAssetRunManifest,
    *,
    paths: ResearchPathManager,
) -> PublishedRunManifest:
    path = paths.research_artifact_path(
        manifest.experiment_id,
        f"{manifest.run_id}.run_manifest.json",
    )
    if not path.is_absolute() or ResearchPathManager.is_within(
        path, paths.project_root
    ):
        raise MultiAssetResearchPackageError(
            "run_manifest.repository_external_path_required"
        )
    created = write_json_atomic_create_or_verify(path, manifest.as_dict())
    return PublishedRunManifest(
        path=path,
        content_hash=manifest.content_hash,
        created=created,
    )


def publish_failure_run_manifest(
    manifest: MultiAssetRunManifest,
    *,
    paths: ResearchPathManager,
) -> PublishedRunManifest:
    """Publish a failed attempt without colliding with a prior run outcome."""

    if manifest.status is not RunStatus.FAILED:
        raise MultiAssetResearchPackageError("failure_run_manifest_status_required")
    hash_suffix = manifest.content_hash.removeprefix("sha256:")[:16]
    path = paths.research_artifact_path(
        manifest.experiment_id,
        (f"{manifest.run_id}.failure.{hash_suffix}.run_manifest.json"),
    )
    if not path.is_absolute() or ResearchPathManager.is_within(
        path, paths.project_root
    ):
        raise MultiAssetResearchPackageError(
            "run_manifest.repository_external_path_required"
        )
    created = write_json_atomic_create_or_verify(path, manifest.as_dict())
    return PublishedRunManifest(
        path=path,
        content_hash=manifest.content_hash,
        created=created,
    )


__all__ = [
    "ArtifactChecksum",
    "BoundedEvidenceArtifactResolver",
    "EVIDENCE_ARTIFACT_ENVELOPE_SCHEMA",
    "EvidenceArtifactRef",
    "EvidenceArtifactRole",
    "MULTI_ASSET_RUN_MANIFEST_SCHEMA_VERSION",
    "MultiAssetResearchPackageError",
    "MultiAssetRunManifest",
    "PublishedRunClaim",
    "PublishedRunManifest",
    "ResolvedEvidenceArtifact",
    "RunStatus",
    "RuntimeEnvironment",
    "bytes_sha256",
    "encode_evidence_artifact",
    "evidence_artifact_schema_hash",
    "publish_failure_run_manifest",
    "publish_run_manifest",
    "reserve_run_id",
]
