"""Operations adapter for the monorepo's canonical release identity."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from market_research.application import ReleaseMetadata

GIT_SHA_ENV = "RESEARCH_OPS_GIT_SHA"
RELEASE_ID_ENV = "RESEARCH_OPS_RELEASE_ID"
BUILD_DIGEST_ENV = "RESEARCH_OPS_BUILD_DIGEST"
RELEASE_BUNDLE_DIGEST_ENV = "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def configured_release(
    environ: Mapping[str, str] | None = None,
) -> ReleaseMetadata:
    environment = os.environ if environ is None else environ
    return ReleaseMetadata.from_environ(
        environment,
        git_sha_key=GIT_SHA_ENV,
        release_id_key=RELEASE_ID_ENV,
        build_digest_key=BUILD_DIGEST_ENV,
    )


def configured_release_bundle_digest(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return the exact immutable release-bundle identity or fail closed."""

    environment = os.environ if environ is None else environ
    digest = environment.get(RELEASE_BUNDLE_DIGEST_ENV, "").strip()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError("release_bundle_digest_invalid")
    return digest


__all__ = [
    "BUILD_DIGEST_ENV",
    "GIT_SHA_ENV",
    "RELEASE_BUNDLE_DIGEST_ENV",
    "RELEASE_ID_ENV",
    "configured_release",
    "configured_release_bundle_digest",
]
