from __future__ import annotations

from tests.dataset_provenance_fixture import TEST_SOURCE_PROVENANCE
import json
import sqlite3
from pathlib import Path
import pytest
from datetime import datetime, timezone
from market_research.research.dataset_freeze import (
    DatasetFreezeError,
    freeze_sqlite_candles_dataset,
)
from market_research.research.datasets.artifact_manifest import (
    ArtifactManifestError,
    build_artifact_manifest,
    load_artifact_manifest,
    parse_artifact_manifest,
)
from market_research.research.datasets.hashing_contract import artifact_manifest_hash
from market_research.research.datasets.source_provenance import (
    SourceProvenanceError,
    build_dataset_source_provenance,
    parse_dataset_source_provenance,
    source_provenance_hash,
)
from market_research.research.dataset_snapshot import FrozenSQLiteCandleAdapter
from market_research.research.datasets.contracts import (
    DatasetArtifactRef,
    DatasetResolutionContext,
    DatasetSliceQuery,
)


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "source.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        db.executemany(
            "INSERT INTO candles VALUES ('KRW-BTC','1m',?,?,?,?,?,?)",
            [(1, 1.0, 1.0, 1.0, 1.0, 1.0), (2, 2.0, 2.0, 2.0, 2.0, 1.0)],
        )
    return path


def test_freeze_writes_first_class_artifact_manifest(tmp_path: Path) -> None:
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=_source(tmp_path),
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    manifest = load_artifact_manifest(
        frozen["artifact_manifest_uri"], frozen["artifact_manifest_hash"]
    )
    assert manifest.content_hash == frozen["artifact_content_hash"]
    assert manifest.locator.path == frozen["artifact_path"]
    assert manifest.schema_version == 3
    assert (
        manifest.source_provenance.provenance_manifest_hash
        == frozen["source_provenance_hash"]
    )


