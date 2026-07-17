from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_research.orderbook_depth_store import build_orderbook_depth_snapshot
from market_research.research.dataset_snapshot import (
    Candle,
    DatasetSnapshot,
    SQLiteOrderbookDepthAdapter,
    SQLiteTopOfBookAdapter,
    TopOfBookQuote,
    _build_source_agnostic_dataset_quality_report,
    _db_table_schema_fingerprint,
    _require_execution_evidence_source_verified,
)
from market_research.research.datasets.contracts import DatasetLoadContext
from market_research.research.experiment_manifest import (
    DateRange,
    OrderbookDepthDatasetSpec,
    TopOfBookDatasetSpec,
)
from market_research.research.validation_protocol import (
    _depth_provenance_reasons,
    _top_of_book_provenance_reasons,
)


def _file_hash(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _write_top_of_book(path: Path, *, bid: float, ask: float) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE orderbook_top_snapshots (
                ts INTEGER NOT NULL,
                pair TEXT NOT NULL,
                bid_price REAL NOT NULL,
                ask_price REAL NOT NULL,
                spread_bps REAL NOT NULL,
                source TEXT NOT NULL,
                observed_at_epoch_sec REAL,
                PRIMARY KEY (ts, pair, source)
            )
            """
        )
        connection.execute(
            "INSERT INTO orderbook_top_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1_000, "KRW-BTC", bid, ask, 100.0, "fixture", 1.0),
        )
        connection.commit()
    finally:
        connection.close()


def _write_depth(path: Path, *, bid: float, ask: float) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE orderbook_depth_levels (
                ts INTEGER NOT NULL,
                pair TEXT NOT NULL,
                side TEXT NOT NULL,
                level_index INTEGER NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                cumulative_size REAL NOT NULL,
                cumulative_notional REAL NOT NULL,
                source TEXT NOT NULL,
                observed_at_epoch_sec REAL,
                PRIMARY KEY (ts, pair, side, level_index, source)
            )
            """
        )
        connection.executemany(
            "INSERT INTO orderbook_depth_levels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (1_000, "KRW-BTC", "bid", 0, bid, 1.0, 1.0, bid, "fixture", 1.0),
                (1_000, "KRW-BTC", "ask", 0, ask, 1.0, 1.0, ask, "fixture", 1.0),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _manifest(*, top=None, depth=None):
    return SimpleNamespace(
        market="KRW-BTC",
        interval="1m",
        research_classification="validated_candidate",
        dataset=SimpleNamespace(top_of_book=top, depth=depth),
    )


def test_top_of_book_typed_locator_is_the_data_authority_not_runtime_db(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "top-artifact.sqlite"
    runtime = tmp_path / "runtime.sqlite"
    _write_top_of_book(artifact, bid=100.0, ask=101.0)
    _write_top_of_book(runtime, bid=900.0, ask=901.0)
    content_hash = _file_hash(artifact)
    schema_hash = _db_table_schema_fingerprint(artifact, "orderbook_top_snapshots")
    spec = TopOfBookDatasetSpec(
        required=True,
        source_content_hash=content_hash,
        source_schema_hash=schema_hash,
        locator={
            "type": "content_addressed_local",
            "path": str(artifact),
            "artifact_content_hash": content_hash,
        },
    )
    manifest = _manifest(top=spec)
    adapter = SQLiteTopOfBookAdapter()

    quotes = adapter.load_event_quotes(
        manifest=manifest,
        candles=(Candle(1_000, 100.0, 101.0, 99.0, 100.0, 1.0),),
        execution_quote_lookahead_ms=0,
        context=DatasetLoadContext(db_path=runtime),
    )
    provenance = adapter.provenance(
        manifest=manifest, context=DatasetLoadContext(db_path=runtime)
    )

    assert quotes[0].bid_price == 100.0
    assert provenance["source_artifact_content_hash"] == content_hash
    assert provenance["source_schema_hash"] == schema_hash
    assert provenance["source_locator"]["path"] == str(artifact.resolve())


def test_top_of_book_source_artifact_hash_is_stable_across_split_evidence(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "top-artifact.sqlite"
    _write_top_of_book(artifact, bid=100.0, ask=101.0)
    content_hash = _file_hash(artifact)
    schema_hash = _db_table_schema_fingerprint(artifact, "orderbook_top_snapshots")
    spec = TopOfBookDatasetSpec(
        source_content_hash=content_hash,
        source_schema_hash=schema_hash,
        locator={
            "type": "content_addressed_local",
            "path": str(artifact),
            "artifact_content_hash": content_hash,
        },
    )
    provenance = SQLiteTopOfBookAdapter().provenance(
        manifest=_manifest(top=spec), context=DatasetLoadContext(db_path=None)
    )

    def report(split: str, price: float):
        candle = Candle(1_000, price, price, price, price, 1.0)
        quote = TopOfBookQuote(1_000, "KRW-BTC", price, price + 1.0, 100.0, "fixture")
        snapshot = DatasetSnapshot(
            snapshot_id="snapshot",
            source="frozen_sqlite_candles",
            market="KRW-BTC",
            interval="1m",
            split_name=split,
            date_range=DateRange("1970-01-01", "1970-01-01"),
            candles=(candle,),
            top_of_book_quotes=(quote,),
            top_of_book_event_quotes=(quote,),
            top_of_book_requested=True,
            top_of_book_source="sqlite_orderbook_top_snapshots",
            top_of_book_source_content_hash=content_hash,
            top_of_book_source_schema_hash=schema_hash,
            top_of_book_adapter_provenance=provenance,
        )
        return _build_source_agnostic_dataset_quality_report(
            db_path=None, snapshot=snapshot
        ).payload

    train = report("train", 100.0)
    validation = report("validation", 200.0)

    assert train["top_of_book_source_content_hash"] == content_hash
    assert validation["top_of_book_source_content_hash"] == content_hash
    assert (
        train["top_of_book_split_content_hash"]
        != validation["top_of_book_split_content_hash"]
    )
    manifest = _manifest(top=spec)
    assert (
        _top_of_book_provenance_reasons(
            manifest=manifest, split_name="train", payload=train
        )
        == []
    )
    assert (
        _top_of_book_provenance_reasons(
            manifest=manifest, split_name="validation", payload=validation
        )
        == []
    )


def test_changed_top_of_book_artifact_is_rejected_before_execution(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "top-artifact.sqlite"
    _write_top_of_book(artifact, bid=100.0, ask=101.0)
    original_hash = _file_hash(artifact)
    spec = TopOfBookDatasetSpec(
        source_content_hash=original_hash,
        source_schema_hash=_db_table_schema_fingerprint(
            artifact, "orderbook_top_snapshots"
        ),
        locator={
            "type": "content_addressed_local",
            "path": str(artifact),
            "artifact_content_hash": original_hash,
        },
    )
    connection = sqlite3.connect(artifact)
    try:
        connection.execute(
            "INSERT INTO orderbook_top_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            (2_000, "KRW-BTC", 200.0, 201.0, 100.0, "fixture", 2.0),
        )
        connection.commit()
    finally:
        connection.close()
    provenance = SQLiteTopOfBookAdapter().provenance(
        manifest=_manifest(top=spec), context=DatasetLoadContext(db_path=None)
    )

    with pytest.raises(ValueError, match="top_of_book_artifact_verification_failed"):
        _require_execution_evidence_source_verified(
            spec=spec,
            provenance=provenance,
            evidence="top_of_book",
            validation_bound=True,
        )


def test_depth_typed_locator_and_source_hash_are_independent_of_split(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "depth-artifact.sqlite"
    runtime = tmp_path / "runtime.sqlite"
    _write_depth(artifact, bid=100.0, ask=101.0)
    _write_depth(runtime, bid=900.0, ask=901.0)
    content_hash = _file_hash(artifact)
    schema_hash = _db_table_schema_fingerprint(artifact, "orderbook_depth_levels")
    spec = OrderbookDepthDatasetSpec(
        required=True,
        source_content_hash=content_hash,
        source_schema_hash=schema_hash,
        locator={
            "type": "content_addressed_local",
            "path": str(artifact),
            "artifact_content_hash": content_hash,
        },
        options={"source_filter": "fixture"},
    )
    manifest = _manifest(depth=spec)
    adapter = SQLiteOrderbookDepthAdapter()
    snapshots = adapter.load_event_snapshots(
        manifest=manifest,
        candles=(Candle(1_000, 100.0, 101.0, 99.0, 100.0, 1.0),),
        execution_depth_lookahead_ms=0,
        context=DatasetLoadContext(db_path=runtime),
    )
    provenance = adapter.provenance(
        manifest=manifest, context=DatasetLoadContext(db_path=runtime)
    )

    assert snapshots[0].bids[0].price == 100.0
    assert provenance["source_artifact_content_hash"] == content_hash

    def report(split: str, bid: float):
        snapshot = DatasetSnapshot(
            snapshot_id="snapshot",
            source="frozen_sqlite_candles",
            market="KRW-BTC",
            interval="1m",
            split_name=split,
            date_range=DateRange("1970-01-01", "1970-01-01"),
            candles=(Candle(1_000, bid, bid, bid, bid, 1.0),),
            orderbook_depth_snapshots=(
                build_orderbook_depth_snapshot(
                    ts=1_000,
                    pair="KRW-BTC",
                    bid_levels=((bid, 1.0),),
                    ask_levels=((bid + 1.0, 1.0),),
                    source="fixture",
                ),
            ),
            orderbook_depth_requested=True,
            orderbook_depth_source="orderbook_depth_levels",
            orderbook_depth_source_content_hash=content_hash,
            orderbook_depth_source_schema_hash=schema_hash,
            orderbook_depth_adapter_provenance=provenance,
        )
        return _build_source_agnostic_dataset_quality_report(
            db_path=None, snapshot=snapshot
        ).payload

    train = report("train", 100.0)
    validation = report("validation", 200.0)
    assert train["l2_depth_source_content_hash"] == content_hash
    assert validation["l2_depth_source_content_hash"] == content_hash
    assert train["l2_depth_content_hash"] != validation["l2_depth_content_hash"]
    manifest = _manifest(depth=spec)
    manifest.execution_timing = SimpleNamespace(
        depth_required=False,
        min_execution_reality_level_for_validation=None,
    )
    manifest.execution_model = SimpleNamespace(scenarios=())
    assert (
        _depth_provenance_reasons(manifest=manifest, split_name="train", payload=train)
        == []
    )
    assert (
        _depth_provenance_reasons(
            manifest=manifest, split_name="validation", payload=validation
        )
        == []
    )
