from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from market_research.research.data_plane import _scan_candles_sql
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from market_research.research.dataset_snapshot import load_dataset_split
from market_research.research.datasets.hashing_contract import artifact_content_hash
from market_research.research_composition import load_builtin_manifest

from tests.dataset_provenance_fixture import TEST_SOURCE_PROVENANCE
from tests.research_noop_success_fixture import create_success_fixture


def _source(tmp_path: Path, *, volume: float | None) -> Path:
    path = tmp_path / "source.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        db.execute("INSERT INTO candles VALUES ('KRW-BTC','1m',1,1,1,1,1,?)", (volume,))
    return path


def test_null_volume_is_not_hash_equivalent_to_real_zero_volume() -> None:
    with pytest.raises(ValueError, match="candle_volume_missing"):
        artifact_content_hash(
            [(1, 1.0, 1.0, 1.0, 1.0, None)], market="KRW-BTC", interval="1m"
        )
    assert artifact_content_hash(
        [(1, 1.0, 1.0, 1.0, 1.0, 0.0)], market="KRW-BTC", interval="1m"
    ).startswith("sha256:")


def test_freeze_rejects_missing_and_non_finite_ohlcv_without_publication(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, volume=None)
    with pytest.raises(ValueError, match="candle_volume_missing"):
        freeze_sqlite_candles_dataset(
            source_provenance=TEST_SOURCE_PROVENANCE,
            source_db=source,
            market="KRW-BTC",
            interval="1m",
            start_ts=1,
            end_ts=1,
            out_dir=tmp_path / "out",
        )
    assert not (tmp_path / "out").exists()

    with pytest.raises(ValueError, match="candle_close_non_finite"):
        artifact_content_hash(
            [(1, 1.0, 1.0, 1.0, float("inf"), 1.0)], market="KRW-BTC", interval="1m"
        )


def test_mutable_sqlite_loader_rejects_null_volume_instead_of_imputing_zero(
    tmp_path: Path,
) -> None:
    db_path, manifest_path = create_success_fixture(tmp_path)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE candles SET volume=NULL WHERE ts=(SELECT MIN(ts) FROM candles)"
        )
    manifest = load_builtin_manifest(manifest_path)
    with pytest.raises(ValueError, match="candle_volume_missing"):
        load_dataset_split(db_path=db_path, manifest=manifest, split_name="train")


def test_streaming_readiness_reports_missing_ohlcv_as_quality_failure(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path, volume=None)
    stats = _scan_candles_sql(
        db_path=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=1,
        interval_ms=60_000,
        max_missing_ranges=20,
        max_missing_sample=20,
    )
    assert stats["missing_ohlcv_count"] == 1
    assert stats["non_finite_ohlcv_count"] == 0
