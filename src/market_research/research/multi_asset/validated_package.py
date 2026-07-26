"""Portable, content-addressed multi-asset research packages.

The package is a directory, not an environment-specific archive.  Every
research object is stored below ``objects/sha256`` and addressed only through
relative paths.  The bundled verifier and replay program use the Python
standard library and therefore run from an empty cold root without importing
``market_research`` or consulting Git, a virtual environment, caches, prior
results, or live external state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence, cast

from market_research.paths import ResearchPathManager
from market_research.research.multi_asset.cards import DataCard, ModelCard
from market_research.research.multi_asset.evidence_graph import (
    EvidenceGraph,
    EvidenceNodeKind,
)
from market_research.research.multi_asset import portable_runtime as _portable_runtime


VALIDATED_PACKAGE_SCHEMA_VERSION = 1
PORTABLE_REPLAY_ALGORITHM_VERSION = "multi-asset-portable-replay-v1"
PACKAGE_BUILD_DESCRIPTOR_SCHEMA_VERSION = 1

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_QUALITY_FLAG = re.compile(r"^[A-Z][A-Z0-9_:-]{0,127}$")
_MAX_COMPONENT_BYTES = 64 * 1024 * 1024


class ValidatedPackageError(ValueError):
    """A portable research package is incomplete, unsafe, or tampered."""


class PackageArtifactRole(StrEnum):
    REQUEST = "REQUEST"
    SPEC = "SPEC"
    IMMUTABLE_INPUT = "IMMUTABLE_INPUT"
    DATA_CARD = "DATA_CARD"
    MODEL_CARD = "MODEL_CARD"
    POLICY = "POLICY"
    CONFIGURATION = "CONFIGURATION"
    SOURCE_IDENTITY = "SOURCE_IDENTITY"
    DEPENDENCY_IDENTITY = "DEPENDENCY_IDENTITY"
    RUNTIME_IDENTITY = "RUNTIME_IDENTITY"
    NORMALIZED_EVIDENCE = "NORMALIZED_EVIDENCE"
    DERIVED_EVIDENCE = "DERIVED_EVIDENCE"
    ACCOUNTING = "ACCOUNTING"
    EVIDENCE_GRAPH = "EVIDENCE_GRAPH"
    QUALITY_FLAGS = "QUALITY_FLAGS"
    CHECKSUMS = "CHECKSUMS"
    STUDY = "STUDY"
    REPORT = "REPORT"


_GENERATED_ROLES = frozenset(
    {
        PackageArtifactRole.SOURCE_IDENTITY,
        PackageArtifactRole.QUALITY_FLAGS,
        PackageArtifactRole.CHECKSUMS,
        PackageArtifactRole.STUDY,
        PackageArtifactRole.REPORT,
    }
)
_REQUIRED_INPUT_SINGLETONS = frozenset(
    {
        PackageArtifactRole.REQUEST,
        PackageArtifactRole.SPEC,
        PackageArtifactRole.POLICY,
        PackageArtifactRole.CONFIGURATION,
        PackageArtifactRole.DEPENDENCY_IDENTITY,
        PackageArtifactRole.RUNTIME_IDENTITY,
        PackageArtifactRole.ACCOUNTING,
        PackageArtifactRole.EVIDENCE_GRAPH,
    }
)
_REQUIRED_INPUT_MULTI = frozenset(
    {
        PackageArtifactRole.IMMUTABLE_INPUT,
        PackageArtifactRole.DATA_CARD,
        PackageArtifactRole.MODEL_CARD,
        PackageArtifactRole.NORMALIZED_EVIDENCE,
        PackageArtifactRole.DERIVED_EVIDENCE,
    }
)
_ALL_SINGLETONS = _REQUIRED_INPUT_SINGLETONS | _GENERATED_ROLES

_GRAPH_ROLE_KINDS: Mapping[PackageArtifactRole, frozenset[EvidenceNodeKind]] = {
    PackageArtifactRole.REQUEST: frozenset({EvidenceNodeKind.CONFIGURATION}),
    PackageArtifactRole.SPEC: frozenset({EvidenceNodeKind.CONFIGURATION}),
    PackageArtifactRole.IMMUTABLE_INPUT: frozenset(
        {EvidenceNodeKind.SOURCE_ROW, EvidenceNodeKind.IMMUTABLE_INPUT}
    ),
    PackageArtifactRole.DATA_CARD: frozenset({EvidenceNodeKind.DATA_CARD}),
    PackageArtifactRole.MODEL_CARD: frozenset({EvidenceNodeKind.MODEL_CARD}),
    PackageArtifactRole.POLICY: frozenset({EvidenceNodeKind.POLICY}),
    PackageArtifactRole.CONFIGURATION: frozenset(
        {EvidenceNodeKind.CONFIGURATION}
    ),
    PackageArtifactRole.NORMALIZED_EVIDENCE: frozenset(
        {EvidenceNodeKind.NORMALIZED}
    ),
    PackageArtifactRole.DERIVED_EVIDENCE: frozenset(
        {
            EvidenceNodeKind.DERIVED,
            EvidenceNodeKind.ANALYSIS,
            EvidenceNodeKind.REPORT_CLAIM,
        }
    ),
    PackageArtifactRole.ACCOUNTING: frozenset({EvidenceNodeKind.ACCOUNTING}),
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_json_file(value: object) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _hash_payload(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _hash_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _require_id(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise ValidatedPackageError(f"{field_name}_invalid")
    return value


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValidatedPackageError(f"{field_name}_invalid")
    return value


def _require_hash(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValidatedPackageError(f"{field_name}_invalid")
    return value


def _quality_flags(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    result = tuple(values)
    if result != tuple(sorted(set(result))):
        raise ValidatedPackageError(f"{field_name}_must_be_sorted_unique")
    if any(not _QUALITY_FLAG.fullmatch(value) for value in result):
        raise ValidatedPackageError(f"{field_name}_invalid")
    return result


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValidatedPackageError(f"{field_name}_object_required")
    return cast(Mapping[str, object], value)


def _exact(
    value: Mapping[str, object],
    expected: frozenset[str],
    field_name: str,
) -> None:
    if set(value) != expected:
        raise ValidatedPackageError(f"{field_name}_fields_invalid")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValidatedPackageError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValidatedPackageError(f"nonfinite_json_constant:{value}")


def _load_json_bytes(raw: bytes, field_name: str) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidatedPackageError(f"{field_name}_invalid_json") from exc


def _safe_relative_path(value: str, field_name: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith("/")
    ):
        raise ValidatedPackageError(f"{field_name}_invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != value
    ):
        raise ValidatedPackageError(f"{field_name}_invalid")
    return path


def _artifact_relative_path(content_hash: str) -> str:
    digest = _require_hash(content_hash, "artifact.content_hash").removeprefix(
        "sha256:"
    )
    return f"objects/sha256/{digest}"


@dataclass(frozen=True, slots=True)
class PortableSourceArtifact:
    """One build-time payload; its source path is never retained in the package."""

    logical_id: str
    version: str
    role: PackageArtifactRole
    media_type: str
    payload: bytes
    quality_flags: tuple[str, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.logical_id, "portable_source.logical_id")
        _require_id(self.version, "portable_source.version")
        if not isinstance(self.role, PackageArtifactRole):
            raise ValidatedPackageError("portable_source.role_invalid")
        _require_text(self.media_type, "portable_source.media_type")
        if (
            not isinstance(self.payload, bytes)
            or not self.payload
            or len(self.payload) > _MAX_COMPONENT_BYTES
        ):
            raise ValidatedPackageError("portable_source.payload_size_invalid")
        _quality_flags(self.quality_flags, "portable_source.quality_flags")
        object.__setattr__(self, "content_hash", _hash_bytes(self.payload))

    @classmethod
    def from_json(
        cls,
        *,
        logical_id: str,
        version: str,
        role: PackageArtifactRole,
        payload: object,
        quality_flags: Sequence[str] = (),
    ) -> "PortableSourceArtifact":
        return cls(
            logical_id=logical_id,
            version=version,
            role=role,
            media_type="application/json",
            payload=_canonical_json_file(payload),
            quality_flags=tuple(quality_flags),
        )


@dataclass(frozen=True, slots=True)
class PortableArtifactRecord:
    logical_id: str
    version: str
    role: PackageArtifactRole
    relative_path: str
    content_hash: str
    byte_length: int
    media_type: str
    quality_flags: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_id(self.logical_id, "portable_artifact.logical_id")
        _require_id(self.version, "portable_artifact.version")
        if not isinstance(self.role, PackageArtifactRole):
            raise ValidatedPackageError("portable_artifact.role_invalid")
        _safe_relative_path(self.relative_path, "portable_artifact.relative_path")
        _require_hash(self.content_hash, "portable_artifact.content_hash")
        if self.relative_path != _artifact_relative_path(self.content_hash):
            raise ValidatedPackageError(
                "portable_artifact.path_not_content_addressed"
            )
        if (
            isinstance(self.byte_length, bool)
            or not isinstance(self.byte_length, int)
            or self.byte_length <= 0
            or self.byte_length > _MAX_COMPONENT_BYTES
        ):
            raise ValidatedPackageError("portable_artifact.byte_length_invalid")
        _require_text(self.media_type, "portable_artifact.media_type")
        _quality_flags(self.quality_flags, "portable_artifact.quality_flags")

    @classmethod
    def from_source(cls, source: PortableSourceArtifact) -> "PortableArtifactRecord":
        return cls(
            logical_id=source.logical_id,
            version=source.version,
            role=source.role,
            relative_path=_artifact_relative_path(source.content_hash),
            content_hash=source.content_hash,
            byte_length=len(source.payload),
            media_type=source.media_type,
            quality_flags=source.quality_flags,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "logical_id": self.logical_id,
            "version": self.version,
            "role": self.role.value,
            "relative_path": self.relative_path,
            "content_hash": self.content_hash,
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "quality_flags": list(self.quality_flags),
        }

    @classmethod
    def from_dict(cls, value: object) -> "PortableArtifactRecord":
        payload = _mapping(value, "portable_artifact")
        _exact(
            payload,
            frozenset(
                {
                    "logical_id",
                    "version",
                    "role",
                    "relative_path",
                    "content_hash",
                    "byte_length",
                    "media_type",
                    "quality_flags",
                }
            ),
            "portable_artifact",
        )
        try:
            role = PackageArtifactRole(cast(str, payload["role"]))
        except (TypeError, ValueError) as exc:
            raise ValidatedPackageError("portable_artifact.role_invalid") from exc
        raw_flags = payload["quality_flags"]
        if not isinstance(raw_flags, list) or any(
            not isinstance(item, str) for item in raw_flags
        ):
            raise ValidatedPackageError(
                "portable_artifact.quality_flags_array_required"
            )
        byte_length = payload["byte_length"]
        if isinstance(byte_length, bool) or not isinstance(byte_length, int):
            raise ValidatedPackageError("portable_artifact.byte_length_invalid")
        return cls(
            logical_id=_require_id(
                payload["logical_id"],
                "portable_artifact.logical_id",
            ),
            version=_require_id(
                payload["version"],
                "portable_artifact.version",
            ),
            role=role,
            relative_path=_require_text(
                payload["relative_path"],
                "portable_artifact.relative_path",
            ),
            content_hash=_require_hash(
                payload["content_hash"],
                "portable_artifact.content_hash",
            ),
            byte_length=byte_length,
            media_type=_require_text(
                payload["media_type"],
                "portable_artifact.media_type",
            ),
            quality_flags=tuple(cast(list[str], raw_flags)),
        )


@dataclass(frozen=True, slots=True)
class SupportFileRecord:
    relative_path: str
    content_hash: str
    byte_length: int
    purpose: str

    def __post_init__(self) -> None:
        _safe_relative_path(self.relative_path, "support_file.relative_path")
        if self.relative_path.startswith("objects/"):
            raise ValidatedPackageError("support_file.objects_path_forbidden")
        _require_hash(self.content_hash, "support_file.content_hash")
        if (
            isinstance(self.byte_length, bool)
            or not isinstance(self.byte_length, int)
            or self.byte_length <= 0
        ):
            raise ValidatedPackageError("support_file.byte_length_invalid")
        _require_id(self.purpose, "support_file.purpose")

    def as_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "content_hash": self.content_hash,
            "byte_length": self.byte_length,
            "purpose": self.purpose,
        }

    @classmethod
    def from_dict(cls, value: object) -> "SupportFileRecord":
        payload = _mapping(value, "support_file")
        _exact(
            payload,
            frozenset(
                {"relative_path", "content_hash", "byte_length", "purpose"}
            ),
            "support_file",
        )
        byte_length = payload["byte_length"]
        if isinstance(byte_length, bool) or not isinstance(byte_length, int):
            raise ValidatedPackageError("support_file.byte_length_invalid")
        return cls(
            relative_path=_require_text(
                payload["relative_path"],
                "support_file.relative_path",
            ),
            content_hash=_require_hash(
                payload["content_hash"],
                "support_file.content_hash",
            ),
            byte_length=byte_length,
            purpose=_require_id(payload["purpose"], "support_file.purpose"),
        )


@dataclass(frozen=True, slots=True)
class ValidatedPackageManifest:
    package_id: str
    package_version: str
    seed: int
    artifacts: tuple[PortableArtifactRecord, ...]
    support_files: tuple[SupportFileRecord, ...]
    package_quality_flags: tuple[str, ...]
    content_hash: str = field(init=False)
    replay_algorithm_version: str = PORTABLE_REPLAY_ALGORITHM_VERSION
    schema_version: int = VALIDATED_PACKAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != VALIDATED_PACKAGE_SCHEMA_VERSION:
            raise ValidatedPackageError("package_manifest.schema_version_invalid")
        _require_id(self.package_id, "package_manifest.package_id")
        _require_id(self.package_version, "package_manifest.package_version")
        _require_id(
            self.replay_algorithm_version,
            "package_manifest.replay_algorithm_version",
        )
        if self.replay_algorithm_version != PORTABLE_REPLAY_ALGORITHM_VERSION:
            raise ValidatedPackageError(
                "package_manifest.replay_algorithm_unsupported"
            )
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValidatedPackageError("package_manifest.seed_invalid")
        artifact_keys = tuple(
            (item.role.value, item.logical_id, item.version)
            for item in self.artifacts
        )
        if artifact_keys != tuple(sorted(set(artifact_keys))):
            raise ValidatedPackageError(
                "package_manifest.artifacts_must_be_sorted_unique"
            )
        logical_ids = tuple(item.logical_id for item in self.artifacts)
        if len(logical_ids) != len(set(logical_ids)):
            raise ValidatedPackageError("package_manifest.logical_id_duplicate")
        support_paths = tuple(item.relative_path for item in self.support_files)
        if support_paths != tuple(sorted(set(support_paths))):
            raise ValidatedPackageError(
                "package_manifest.support_files_must_be_sorted_unique"
            )
        if set(support_paths) != {
            "INSTRUCTIONS.txt",
            "portable_runtime.py",
            "reproduce.py",
            "verify.py",
        }:
            raise ValidatedPackageError("package_manifest.support_files_incomplete")
        _quality_flags(
            self.package_quality_flags,
            "package_manifest.package_quality_flags",
        )
        self._validate_roles()
        if self.package_quality_flags != tuple(
            sorted(
                {
                    flag
                    for item in self.artifacts
                    if item.role is not PackageArtifactRole.QUALITY_FLAGS
                    for flag in item.quality_flags
                }
            )
        ):
            raise ValidatedPackageError(
                "package_manifest.quality_flag_propagation_mismatch"
            )
        object.__setattr__(self, "content_hash", _hash_payload(self.identity_payload()))

    def _validate_roles(self) -> None:
        counts = {
            role: sum(item.role is role for item in self.artifacts)
            for role in PackageArtifactRole
        }
        for role in _ALL_SINGLETONS:
            if counts[role] != 1:
                raise ValidatedPackageError(
                    f"package_manifest.role_cardinality_invalid:{role.value}"
                )
        for role in _REQUIRED_INPUT_MULTI:
            if counts[role] < 1:
                raise ValidatedPackageError(
                    f"package_manifest.role_required:{role.value}"
                )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "package_type": "PORTABLE_VALIDATED_MULTI_ASSET_RESEARCH",
            "package_id": self.package_id,
            "package_version": self.package_version,
            "replay_algorithm_version": self.replay_algorithm_version,
            "seed": self.seed,
            "artifacts": [item.as_dict() for item in self.artifacts],
            "support_files": [item.as_dict() for item in self.support_files],
            "package_quality_flags": list(self.package_quality_flags),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "ValidatedPackageManifest":
        payload = _mapping(value, "package_manifest")
        _exact(
            payload,
            frozenset(
                {
                    "schema_version",
                    "package_type",
                    "package_id",
                    "package_version",
                    "replay_algorithm_version",
                    "seed",
                    "artifacts",
                    "support_files",
                    "package_quality_flags",
                    "content_hash",
                }
            ),
            "package_manifest",
        )
        if payload["package_type"] != "PORTABLE_VALIDATED_MULTI_ASSET_RESEARCH":
            raise ValidatedPackageError("package_manifest.package_type_invalid")
        raw_artifacts = payload["artifacts"]
        raw_support = payload["support_files"]
        raw_flags = payload["package_quality_flags"]
        if not isinstance(raw_artifacts, list) or not isinstance(raw_support, list):
            raise ValidatedPackageError("package_manifest.array_fields_invalid")
        if not isinstance(raw_flags, list) or any(
            not isinstance(item, str) for item in raw_flags
        ):
            raise ValidatedPackageError(
                "package_manifest.package_quality_flags_array_required"
            )
        seed = payload["seed"]
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValidatedPackageError("package_manifest.seed_invalid")
        result = cls(
            package_id=_require_id(
                payload["package_id"],
                "package_manifest.package_id",
            ),
            package_version=_require_id(
                payload["package_version"],
                "package_manifest.package_version",
            ),
            seed=seed,
            artifacts=tuple(
                PortableArtifactRecord.from_dict(item) for item in raw_artifacts
            ),
            support_files=tuple(
                SupportFileRecord.from_dict(item) for item in raw_support
            ),
            package_quality_flags=tuple(cast(list[str], raw_flags)),
            replay_algorithm_version=_require_id(
                payload["replay_algorithm_version"],
                "package_manifest.replay_algorithm_version",
            ),
            schema_version=cast(int, payload["schema_version"]),
        )
        if payload["content_hash"] != result.content_hash:
            raise ValidatedPackageError("package_manifest.content_hash_mismatch")
        return result


@dataclass(frozen=True, slots=True)
class PortablePackageBuildRequest:
    package_id: str
    package_version: str
    seed: int
    artifacts: tuple[PortableSourceArtifact, ...]

    def __post_init__(self) -> None:
        _require_id(self.package_id, "package_build.package_id")
        _require_id(self.package_version, "package_build.package_version")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise ValidatedPackageError("package_build.seed_invalid")
        keys = tuple(
            (item.role.value, item.logical_id, item.version)
            for item in self.artifacts
        )
        if keys != tuple(sorted(set(keys))):
            raise ValidatedPackageError(
                "package_build.artifacts_must_be_sorted_unique"
            )
        logical_ids = tuple(item.logical_id for item in self.artifacts)
        if len(logical_ids) != len(set(logical_ids)):
            raise ValidatedPackageError("package_build.logical_id_duplicate")
        if any(item.role in _GENERATED_ROLES for item in self.artifacts):
            raise ValidatedPackageError("package_build.generated_role_forbidden")
        counts = {
            role: sum(item.role is role for item in self.artifacts)
            for role in PackageArtifactRole
        }
        for role in _REQUIRED_INPUT_SINGLETONS:
            if counts[role] != 1:
                raise ValidatedPackageError(
                    f"package_build.role_cardinality_invalid:{role.value}"
                )
        for role in _REQUIRED_INPUT_MULTI:
            if counts[role] < 1:
                raise ValidatedPackageError(
                    f"package_build.role_required:{role.value}"
                )
        self._validate_semantic_artifacts()

    def _validate_semantic_artifacts(self) -> None:
        graph: EvidenceGraph | None = None
        for artifact in self.artifacts:
            if artifact.media_type == "application/json":
                payload = _load_json_bytes(
                    artifact.payload,
                    f"package_build.{artifact.logical_id}",
                )
            else:
                payload = None
            if artifact.role is PackageArtifactRole.DATA_CARD:
                data_card = DataCard.from_dict(payload)
                if artifact.quality_flags != data_card.quality_flags:
                    raise ValidatedPackageError(
                        "package_build.data_card_quality_flags_mismatch"
                    )
            elif artifact.role is PackageArtifactRole.MODEL_CARD:
                model_card = ModelCard.from_dict(payload)
                if artifact.quality_flags != model_card.quality_flags:
                    raise ValidatedPackageError(
                        "package_build.model_card_quality_flags_mismatch"
                    )
            elif artifact.role is PackageArtifactRole.EVIDENCE_GRAPH:
                graph = EvidenceGraph.from_dict(payload)
                graph_flags = tuple(
                    sorted(
                        {
                            flag
                            for node in graph.nodes
                            for flag in node.quality_flags
                        }
                    )
                )
                if artifact.quality_flags != graph_flags:
                    raise ValidatedPackageError(
                        "package_build.graph_quality_flags_mismatch"
                    )
            elif artifact.role in (
                _REQUIRED_INPUT_SINGLETONS
                | _REQUIRED_INPUT_MULTI
            ) and artifact.media_type != "application/json":
                if artifact.role is not PackageArtifactRole.IMMUTABLE_INPUT:
                    raise ValidatedPackageError(
                        f"package_build.json_media_type_required:{artifact.role.value}"
                    )
        if graph is None:  # pragma: no cover - cardinality checked above
            raise ValidatedPackageError("package_build.evidence_graph_required")
        self._validate_graph_bindings(graph)

    def _validate_graph_bindings(self, graph: EvidenceGraph) -> None:
        nodes = {item.node_id: item for item in graph.nodes}
        traceable = tuple(
            item for item in self.artifacts if item.role in _GRAPH_ROLE_KINDS
        )
        for artifact in traceable:
            node = nodes.get(artifact.logical_id)
            if node is None:
                raise ValidatedPackageError(
                    f"package_build.graph_node_missing:{artifact.logical_id}"
                )
            if (
                node.kind not in _GRAPH_ROLE_KINDS[artifact.role]
                or node.version != artifact.version
                or node.content_hash != artifact.content_hash
            ):
                raise ValidatedPackageError(
                    f"package_build.graph_node_binding_mismatch:{artifact.logical_id}"
                )
        traceable_ids = {item.logical_id for item in traceable}
        if set(nodes) != traceable_ids:
            raise ValidatedPackageError("package_build.graph_unbound_node")


@dataclass(frozen=True, slots=True)
class PublishedValidatedPackage:
    path: Path
    manifest: ValidatedPackageManifest
    created: bool


@dataclass(frozen=True, slots=True)
class PackageVerificationReceipt:
    package_id: str
    package_version: str
    manifest_hash: str
    study_content_hash: str
    report_content_hash: str
    files_verified: int
    quality_flags: tuple[str, ...]
    status: str = "PASS"

    @classmethod
    def from_dict(cls, value: object) -> "PackageVerificationReceipt":
        payload = _mapping(value, "package_verification_receipt")
        expected = frozenset(
            {
                "status",
                "package_id",
                "package_version",
                "manifest_hash",
                "study_content_hash",
                "report_content_hash",
                "files_verified",
                "quality_flags",
            }
        )
        _exact(payload, expected, "package_verification_receipt")
        if payload["status"] != "PASS":
            raise ValidatedPackageError("package_verification_receipt.status_invalid")
        flags = payload["quality_flags"]
        files = payload["files_verified"]
        if not isinstance(flags, list) or any(
            not isinstance(item, str) for item in flags
        ):
            raise ValidatedPackageError(
                "package_verification_receipt.quality_flags_invalid"
            )
        if isinstance(files, bool) or not isinstance(files, int) or files <= 0:
            raise ValidatedPackageError(
                "package_verification_receipt.files_verified_invalid"
            )
        return cls(
            package_id=_require_id(
                payload["package_id"],
                "package_verification_receipt.package_id",
            ),
            package_version=_require_id(
                payload["package_version"],
                "package_verification_receipt.package_version",
            ),
            manifest_hash=_require_hash(
                payload["manifest_hash"],
                "package_verification_receipt.manifest_hash",
            ),
            study_content_hash=_require_hash(
                payload["study_content_hash"],
                "package_verification_receipt.study_content_hash",
            ),
            report_content_hash=_require_hash(
                payload["report_content_hash"],
                "package_verification_receipt.report_content_hash",
            ),
            files_verified=files,
            quality_flags=tuple(cast(list[str], flags)),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "package_id": self.package_id,
            "package_version": self.package_version,
            "manifest_hash": self.manifest_hash,
            "study_content_hash": self.study_content_hash,
            "report_content_hash": self.report_content_hash,
            "files_verified": self.files_verified,
            "quality_flags": list(self.quality_flags),
        }


@dataclass(frozen=True, slots=True)
class PackageReproductionReceipt:
    package_id: str
    package_version: str
    manifest_hash: str
    first_study_content_hash: str
    second_study_content_hash: str
    first_report_content_hash: str
    second_report_content_hash: str
    mismatch_fields: tuple[str, ...]
    status: str

    @classmethod
    def from_dict(cls, value: object) -> "PackageReproductionReceipt":
        payload = _mapping(value, "package_reproduction_receipt")
        expected = frozenset(
            {
                "status",
                "package_id",
                "package_version",
                "manifest_hash",
                "first_study_content_hash",
                "second_study_content_hash",
                "first_report_content_hash",
                "second_report_content_hash",
                "mismatch_fields",
            }
        )
        _exact(payload, expected, "package_reproduction_receipt")
        if payload["status"] not in {"PASS", "FAIL"}:
            raise ValidatedPackageError("package_reproduction_receipt.status_invalid")
        raw_mismatches = payload["mismatch_fields"]
        if not isinstance(raw_mismatches, list) or any(
            not isinstance(item, str) for item in raw_mismatches
        ):
            raise ValidatedPackageError(
                "package_reproduction_receipt.mismatch_fields_invalid"
            )
        hashes = {}
        for field_name in (
            "manifest_hash",
            "first_study_content_hash",
            "second_study_content_hash",
            "first_report_content_hash",
            "second_report_content_hash",
        ):
            hashes[field_name] = _require_hash(
                payload[field_name],
                f"package_reproduction_receipt.{field_name}",
            )
        return cls(
            package_id=_require_id(
                payload["package_id"],
                "package_reproduction_receipt.package_id",
            ),
            package_version=_require_id(
                payload["package_version"],
                "package_reproduction_receipt.package_version",
            ),
            manifest_hash=hashes["manifest_hash"],
            first_study_content_hash=hashes["first_study_content_hash"],
            second_study_content_hash=hashes["second_study_content_hash"],
            first_report_content_hash=hashes["first_report_content_hash"],
            second_report_content_hash=hashes["second_report_content_hash"],
            mismatch_fields=tuple(cast(list[str], raw_mismatches)),
            status=cast(str, payload["status"]),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "package_id": self.package_id,
            "package_version": self.package_version,
            "manifest_hash": self.manifest_hash,
            "first_study_content_hash": self.first_study_content_hash,
            "second_study_content_hash": self.second_study_content_hash,
            "first_report_content_hash": self.first_report_content_hash,
            "second_report_content_hash": self.second_report_content_hash,
            "mismatch_fields": list(self.mismatch_fields),
        }


_VERIFY_WRAPPER = b"""from __future__ import annotations
import runpy
import sys
from pathlib import Path
sys.dont_write_bytecode = True
runtime = runpy.run_path(str(Path(__file__).with_name("portable_runtime.py")))
raise SystemExit(runtime["main"]("verify"))
"""

_REPRODUCE_WRAPPER = b"""from __future__ import annotations
import runpy
import sys
from pathlib import Path
sys.dont_write_bytecode = True
runtime = runpy.run_path(str(Path(__file__).with_name("portable_runtime.py")))
raise SystemExit(runtime["main"]("reproduce"))
"""

_INSTRUCTIONS = b"""Portable validated multi-asset research package

