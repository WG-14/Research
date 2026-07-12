from __future__ import annotations
import json
import sqlite3
from pathlib import Path
import pytest
from market_research.research.dataset_freeze import DatasetFreezeError, freeze_sqlite_candles_dataset
from market_research.research.datasets.artifact_manifest import ArtifactManifestError, load_artifact_manifest, parse_artifact_manifest
from market_research.research.datasets.hashing_contract import artifact_manifest_hash


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


@pytest.mark.parametrize("mutation", ("uri", "locator", "schema", "rows", "market", "interval", "scope"))
def test_loader_rejects_authoritative_sidecar_tamper(tmp_path: Path, mutation: str) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    path = Path(frozen["artifact_manifest_uri"])
    payload = json.loads(path.read_text())
    if mutation == "uri": payload["artifact"]["uri"] = str(tmp_path / "other.sqlite")
    elif mutation == "locator": payload["locator"]["path"] = str(tmp_path / "other.sqlite")
    elif mutation == "schema": payload["artifact"]["schema_hash"] = "sha256:" + "0" * 64
    elif mutation == "rows": payload["artifact"]["row_count"] = 3
    elif mutation == "market": payload["scope"]["market"] = "KRW-ETH"
    elif mutation == "interval": payload["scope"]["interval"] = "5m"
    else: payload["scope"]["start_ts"] = 3
    path.write_text(json.dumps(payload))
    with pytest.raises(ArtifactManifestError):
        load_artifact_manifest(path, frozen["artifact_manifest_hash"])


@pytest.mark.parametrize("section,key,value", ((None, "unknown", 1), ("artifact", "unknown", 1), ("scope", "unknown", 1), ("canonicalization", "unknown", 1)))
def test_manifest_unknown_fields_fail_closed(tmp_path: Path, section: str | None, key: str, value: object) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    payload = json.loads(Path(frozen["artifact_manifest_uri"]).read_text())
    (payload if section is None else payload[section])[key] = value
    payload["artifact_manifest_hash"] = artifact_manifest_hash({k:v for k,v in payload.items() if k != "artifact_manifest_hash"})
    with pytest.raises(ArtifactManifestError, match="unknown_field"):
        parse_artifact_manifest(payload)


@pytest.mark.parametrize("name,version", (("other", 1), ("ohlcv_pair_interval_rows", 2)))
def test_manifest_canonicalization_is_strict(tmp_path: Path, name: str, version: int) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    payload = json.loads(Path(frozen["artifact_manifest_uri"]).read_text())
    payload["canonicalization"] = {"name": name, "version": version}
    payload["artifact_manifest_hash"] = artifact_manifest_hash({k:v for k,v in payload.items() if k != "artifact_manifest_hash"})
    with pytest.raises(ArtifactManifestError, match="canonicalization"):
        parse_artifact_manifest(payload)


def test_manifest_identity_is_path_independent_but_integrity_hash_is_not(tmp_path: Path) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    payload = json.loads(Path(frozen["artifact_manifest_uri"]).read_text())
    original_identity, original_integrity = payload["artifact_identity_hash"], payload["artifact_manifest_hash"]
    payload["artifact"]["uri"] = str(tmp_path / "other.sqlite")
    payload["locator"]["path"] = str(tmp_path / "other.sqlite")
    payload["artifact_manifest_hash"] = artifact_manifest_hash({k:v for k,v in payload.items() if k != "artifact_manifest_hash"})
    assert payload["artifact_identity_hash"] == original_identity
    assert payload["artifact_manifest_hash"] != original_integrity


def test_unknown_artifact_manifest_schema_is_rejected(tmp_path: Path) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    payload = json.loads(Path(frozen["artifact_manifest_uri"]).read_text())
    payload["schema_version"] = 999
    with pytest.raises(ArtifactManifestError, match="schema_version"):
        parse_artifact_manifest(payload)


def test_existing_tampered_artifact_is_not_reused(tmp_path: Path) -> None:
    source = _source(tmp_path)
    frozen = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    with sqlite3.connect(frozen["artifact_path"]) as db:
        db.execute("UPDATE candles SET close=9 WHERE ts=1")
    with pytest.raises(DatasetFreezeError, match="tampered"):
        freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