def test_same_rows_with_different_provenance_publish_distinct_artifacts(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    original = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    alternate = build_dataset_source_provenance(
        sources=(
            {
                **TEST_SOURCE_PROVENANCE.sources[0].as_dict(),
                "provider_id": "alternate-provider",
            },
        ),
        source_priority=("alternate-provider",),
        lineage=(stage.as_dict() for stage in TEST_SOURCE_PROVENANCE.lineage),
    )
    changed = freeze_sqlite_candles_dataset(
        source_provenance=alternate,
        source_db=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    assert original["artifact_content_hash"] == changed["artifact_content_hash"]
    assert original["source_provenance_hash"] != changed["source_provenance_hash"]
    assert original["artifact_id"] != changed["artifact_id"]
    assert original["artifact_path"] != changed["artifact_path"]


def test_source_provenance_priority_and_supported_semantics_fail_closed() -> None:
    payload = TEST_SOURCE_PROVENANCE.as_dict()
    payload["source_priority"] = ["undeclared-provider"]
    payload["provenance_manifest_hash"] = source_provenance_hash(payload)
    with pytest.raises(SourceProvenanceError, match="priority"):
        parse_dataset_source_provenance(payload)


def test_artifact_scope_must_be_covered_by_each_declared_source(tmp_path: Path) -> None:
    narrow = build_dataset_source_provenance(
        sources=(
            {
                **TEST_SOURCE_PROVENANCE.sources[0].as_dict(),
                "coverage_start_ts": 1,
                "coverage_end_ts": 1,
            },
        ),
        source_priority=TEST_SOURCE_PROVENANCE.source_priority,
        lineage=(stage.as_dict() for stage in TEST_SOURCE_PROVENANCE.lineage),
    )
    with pytest.raises(ArtifactManifestError, match="outside_source_coverage"):
        build_artifact_manifest(
            artifact_id="immutable-candle:test",
            path=str((tmp_path / "candles.sqlite").resolve()),
            content_hash="sha256:" + "a" * 64,
            schema_hash="sha256:" + "b" * 64,
            row_count=2,
            market="KRW-BTC",
            interval="1m",
            start_ts=1,
            end_ts=2,
            coverage_start_ts=1,
            coverage_end_ts=60_001,
            source_provenance=narrow,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("asset_class", "equity"),
        ("instrument_scope", "point_in_time_universe"),
        ("observation_calendar", "exchange_sessions"),
        ("timezone", "Asia/Seoul"),
        ("price_adjustment", "split_adjusted"),
        ("corporate_actions", "required"),
        ("universe", "point_in_time"),
    ),
)
def test_unsupported_data_semantics_are_structurally_rejected(
    field: str, value: str
) -> None:
    payload = TEST_SOURCE_PROVENANCE.as_dict()
    payload["semantics"][field] = value
    payload["provenance_manifest_hash"] = source_provenance_hash(payload)
    with pytest.raises(SourceProvenanceError, match="outside_supported_scope"):
        parse_dataset_source_provenance(payload)


def test_lineage_layers_cannot_be_omitted_or_reordered() -> None:
    payload = TEST_SOURCE_PROVENANCE.as_dict()
    payload["lineage"] = list(reversed(payload["lineage"]))
    payload["provenance_manifest_hash"] = source_provenance_hash(payload)
    with pytest.raises(SourceProvenanceError, match="raw_cleaned_standardized"):
        parse_dataset_source_provenance(payload)

    payload = TEST_SOURCE_PROVENANCE.as_dict()
    payload["semantics"]["observation_calendar"] = "exchange_sessions"
    payload["provenance_manifest_hash"] = source_provenance_hash(payload)
    with pytest.raises(SourceProvenanceError, match="outside_supported_scope"):
        parse_dataset_source_provenance(payload)


def test_legacy_artifact_manifest_schema_two_requires_refreeze(tmp_path: Path) -> None:
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=_source(tmp_path),
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    payload = json.loads(Path(frozen["artifact_manifest_uri"]).read_text())
    payload["schema_version"] = 2
    with pytest.raises(ArtifactManifestError, match="schema_version_unsupported"):
        parse_artifact_manifest(payload)


def test_loader_rejects_tampered_artifact_manifest(tmp_path: Path) -> None:
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=_source(tmp_path),
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    path = Path(frozen["artifact_manifest_uri"])
    payload = json.loads(path.read_text())
    payload["scope"]["market"] = "KRW-ETH"
    path.write_text(json.dumps(payload))
    with pytest.raises(ArtifactManifestError, match="hash_mismatch"):
        load_artifact_manifest(path)


@pytest.mark.parametrize(
    "mutation", ("uri", "locator", "schema", "rows", "market", "interval", "scope")
)
def test_loader_rejects_authoritative_sidecar_tamper(
    tmp_path: Path, mutation: str
) -> None:
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=_source(tmp_path),
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    path = Path(frozen["artifact_manifest_uri"])
    payload = json.loads(path.read_text())
    if mutation == "uri":
        payload["artifact"]["uri"] = str(tmp_path / "other.sqlite")
    elif mutation == "locator":
        payload["locator"]["path"] = str(tmp_path / "other.sqlite")
    elif mutation == "schema":
        payload["artifact"]["schema_hash"] = "sha256:" + "0" * 64
    elif mutation == "rows":
        payload["artifact"]["row_count"] = 3
    elif mutation == "market":
        payload["scope"]["market"] = "KRW-ETH"
    elif mutation == "interval":
        payload["scope"]["interval"] = "5m"
    else:
        payload["scope"]["start_ts"] = 3
    path.write_text(json.dumps(payload))
    with pytest.raises(ArtifactManifestError):
        load_artifact_manifest(path, frozen["artifact_manifest_hash"])


@pytest.mark.parametrize(
    "section,key,value",
    (
        (None, "unknown", 1),
        ("artifact", "unknown", 1),
        ("scope", "unknown", 1),
        ("canonicalization", "unknown", 1),
    ),
)
def test_manifest_unknown_fields_fail_closed(
    tmp_path: Path, section: str | None, key: str, value: object
) -> None:
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=_source(tmp_path),
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    payload = json.loads(Path(frozen["artifact_manifest_uri"]).read_text())
    (payload if section is None else payload[section])[key] = value
    payload["artifact_manifest_hash"] = artifact_manifest_hash(
        {k: v for k, v in payload.items() if k != "artifact_manifest_hash"}
    )
    with pytest.raises(ArtifactManifestError, match="unknown_field"):
        parse_artifact_manifest(payload)


@pytest.mark.parametrize(
    "name,version", (("other", 1), ("ohlcv_pair_interval_rows", 2))
)
def test_manifest_canonicalization_is_strict(
    tmp_path: Path, name: str, version: int
) -> None:
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=_source(tmp_path),
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    payload = json.loads(Path(frozen["artifact_manifest_uri"]).read_text())
    payload["canonicalization"] = {"name": name, "version": version}
    payload["artifact_manifest_hash"] = artifact_manifest_hash(
        {k: v for k, v in payload.items() if k != "artifact_manifest_hash"}
    )
    with pytest.raises(ArtifactManifestError, match="canonicalization"):
        parse_artifact_manifest(payload)


def test_manifest_identity_is_path_independent_but_integrity_hash_is_not(
    tmp_path: Path,
) -> None:
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=_source(tmp_path),
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    payload = json.loads(Path(frozen["artifact_manifest_uri"]).read_text())
    original_identity, original_integrity = (
        payload["artifact_identity_hash"],
        payload["artifact_manifest_hash"],
    )
    payload["artifact"]["uri"] = str(tmp_path / "other.sqlite")
    payload["locator"]["path"] = str(tmp_path / "other.sqlite")
    payload["artifact_manifest_hash"] = artifact_manifest_hash(
        {k: v for k, v in payload.items() if k != "artifact_manifest_hash"}
    )
    assert payload["artifact_identity_hash"] == original_identity
    assert payload["artifact_manifest_hash"] != original_integrity


def test_unknown_artifact_manifest_schema_is_rejected(tmp_path: Path) -> None:
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=_source(tmp_path),
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    payload = json.loads(Path(frozen["artifact_manifest_uri"]).read_text())
    payload["schema_version"] = 999
    with pytest.raises(ArtifactManifestError, match="schema_version"):
        parse_artifact_manifest(payload)


def test_existing_tampered_artifact_is_not_reused(tmp_path: Path) -> None:
    source = _source(tmp_path)
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    with sqlite3.connect(frozen["artifact_path"]) as db:
        db.execute("UPDATE candles SET close=9 WHERE ts=1")
    with pytest.raises(DatasetFreezeError, match="tampered"):
        freeze_sqlite_candles_dataset(
            source_provenance=TEST_SOURCE_PROVENANCE,
            source_db=source,
            market="KRW-BTC",
            interval="1m",
            start_ts=1,
            end_ts=2,
            out_dir=tmp_path / "out",
        )


def _utc_ts(hour: int, minute: int = 0) -> int:
    return int(
        datetime(2026, 1, 1, hour, minute, tzinfo=timezone.utc).timestamp() * 1000
    )


def _hourly_source(tmp_path: Path) -> Path:
    source = tmp_path / "hourly.sqlite"
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        for hour in range(13):
            db.execute(
                "INSERT INTO candles VALUES ('KRW-BTC','60m',?,?,?,?,?,?)",
                (_utc_ts(hour), 1.0, 1.0, 1.0, 1.0, 1.0),
            )
    return source


def _verified_hourly(tmp_path: Path):
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=_hourly_source(tmp_path),
        market="KRW-BTC",
        interval="60m",
        start_ts=_utc_ts(0),
        end_ts=_utc_ts(12),
        out_dir=tmp_path / "out",
    )
    adapter = FrozenSQLiteCandleAdapter()
    handle = adapter.resolve(
        DatasetArtifactRef(
            frozen["artifact_manifest_uri"], frozen["artifact_manifest_hash"]
        ),
        DatasetResolutionContext(),
    )
    return frozen, adapter, adapter.verify(handle)