This directory is immutable and content addressed. Do not edit it.

Verify from any empty cold root with Python 3.12 or later:
  python -I /absolute/path/to/package/verify.py /absolute/path/to/package

Recompute the canonical study and report twice and compare them:
  python -I /absolute/path/to/package/reproduce.py /absolute/path/to/package

Optionally preserve a repository-external receipt:
  python -I /absolute/path/to/package/reproduce.py /absolute/path/to/package \\
    --out /absolute/external/path/reproduction-receipt.json

The programs use only the Python standard library and bundled relative files.
They do not read Git metadata, a virtual environment, caches, prior results,
network services, credentials, or paths outside this package except --out.
"""

_GENERATED_LOGICAL_IDS = frozenset(
    {
        "source:portable-runtime",
        "quality:package",
        "checksums:package",
        "study:portable",
        "report:portable",
    }
)


def _runtime_source_bytes() -> bytes:
    source_path = Path(_portable_runtime.__file__).resolve(strict=True)
    return source_path.read_bytes()


def _support_payloads() -> dict[str, tuple[bytes, str]]:
    return {
        "INSTRUCTIONS.txt": (_INSTRUCTIONS, "instructions"),
        "portable_runtime.py": (_runtime_source_bytes(), "replay_source"),
        "reproduce.py": (_REPRODUCE_WRAPPER, "reproduce_entrypoint"),
        "verify.py": (_VERIFY_WRAPPER, "verify_entrypoint"),
    }


def _support_records(
    payloads: Mapping[str, tuple[bytes, str]],
) -> tuple[SupportFileRecord, ...]:
    return tuple(
        SupportFileRecord(
            relative_path=relative_path,
            content_hash=_hash_bytes(raw),
            byte_length=len(raw),
            purpose=purpose,
        )
        for relative_path, (raw, purpose) in sorted(payloads.items())
    )


def _generated_source_identity(
    support_records: Sequence[SupportFileRecord],
) -> PortableSourceArtifact:
    records = {item.relative_path: item for item in support_records}
    return PortableSourceArtifact.from_json(
        logical_id="source:portable-runtime",
        version=PORTABLE_REPLAY_ALGORITHM_VERSION,
        role=PackageArtifactRole.SOURCE_IDENTITY,
        payload={
            "schema_version": 1,
            "source_id": "portable-multi-asset-runtime",
            "source_version": PORTABLE_REPLAY_ALGORITHM_VERSION,
            "replay_source_path": "portable_runtime.py",
            "replay_source_hash": records["portable_runtime.py"].content_hash,
            "verify_wrapper_hash": records["verify.py"].content_hash,
            "reproduce_wrapper_hash": records["reproduce.py"].content_hash,
        },
    )


def _generated_quality_flags(
    values: Sequence[str],
) -> PortableSourceArtifact:
    flags = _quality_flags(values, "package_build.package_quality_flags")
    return PortableSourceArtifact.from_json(
        logical_id="quality:package",
        version="v1",
        role=PackageArtifactRole.QUALITY_FLAGS,
        payload={"schema_version": 1, "quality_flags": list(flags)},
    )


def _generated_checksums(
    records: Sequence[PortableArtifactRecord],
) -> PortableSourceArtifact:
    return PortableSourceArtifact.from_json(
        logical_id="checksums:package",
        version="v1",
        role=PackageArtifactRole.CHECKSUMS,
        payload={
            "schema_version": 1,
            "checksums": [
                {
                    "logical_id": item.logical_id,
                    "relative_path": item.relative_path,
                    "content_hash": item.content_hash,
                    "byte_length": item.byte_length,
                }
                for item in records
            ],
        },
    )


def _runtime_manifest_payload(
    *,
    request: PortablePackageBuildRequest,
    records: Sequence[PortableArtifactRecord],
    package_quality_flags: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema_version": VALIDATED_PACKAGE_SCHEMA_VERSION,
        "package_type": "PORTABLE_VALIDATED_MULTI_ASSET_RESEARCH",
        "package_id": request.package_id,
        "package_version": request.package_version,
        "replay_algorithm_version": PORTABLE_REPLAY_ALGORITHM_VERSION,
        "seed": request.seed,
        "artifacts": [item.as_dict() for item in records],
        "support_files": [],
        "package_quality_flags": list(package_quality_flags),
        "content_hash": _hash_payload(
            {
                "provisional": "replay-input-only",
                "package_id": request.package_id,
                "package_version": request.package_version,
            }
        ),
    }


def _json_payloads(
    artifacts: Sequence[PortableSourceArtifact],
) -> dict[str, object]:
    return {
        item.logical_id: _load_json_bytes(
            item.payload,
            f"package_build.{item.logical_id}",
        )
        for item in artifacts
        if item.media_type == "application/json"
    }


def _build_plan(
    request: PortablePackageBuildRequest,
) -> tuple[
    ValidatedPackageManifest,
    dict[str, bytes],
    dict[str, tuple[bytes, str]],
]:
    if set(item.logical_id for item in request.artifacts).intersection(
        _GENERATED_LOGICAL_IDS
    ):
        raise ValidatedPackageError("package_build.generated_logical_id_collision")
    support_payloads = _support_payloads()
    support_records = _support_records(support_payloads)
    source_identity = _generated_source_identity(support_records)
    package_quality_flags = tuple(
        sorted(
            {
                flag
                for item in request.artifacts
                for flag in item.quality_flags
            }
        )
    )
    quality_artifact = _generated_quality_flags(package_quality_flags)
    pre_checksum_artifacts = tuple(
        sorted(
            (*request.artifacts, source_identity, quality_artifact),
            key=lambda item: (item.role.value, item.logical_id, item.version),
        )
    )
    pre_checksum_records = tuple(
        PortableArtifactRecord.from_source(item) for item in pre_checksum_artifacts
    )
    checksums = _generated_checksums(pre_checksum_records)
    replay_inputs = tuple(
        sorted(
            (*pre_checksum_artifacts, checksums),
            key=lambda item: (item.role.value, item.logical_id, item.version),
        )
    )
    replay_records = tuple(
        PortableArtifactRecord.from_source(item) for item in replay_inputs
    )
    provisional = _runtime_manifest_payload(
        request=request,
        records=replay_records,
        package_quality_flags=package_quality_flags,
    )
    study_payload, report_payload = (
        _portable_runtime.canonical_replay_outputs(  # type: ignore[no-untyped-call]
            provisional,
            _json_payloads(replay_inputs),
        )
    )
    study = PortableSourceArtifact.from_json(
        logical_id="study:portable",
        version=request.package_version,
        role=PackageArtifactRole.STUDY,
        payload=study_payload,
        quality_flags=package_quality_flags,
    )
    report = PortableSourceArtifact.from_json(
        logical_id="report:portable",
        version=request.package_version,
        role=PackageArtifactRole.REPORT,
        payload=report_payload,
        quality_flags=package_quality_flags,
    )
    all_artifacts = tuple(
        sorted(
            (*replay_inputs, study, report),
            key=lambda item: (item.role.value, item.logical_id, item.version),
        )
    )
    records = tuple(
        PortableArtifactRecord.from_source(item) for item in all_artifacts
    )
    manifest = ValidatedPackageManifest(
        package_id=request.package_id,
        package_version=request.package_version,
        seed=request.seed,
        artifacts=records,
        support_files=support_records,
        package_quality_flags=package_quality_flags,
    )
    object_payloads: dict[str, bytes] = {}
    for artifact in all_artifacts:
        previous = object_payloads.setdefault(
            _artifact_relative_path(artifact.content_hash),
            artifact.payload,
        )
        if previous != artifact.payload:
            raise ValidatedPackageError("package_build.content_hash_collision")
    return manifest, object_payloads, support_payloads


def _external_directory_target(
    path_value: str | Path,
    *,
    project_root: Path,
) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise ValidatedPackageError("package_path_absolute_required")
    parent = path.parent.resolve(strict=True)
    target = parent / path.name
    if ResearchPathManager.is_within(target, project_root):
        raise ValidatedPackageError("package_path_repository_external_required")
    return target


def _write_file_once(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _manifest_from_package(path: Path) -> ValidatedPackageManifest:
    raw = (path / "manifest.json").read_bytes()
    return ValidatedPackageManifest.from_dict(
        _load_json_bytes(raw, "package_manifest")
    )


def build_validated_package(
    request: PortablePackageBuildRequest,
    output_path: str | Path,
    *,
    project_root: Path,
) -> PublishedValidatedPackage:
    """Atomically create one immutable, repository-external package directory."""

    if not isinstance(request, PortablePackageBuildRequest):
        raise ValidatedPackageError("package_build.request_required")
    target = _external_directory_target(output_path, project_root=project_root)
    manifest, object_payloads, support_payloads = _build_plan(request)
    if target.exists():
        receipt = verify_validated_package(target)
        existing = _manifest_from_package(target)
        if (
            receipt.status != "PASS"
            or existing.content_hash != manifest.content_hash
        ):
            raise ValidatedPackageError("package_build.existing_target_conflict")
        return PublishedValidatedPackage(
            path=target,
            manifest=existing,
            created=False,
        )
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
    )
    try:
        for relative_path, raw in sorted(object_payloads.items()):
            _write_file_once(staging / relative_path, raw)
        for relative_path, (raw, _purpose) in sorted(support_payloads.items()):
            _write_file_once(staging / relative_path, raw)
        _write_file_once(
            staging / "manifest.json",
            _canonical_json_file(manifest.as_dict()),
        )
        verification = verify_validated_package(staging)
        if (
            verification.status != "PASS"
            or verification.manifest_hash != manifest.content_hash
        ):
            raise ValidatedPackageError("package_build.internal_verification_failed")
        try:
            os.rename(staging, target)
        except FileExistsError:
            existing_receipt = verify_validated_package(target)
            if existing_receipt.manifest_hash != manifest.content_hash:
                raise ValidatedPackageError(
                    "package_build.concurrent_target_conflict"
                )
            return PublishedValidatedPackage(
                path=target,
                manifest=_manifest_from_package(target),
                created=False,
            )
        return PublishedValidatedPackage(
            path=target,
            manifest=manifest,
            created=True,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _trusted_runtime_matches(package_path: Path) -> None:
    bundled = (package_path / "portable_runtime.py").read_bytes()
    if bundled != _runtime_source_bytes():
        raise ValidatedPackageError("package_runtime_source_unsupported_or_tampered")


def verify_validated_package(
    package_path: str | Path,
) -> PackageVerificationReceipt:
    """Verify with the trusted copy of the same stdlib-only bundled algorithm."""

    try:
        path = Path(package_path).expanduser().resolve(strict=True)
        _trusted_runtime_matches(path)
        payload = _portable_runtime.verify_package(  # type: ignore[no-untyped-call]
            path
        )
        return PackageVerificationReceipt.from_dict(payload)
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, ValidatedPackageError):
            raise
        raise ValidatedPackageError(f"package_verification_failed:{exc}") from exc


def reproduce_validated_package(
    package_path: str | Path,
) -> PackageReproductionReceipt:
    """Recompute study and report twice using bundled inputs and source only."""

    try:
        path = Path(package_path).expanduser().resolve(strict=True)
        _trusted_runtime_matches(path)
        payload = _portable_runtime.reproduce_package(  # type: ignore[no-untyped-call]
            path
        )
        return PackageReproductionReceipt.from_dict(payload)
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, ValidatedPackageError):
            raise
        raise ValidatedPackageError(f"package_reproduction_failed:{exc}") from exc


def _read_external_component(
    path_value: object,
    *,
    project_root: Path,
    media_type: str,
    label: str,
) -> bytes:
    if not isinstance(path_value, str):
        raise ValidatedPackageError(f"{label}.path_invalid")
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        raise ValidatedPackageError(f"{label}.path_absolute_required")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ValidatedPackageError(f"{label}.path_noncanonical")
    if not resolved.is_file() or resolved.is_symlink():
        raise ValidatedPackageError(f"{label}.regular_file_required")
    if ResearchPathManager.is_within(resolved, project_root):
        raise ValidatedPackageError(f"{label}.repository_external_required")
    raw = resolved.read_bytes()
    if not raw or len(raw) > _MAX_COMPONENT_BYTES:
        raise ValidatedPackageError(f"{label}.size_invalid")
    if media_type == "application/json":
        return _canonical_json_file(_load_json_bytes(raw, label))
    return raw


def load_package_build_request(
    descriptor_path: str | Path,
    *,
    project_root: Path,
) -> PortablePackageBuildRequest:
    """Load a strict external build descriptor without retaining source paths."""

    descriptor = Path(descriptor_path).expanduser()
    if not descriptor.is_absolute():
        raise ValidatedPackageError("package_descriptor.absolute_path_required")
    descriptor = descriptor.resolve(strict=True)
    if (
        not descriptor.is_file()
        or descriptor.is_symlink()
        or ResearchPathManager.is_within(descriptor, project_root)
    ):
        raise ValidatedPackageError(
            "package_descriptor.repository_external_regular_file_required"
        )
    payload = _mapping(
        _load_json_bytes(descriptor.read_bytes(), "package_descriptor"),
        "package_descriptor",
    )
    _exact(
        payload,
        frozenset(
            {
                "schema_version",
                "package_id",
                "package_version",
                "seed",
                "artifacts",
            }
        ),
        "package_descriptor",
    )
    if payload["schema_version"] != PACKAGE_BUILD_DESCRIPTOR_SCHEMA_VERSION:
        raise ValidatedPackageError("package_descriptor.schema_version_invalid")
    raw_artifacts = payload["artifacts"]
    if not isinstance(raw_artifacts, list):
        raise ValidatedPackageError("package_descriptor.artifacts_array_required")
    artifacts: list[PortableSourceArtifact] = []
    for index, raw_item in enumerate(raw_artifacts):
        label = f"package_descriptor.artifacts.{index}"
        item = _mapping(raw_item, label)
        _exact(
            item,
            frozenset(
                {
                    "logical_id",
                    "version",
                    "role",
                    "path",
                    "media_type",
                    "quality_flags",
                }
            ),
            label,
        )
        try:
            role = PackageArtifactRole(cast(str, item["role"]))
        except (TypeError, ValueError) as exc:
            raise ValidatedPackageError(f"{label}.role_invalid") from exc
        media_type = _require_text(item["media_type"], f"{label}.media_type")
        raw_flags = item["quality_flags"]
        if not isinstance(raw_flags, list) or any(
            not isinstance(flag, str) for flag in raw_flags
        ):
            raise ValidatedPackageError(f"{label}.quality_flags_array_required")
        artifacts.append(
            PortableSourceArtifact(
                logical_id=_require_id(item["logical_id"], f"{label}.logical_id"),
                version=_require_id(item["version"], f"{label}.version"),
                role=role,
                media_type=media_type,
                payload=_read_external_component(
                    item["path"],
                    project_root=project_root,
                    media_type=media_type,
                    label=label,
                ),
                quality_flags=tuple(cast(list[str], raw_flags)),
            )
        )
    seed = payload["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValidatedPackageError("package_descriptor.seed_invalid")
    return PortablePackageBuildRequest(
        package_id=_require_id(
            payload["package_id"],
            "package_descriptor.package_id",
        ),
        package_version=_require_id(
            payload["package_version"],
            "package_descriptor.package_version",
        ),
        seed=seed,
        artifacts=tuple(
            sorted(
                artifacts,
                key=lambda item: (item.role.value, item.logical_id, item.version),
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class ValidatedPackageVerifier:
    """Small public facade for trusted verify-only and deterministic replay."""

    replay_algorithm_version: str = PORTABLE_REPLAY_ALGORITHM_VERSION

    def __post_init__(self) -> None:
        if self.replay_algorithm_version != PORTABLE_REPLAY_ALGORITHM_VERSION:
            raise ValidatedPackageError(
                "validated_package_verifier.algorithm_unsupported"
            )

    def verify(self, package_path: str | Path) -> PackageVerificationReceipt:
        return verify_validated_package(package_path)

    def reproduce(self, package_path: str | Path) -> PackageReproductionReceipt:
        return reproduce_validated_package(package_path)


__all__ = [
    "PACKAGE_BUILD_DESCRIPTOR_SCHEMA_VERSION",
    "PORTABLE_REPLAY_ALGORITHM_VERSION",
    "PackageArtifactRole",
    "PackageReproductionReceipt",
    "PackageVerificationReceipt",
    "PortableArtifactRecord",
    "PortablePackageBuildRequest",
    "PortableSourceArtifact",
    "PublishedValidatedPackage",
    "SupportFileRecord",
    "VALIDATED_PACKAGE_SCHEMA_VERSION",
    "ValidatedPackageError",
    "ValidatedPackageManifest",
    "ValidatedPackageVerifier",
    "build_validated_package",
    "load_package_build_request",
    "reproduce_validated_package",
    "verify_validated_package",
]
