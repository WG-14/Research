from __future__ import annotations

from tests.dataset_provenance_fixture import TEST_SOURCE_PROVENANCE
import pytest
import sqlite3
from market_research.research.datasets.locators import (
    LocatorValidationError,
    parse_immutable_locator,
)
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from market_research.research.validation_protocol import (
    validate_immutable_dataset_locator,
)
from market_research.research.datasets.artifact_manifest import ArtifactManifestError


def _locator(path: str = "/tmp/content/candles.sqlite") -> dict[str, str]:
    return {
        "type": "content_addressed_local",
        "path": path,
        "artifact_content_hash": "sha256:" + "b" * 64,
    }


@pytest.mark.parametrize(
    "path", ["datasets/latest.sqlite", "/datasets/current/c.sqlite", "relative.sqlite"]
)
def test_mutable_or_relative_locator_is_rejected(path: str) -> None:
    with pytest.raises(LocatorValidationError):
        parse_immutable_locator(_locator(path))


def test_unknown_locator_and_identity_less_locator_fail_closed() -> None:
    with pytest.raises(LocatorValidationError):
        parse_immutable_locator({"type": "unknown"})
    bad = _locator()
    del bad["artifact_content_hash"]
    with pytest.raises(LocatorValidationError):
        parse_immutable_locator(bad)


def test_content_addressed_local_locator_round_trips() -> None:
    assert parse_immutable_locator(_locator()).as_dict() == _locator()


def test_parent_symlink_locator_is_rejected(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(LocatorValidationError, match="symlink"):
        parse_immutable_locator(_locator(str(link / "candles.sqlite")))


def test_freeze_output_passes_shared_immutable_validator_without_legacy_locator(
    tmp_path,
) -> None:
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        db.execute("INSERT INTO candles VALUES ('KRW-BTC','1m',1,1,1,1,1,1)")
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=1,
        out_dir=tmp_path / "frozen",
    )
    validate_immutable_dataset_locator(
        artifact_manifest_uri=frozen["artifact_manifest_uri"],
        artifact_manifest_hash=frozen["artifact_manifest_hash"],
    )
    with pytest.raises(ArtifactManifestError):
        validate_immutable_dataset_locator(
            artifact_manifest_uri=frozen["artifact_manifest_uri"],
            artifact_manifest_hash="sha256:" + "0" * 64,
        )
