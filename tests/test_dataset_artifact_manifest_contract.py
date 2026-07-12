from __future__ import annotations
import json
import sqlite3
from pathlib import Path
import pytest
from market_research.research.dataset_freeze import DatasetFreezeError, freeze_sqlite_candles_dataset
from market_research.research.datasets.artifact_manifest import ArtifactManifestError, load_artifact_manifest


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "source.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)")
        db.executemany("INSERT INTO candles VALUES ('KRW-BTC','1m',?,?,?,?,?,?)", [(1, 1., 1., 1., 1., 1.), (2, 2., 2., 2., 2., 1.)])
    return path


def test_freeze_writes_first_class_artifact_manifest(tmp_path: Path) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    manifest = load_artifact_manifest(frozen["artifact_manifest_uri"], frozen["artifact_manifest_hash"])
    assert manifest.content_hash == frozen["artifact_content_hash"]
    assert manifest.locator.path == frozen["artifact_path"]


def test_loader_rejects_tampered_artifact_manifest(tmp_path: Path) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    path = Path(frozen["artifact_manifest_uri"])
    payload = json.loads(path.read_text()); payload["scope"]["market"] = "KRW-ETH"; path.write_text(json.dumps(payload))
    with pytest.raises(ArtifactManifestError, match="hash_mismatch"):
        load_artifact_manifest(path)


def test_existing_tampered_artifact_is_not_reused(tmp_path: Path) -> None:
    source = _source(tmp_path)
    frozen = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    with sqlite3.connect(frozen["artifact_path"]) as db:
        db.execute("UPDATE candles SET close=9 WHERE ts=1")
    with pytest.raises(DatasetFreezeError, match="tampered"):
        freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
