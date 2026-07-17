from __future__ import annotations

from tests.dataset_provenance_fixture import TEST_SOURCE_PROVENANCE

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from market_research.research.dataset_snapshot import (
    Candle,
    DatasetSnapshot,
    build_dataset_quality_report,
    load_dataset_split,
)
from market_research.research.datasets.hashing_contract import artifact_content_hash
from market_research.research.experiment_manifest import DateRange
from market_research.research_composition import (
    parse_builtin_manifest as parse_manifest,
)


def _timestamp(day: str, minute: int = 0) -> int:
    return (
        int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)
        + minute * 60_000
    )


def _snapshot(
    *, split_name: str, date_range: DateRange, candles: tuple[Candle, ...]
) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id="hash-domain-fixture",
        source="frozen_sqlite_candles",
        market="KRW-BTC",
        interval="1m",
        split_name=split_name,
        date_range=date_range,
        candles=candles,
        artifact_id="fixture-artifact",
        artifact_content_hash="sha256:" + "a" * 64,
        artifact_schema_hash="sha256:" + "b" * 64,
        adapter_version="2",
    )


def _frozen_manifest(*, frozen: dict[str, object]) -> object:
    return parse_manifest(
        {
            "experiment_id": "hash_domain_fixture",
            "hypothesis": "hash domains remain distinct",
            "strategy_name": "noop_baseline",
            "research_classification": "research_only",
            "market": "KRW-BTC",
            "interval": "1m",
            "dataset": {
                "source": "frozen_sqlite_candles",
                "snapshot_id": "hash-domain-fixture",
                "artifact_manifest_uri": frozen["artifact_manifest_uri"],
                "artifact_manifest_hash": frozen["artifact_manifest_hash"],
                "train": {"start": "2026-01-01", "end": "2026-01-01"},
                "validation": {"start": "2026-01-02", "end": "2026-01-02"},
            },
            "parameter_space": {"NOOP_DECISION_START_INDEX": [0]},
            "cost_model": {"fee_rate": 0.001, "slippage_bps": [10]},
            "acceptance_gate": {
                "min_trade_count": 1,
                "max_mdd_pct": 100,
                "min_profit_factor": 0.1,
                "oos_return_must_be_positive": False,
                "parameter_stability_required": False,
                "final_holdout_required_for_validation": False,
            },
        }
    )


def _freeze_fixture(tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as conn:
        conn.execute(
            """
            CREATE TABLE candles (
                pair TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL,
                open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
                close REAL NOT NULL, volume REAL NOT NULL
            )
            """
        )
        for day, price in (("2026-01-01", 100.0), ("2026-01-02", 101.0)):
            conn.execute(
                "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("KRW-BTC", "1m", _timestamp(day), price, price, price, price, 1.0),
            )
    return freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=_timestamp("2026-01-01"),
        end_ts=_timestamp("2026-01-02"),
        out_dir=tmp_path / "frozen",
    )


def test_artifact_hash_is_distinct_from_snapshot_data_hash() -> None:
    candle = Candle(_timestamp("2026-01-01"), 100.0, 100.0, 100.0, 100.0, 1.0)
    snapshot = _snapshot(
        split_name="train",
        date_range=DateRange("2026-01-01", "2026-01-01"),
        candles=(candle,),
    )

    assert snapshot.artifact_content_hash != snapshot.snapshot_data_hash()


def test_artifact_content_hash_changes_when_artifact_rows_change() -> None:
    original = [("KRW-BTC", "1m", 1, 100.0, 100.0, 100.0, 100.0, 1.0)]
    changed = [("KRW-BTC", "1m", 1, 100.0, 100.0, 100.0, 101.0, 1.0)]

    assert artifact_content_hash(original) != artifact_content_hash(changed)


def test_snapshot_data_hash_does_not_include_split_role() -> None:
    candle = Candle(_timestamp("2026-01-01"), 100.0, 100.0, 100.0, 100.0, 1.0)
    date_range = DateRange("2026-01-01", "2026-01-01")

    assert (
        _snapshot(
            split_name="train", date_range=date_range, candles=(candle,)
        ).snapshot_data_hash()
        == _snapshot(
            split_name="validation", date_range=date_range, candles=(candle,)
        ).snapshot_data_hash()
    )


def test_snapshot_fingerprint_changes_when_split_role_changes() -> None:
    candle = Candle(_timestamp("2026-01-01"), 100.0, 100.0, 100.0, 100.0, 1.0)
    date_range = DateRange("2026-01-01", "2026-01-01")

    assert (
        _snapshot(
            split_name="train", date_range=date_range, candles=(candle,)
        ).snapshot_fingerprint_hash()
        != _snapshot(
            split_name="validation", date_range=date_range, candles=(candle,)
        ).snapshot_fingerprint_hash()
    )


def test_snapshot_query_hash_changes_when_range_changes() -> None:
    candle = Candle(_timestamp("2026-01-01"), 100.0, 100.0, 100.0, 100.0, 1.0)

    assert (
        _snapshot(
            split_name="train",
            date_range=DateRange("2026-01-01", "2026-01-01"),
            candles=(candle,),
        ).snapshot_query_hash()
        != _snapshot(
            split_name="train",
            date_range=DateRange("2026-01-01", "2026-01-02"),
            candles=(candle,),
        ).snapshot_query_hash()
    )


def test_quality_report_never_substitutes_snapshot_hash_for_artifact_hash() -> None:
    snapshot = DatasetSnapshot(
        snapshot_id="no-artifact",
        source="frozen_sqlite_candles",
        market="KRW-BTC",
        interval="1m",
        split_name="train",
        date_range=DateRange("2026-01-01", "2026-01-01"),
        candles=(Candle(_timestamp("2026-01-01"), 100.0, 100.0, 100.0, 100.0, 1.0),),
    )

    report = build_dataset_quality_report(db_path=Path("/unused"), snapshot=snapshot)

    assert report.payload["artifact_content_hash"] is None
    assert report.payload["source_content_hash"] is None
    assert (
        report.payload["snapshot_fingerprint_hash"]
        == snapshot.snapshot_fingerprint_hash()
    )
    assert (
        report.payload["source_content_hash"]
        != report.payload["snapshot_fingerprint_hash"]
    )


def test_artifact_hash_is_not_compared_to_materialized_range_hash(
    tmp_path: Path,
) -> None:
    frozen = _freeze_fixture(tmp_path)
    manifest = _frozen_manifest(frozen=frozen)

    snapshot = load_dataset_split(
        db_path=tmp_path / "unused.sqlite", manifest=manifest, split_name="train"
    )
    report = build_dataset_quality_report(
        db_path=tmp_path / "unused.sqlite", snapshot=snapshot
    )

    assert len(snapshot.candles) == 1
    assert snapshot.artifact_content_hash == frozen["artifact_content_hash"]
    assert snapshot.artifact_content_hash != snapshot.snapshot_data_hash()
    assert report.payload["artifact_content_hash"] == frozen["artifact_content_hash"]
    assert report.payload["snapshot_data_hash"] == snapshot.snapshot_data_hash()
    assert (
        report.payload["snapshot_fingerprint_hash"]
        == snapshot.snapshot_fingerprint_hash()
    )
