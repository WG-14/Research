from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from django.conf import settings
from django.core.exceptions import ValidationError

from market_research.application.adapter_contracts import (
    content_hash_payload,
    report_content_hash_payload,
    sha256_prefixed,
)

from .security import (
    ensure_path_within_root,
    reject_symlink_components,
    validate_relative_artifact_path,
    validate_sha256,
)

if TYPE_CHECKING:
    from .models import ManifestUpload


ArtifactRoot = Literal["data", "artifact", "report", "cache"]
ALLOWED_ROOTS = frozenset({"data", "artifact", "report", "cache"})


@dataclass(frozen=True, slots=True)
class SafeArtifactRef:
    root: ArtifactRoot
    relative_path: str

    def __post_init__(self) -> None:
        if self.root not in ALLOWED_ROOTS:
            raise ValidationError("artifact_ref_root_invalid")
        object.__setattr__(
            self,
            "relative_path",
            validate_relative_artifact_path(self.relative_path),
        )

    def __str__(self) -> str:
        return f"{self.root}:{self.relative_path}"

    @classmethod
    def parse(cls, value: str) -> "SafeArtifactRef":
        root, separator, relative = str(value or "").partition(":")
        if not separator or root not in ALLOWED_ROOTS:
            raise ValidationError("artifact_ref_invalid")
        return cls(root=root, relative_path=relative)  # type: ignore[arg-type]


def _root_path(root: ArtifactRoot) -> Path:
    paths = settings.RESEARCH_PATHS
    return {
        "data": paths.data_root,
        "artifact": paths.artifact_root,
        "report": paths.report_root,
        "cache": paths.cache_root,
    }[root].resolve()


def make_artifact_ref(root: ArtifactRoot, path: Path) -> SafeArtifactRef:
    root_path = _root_path(root)
    resolved = ensure_path_within_root(path, root_path)
    return SafeArtifactRef(root, resolved.relative_to(root_path).as_posix())


def resolve_artifact_ref(
    value: str | SafeArtifactRef,
    *,
    require_exists: bool = True,
) -> Path:
    reference = (
        value if isinstance(value, SafeArtifactRef) else SafeArtifactRef.parse(value)
    )
    root = _root_path(reference.root)
    candidate = root.joinpath(*Path(reference.relative_path).parts)
    resolved = ensure_path_within_root(candidate, root)
    reject_symlink_components(resolved)
    if require_exists and (not resolved.exists() or not resolved.is_file()):
        raise ValidationError("artifact_ref_target_missing")
    return resolved


def publish_manifest_bytes(*, content: bytes, content_hash: str) -> SafeArtifactRef:
    validate_sha256(content_hash, field="manifest_content_hash")
    actual_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    if actual_hash != content_hash:
        raise ValidationError("manifest_content_hash_mismatch")
    digest = content_hash.split(":", 1)[1]
    root = Path(settings.INTERNAL_WEB_MANIFEST_ROOT)
    target = root / digest[:2] / f"{digest}.json"
    ensure_path_within_root(target, settings.RESEARCH_PATHS.data_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    reject_symlink_components(target.parent)

    temporary = target.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = -1
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            reject_symlink_components(target)
            try:
                existing = _read_file_bounded(
                    target,
                    limit=len(content),
                    too_large_code="manifest_content_address_collision",
                    unavailable_code="manifest_content_address_collision",
                )
            except ValidationError as exc:
                raise ValidationError("manifest_content_address_collision") from exc
            if existing != content:
                raise ValidationError("manifest_content_address_collision")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    reject_symlink_components(target)
    return make_artifact_ref("data", target)


def read_verified_manifest_bytes(manifest: "ManifestUpload") -> bytes:
    """Read an immutable manifest with bounded size and metadata verification."""

    validate_sha256(manifest.content_hash, field="manifest_content_hash")
    limit = int(settings.INTERNAL_WEB_MAX_MANIFEST_BYTES)
    expected_size = int(manifest.size_bytes)
    if expected_size <= 0 or expected_size > limit:
        raise ValidationError("manifest_recorded_size_invalid")
    path = resolve_artifact_ref(manifest.storage_ref)
    content = _read_file_bounded(
        path,
        limit=limit,
        too_large_code="manifest_content_too_large_to_verify",
        unavailable_code="manifest_content_unavailable",
    )
    if len(content) != expected_size:
        raise ValidationError("manifest_content_size_mismatch")
    observed_hash = "sha256:" + hashlib.sha256(content).hexdigest()
    if observed_hash != manifest.content_hash:
        raise ValidationError("manifest_content_hash_mismatch")
    return content


def verify_result_artifact(
    value: str | SafeArtifactRef,
    *,
    expected_hash: str,
) -> dict[str, Any]:
    validate_sha256(expected_hash, field="result_hash")
    path = resolve_artifact_ref(value)
    content = _read_file_bounded(
        path,
        limit=int(settings.INTERNAL_WEB_MAX_RESULT_BYTES),
        too_large_code="result_artifact_too_large_to_verify",
        unavailable_code="result_artifact_json_invalid",
    )
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("result_artifact_json_invalid") from exc
    if not isinstance(payload, dict):
        raise ValidationError("result_artifact_must_be_object")
    if payload.get("content_hash") != expected_hash:
        raise ValidationError("result_artifact_recorded_hash_mismatch")
    without_hash = {key: item for key, item in payload.items() if key != "content_hash"}
    candidates = {
        sha256_prefixed(content_hash_payload(without_hash)),
        sha256_prefixed(report_content_hash_payload(payload)),
    }
    if expected_hash not in candidates:
        raise ValidationError("result_artifact_content_hash_mismatch")
    return payload


def _read_file_bounded(
    path: Path,
    *,
    limit: int,
    too_large_code: str,
    unavailable_code: str,
) -> bytes:
    """Read at most ``limit + 1`` bytes without following a final symlink."""

    if limit < 0:
        raise RuntimeError("bounded_file_read_limit_must_not_be_negative")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = -1
                content = handle.read(limit + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except OSError as exc:
        raise ValidationError(unavailable_code) from exc
    if len(content) > limit:
        raise ValidationError(too_large_code)
    return content