def test_same_day_query_after_last_hourly_bucket_is_rejected_before_range_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, adapter, verified = _verified_hourly(tmp_path)
    called = False

    def unexpected_sql(**kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(
        "market_research.research.dataset_snapshot._load_frozen_rows", unexpected_sql
    )
    with pytest.raises(ValueError, match="outside_verified"):
        adapter.materialize(
            verified,
            DatasetSliceQuery(
                "KRW-BTC", "60m", _utc_ts(0), _utc_ts(23, 59), "train", "s", {}
            ),
        )
    assert called is False


def test_query_inside_last_bucket_coverage_is_allowed(tmp_path: Path) -> None:
    _, adapter, verified = _verified_hourly(tmp_path)
    snapshot = adapter.materialize(
        verified,
        DatasetSliceQuery(
            "KRW-BTC", "60m", _utc_ts(12), _utc_ts(12, 59) + 59_999, "train", "s", {}
        ),
    )
    assert [candle.ts for candle in snapshot.candles] == [_utc_ts(12)]


def test_full_day_one_minute_query_is_allowed(tmp_path: Path) -> None:
    source = tmp_path / "minutes.sqlite"
    start = _utc_ts(0)
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        db.executemany(
            "INSERT INTO candles VALUES ('KRW-BTC','1m',?,?,?,?,?,?)",
            [
                (start + minute * 60_000, 1.0, 1.0, 1.0, 1.0, 1.0)
                for minute in range(1440)
            ],
        )
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=start,
        end_ts=start + 1439 * 60_000,
        out_dir=tmp_path / "out",
    )
    adapter = FrozenSQLiteCandleAdapter()
    verified = adapter.verify(
        adapter.resolve(
            DatasetArtifactRef(
                frozen["artifact_manifest_uri"], frozen["artifact_manifest_hash"]
            ),
            DatasetResolutionContext(),
        )
    )
    snapshot = adapter.materialize(
        verified,
        DatasetSliceQuery(
            "KRW-BTC", "1m", start, start + 86_400_000 - 1, "train", "s", {}
        ),
    )
    assert len(snapshot.candles) == 1440


@pytest.mark.parametrize("mutation", ("schema", "row_count", "scope"))
def test_actual_artifact_mutation_is_rejected_by_complete_verification(
    tmp_path: Path, mutation: str
) -> None:
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=_source(tmp_path),
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "out",
    )
    with sqlite3.connect(frozen["artifact_path"]) as db:
        if mutation == "schema":
            db.execute("CREATE INDEX tampered_idx ON candles(ts)")
        elif mutation == "row_count":
            db.execute("INSERT INTO candles VALUES ('KRW-BTC','1m',3,3,3,3,3,1)")
        else:
            db.execute("UPDATE candles SET ts=0 WHERE ts=1")
    adapter = FrozenSQLiteCandleAdapter()
    handle = adapter.resolve(
        DatasetArtifactRef(
            frozen["artifact_manifest_uri"], frozen["artifact_manifest_hash"]
        ),
        DatasetResolutionContext(),
    )
    with pytest.raises(ValueError, match="not_verified"):
        adapter.verify(handle)
