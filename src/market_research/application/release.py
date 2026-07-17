"""Canonical release identity shared by platform adapters.

The Research package owns only the value contract.  Reading deployment
configuration, persisting heartbeats, and enforcing rollout compatibility
remain responsibilities of the operational adapter.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BUILD_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReleaseMetadataError(ValueError):
    """Raised when a release identity is missing or non-canonical."""


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    """Immutable identity of one build from the platform monorepo."""

    git_sha: str
    release_id: str
    build_digest: str

    def __post_init__(self) -> None:
        if not _GIT_SHA_RE.fullmatch(self.git_sha):
            raise ReleaseMetadataError("release_git_sha_invalid")
        if not _RELEASE_ID_RE.fullmatch(self.release_id):
            raise ReleaseMetadataError("release_id_invalid")
        if not _BUILD_DIGEST_RE.fullmatch(self.build_digest):
            raise ReleaseMetadataError("release_build_digest_invalid")

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str],
        *,
        git_sha_key: str = "RESEARCH_PLATFORM_GIT_SHA",
        release_id_key: str = "RESEARCH_PLATFORM_RELEASE_ID",
        build_digest_key: str = "RESEARCH_PLATFORM_BUILD_DIGEST",
    ) -> ReleaseMetadata:
        """Load an exact release identity without accepting implicit defaults."""

        return cls(
            git_sha=str(environ.get(git_sha_key, "")).strip(),
            release_id=str(environ.get(release_id_key, "")).strip(),
            build_digest=str(environ.get(build_digest_key, "")).strip(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "git_sha": self.git_sha,
            "release_id": self.release_id,
            "build_digest": self.build_digest,
        }


__all__ = ["ReleaseMetadata", "ReleaseMetadataError"]
