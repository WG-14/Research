from __future__ import annotations

import pytest

from market_research.application import ReleaseMetadata, ReleaseMetadataError


def test_release_metadata_is_a_canonical_public_value_contract() -> None:
    metadata = ReleaseMetadata(
        git_sha="a" * 40,
        release_id="research-platform-2026.07.17.1",
        build_digest="sha256:" + "b" * 64,
    )

    assert metadata.as_dict() == {
        "git_sha": "a" * 40,
        "release_id": "research-platform-2026.07.17.1",
        "build_digest": "sha256:" + "b" * 64,
    }


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("git_sha", "a" * 39, "release_git_sha_invalid"),
        ("git_sha", "A" * 40, "release_git_sha_invalid"),
        ("release_id", "release id", "release_id_invalid"),
        ("build_digest", "b" * 64, "release_build_digest_invalid"),
    ],
)
def test_release_metadata_rejects_noncanonical_values(
    field: str, value: str, reason: str
) -> None:
    values = {
        "git_sha": "a" * 40,
        "release_id": "release-1",
        "build_digest": "sha256:" + "b" * 64,
    }
    values[field] = value

    with pytest.raises(ReleaseMetadataError, match=reason):
        ReleaseMetadata(**values)


def test_release_metadata_environment_loading_has_no_fallback_identity() -> None:
    with pytest.raises(ReleaseMetadataError, match="release_git_sha_invalid"):
        ReleaseMetadata.from_environ({})

    metadata = ReleaseMetadata.from_environ(
        {
            "GIT": "c" * 40,
            "RELEASE": "release-2",
            "BUILD": "sha256:" + "d" * 64,
        },
        git_sha_key="GIT",
        release_id_key="RELEASE",
        build_digest_key="BUILD",
    )
    assert metadata.git_sha == "c" * 40
