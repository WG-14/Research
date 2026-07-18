"""Strict immutable artifact locator contract.

The locator is deliberately small: integrity is established by the artifact
manifest, not by a filename or a caller supplied ``immutable`` flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LocatorValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ContentAddressedLocal:
    path: str
    artifact_content_hash: str
    type: str = "content_addressed_local"

    def as_dict(self) -> dict[str, str]:
        return {
            "type": self.type,
            "path": self.path,
            "artifact_content_hash": self.artifact_content_hash,
        }


ImmutableLocator = ContentAddressedLocal


def parse_immutable_locator(value: Any) -> ImmutableLocator:
    if not isinstance(value, dict):
        raise LocatorValidationError("immutable_locator_must_be_object")
    if set(value) - {"type", "path", "artifact_content_hash"}:
        raise LocatorValidationError("immutable_locator_unknown_field")
    if value.get("type") != "content_addressed_local":
        raise LocatorValidationError("immutable_locator_unknown_type")
    path = value.get("path")
    content_hash = value.get("artifact_content_hash")
    if not isinstance(path, str) or not path.strip():
        raise LocatorValidationError("immutable_locator_identity_material_missing")
    if not isinstance(content_hash, str) or not content_hash.strip():
        raise LocatorValidationError("immutable_locator_identity_material_missing")
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        raise LocatorValidationError("immutable_locator_path_must_be_absolute")
    # Policy A: a local immutable locator rejects *every* symlink in the
    # component chain.  Checking only the final file permits a mutable parent
    # directory to redirect an otherwise content-addressed looking artifact.
    _reject_symlink_components(resolved)
    normalized = str(resolved.resolve(strict=False))
    parts = {part.lower() for part in Path(normalized).parts}
    if {"latest", "current"} & parts:
        raise LocatorValidationError("immutable_locator_mutable_name")
    for label, digest in (("artifact_content_hash", content_hash),):
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise LocatorValidationError(f"{label}_must_be_sha256")
    return ContentAddressedLocal(path=normalized, artifact_content_hash=content_hash)


def _reject_symlink_components(path: Path) -> None:
    """Reject any extant symlink from the filesystem root through ``path``."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise LocatorValidationError("immutable_locator_parent_symlink_rejected")
