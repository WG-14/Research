from __future__ import annotations

import json
from pathlib import Path
from dataclasses import replace

import pytest

from bithumb_bot.research.dataset_snapshot import (
    Candle,
    DatasetQualityReport,
    DatasetSnapshot,
    _build_source_agnostic_dataset_quality_report,
    build_dataset_quality_report,
    load_dataset_split,
)
from bithumb_bot.research.datasets.contracts import DatasetLoadContext, UnsupportedDatasetAdapterError
from bithumb_bot.research.datasets.registry import default_dataset_adapter_registry
from bithumb_bot.research.experiment_manifest import DateRange, parse_manifest
from bithumb_bot.research.readiness import build_research_readiness_report
from bithumb_bot.research.validation_protocol import ResearchValidationError, _validate_dataset_adapter_provenance


def _manifest(source: str = "sqlite_candles", top_source: str | None = None):
    dataset: dict[str, object] = {
        "source": source,
        "snapshot_id": "adapter_unit",
        "train": {"start": "2023-01-01", "end": "2023-01-01"},
        "validation": {"start": "2023-01-02", "end": "2023-01-02"},
    }
    if top_source is not None:
        dataset["top_of_book"] = {"source": top_source, "missing_policy": "warn"}
    return parse_manifest(
        {
            "experiment_id": "adapter_unit",
            "hypothesis": "Dataset adapters are resolved outside manifest parsing.",
            "strategy_name": "sma_with_filter",
            "market": "KRW-BTC",
            "interval": "1m",
            "dataset": dataset,
            "parameter_space": {"SMA_SHORT": [2], "SMA_LONG": [4]},
            "cost_model": {"fee_rate": 0.0, "slippage_bps": [0]},
            "acceptance_gate": {
                "min_trade_count": 1,
                "max_mdd_pct": 99,
                "min_profit_factor": 0.1,
                "oos_return_must_be_positive": False,
                "parameter_stability_required": False,
            },
        }
    )


class UnitCandleAdapter:
    source = "unit_candles_adapter_source"
    adapter_name = "unit_candle_adapter"
    adapter_version = "1"
    supported_capabilities = frozenset({"candles"})
    supported_top_of_book_sources = frozenset()
    supported_depth_sources = frozenset()
    supports_sqlite_streaming_quality_scan = False

    def load_range(self, *, manifest, split_name: str, date_range: DateRange, context: DatasetLoadContext) -> DatasetSnapshot:
        candles = tuple(
            Candle(date_range.start_ts_ms() + index * 60_000, 100.0, 101.0, 99.0, 100.0, 1.0)
            for index in range(24 * 60)
        )
        return DatasetSnapshot(
            snapshot_id=manifest.dataset.snapshot_id,
            source=manifest.dataset.source,
            market=manifest.market,
            interval=manifest.interval,
            split_name=split_name,
            date_range=date_range,
            candles=candles,
        )

    def quality_report(self, *, snapshot: DatasetSnapshot, context: DatasetLoadContext) -> DatasetQualityReport:
        return _build_source_agnostic_dataset_quality_report(
            db_path=None,
            snapshot=snapshot,
            adapter_name=self.adapter_name,
            adapter_version=self.adapter_version,
            adapter_provenance={"unit": {"source": self.source}},
        )

    def provenance(self, *, manifest, context: DatasetLoadContext) -> dict[str, object]:
        return {
            "dataset_source": manifest.dataset.source,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
        }


def test_sqlite_adapter_registered_by_default() -> None:
    adapter = default_dataset_adapter_registry().resolve("sqlite_candles")

    assert adapter.adapter_name == "sqlite_candle_adapter"
    assert "sqlite_orderbook_top_snapshots" in adapter.supported_top_of_book_sources
    assert "orderbook_depth_levels" in adapter.supported_depth_sources
    assert default_dataset_adapter_registry().resolve_depth("orderbook_depth_levels") is adapter


def test_manifest_parser_accepts_non_sqlite_source_but_registry_fails_closed(tmp_path: Path) -> None:
    manifest = _manifest("unknown_research_source")

    assert manifest.dataset.source == "unknown_research_source"
    with pytest.raises(UnsupportedDatasetAdapterError, match="unsupported_dataset_adapter:unknown_research_source"):
        load_dataset_split(db_path=tmp_path / "unused.sqlite", manifest=manifest, split_name="train")


def test_registered_non_sqlite_adapter_loads_without_manifest_parser_change(tmp_path: Path) -> None:
    default_dataset_adapter_registry().register(UnitCandleAdapter())
    manifest = _manifest("unit_candles_adapter_source")

    snapshot = load_dataset_split(db_path=tmp_path / "unused.sqlite", manifest=manifest, split_name="train")

    assert snapshot.source == "unit_candles_adapter_source"
    assert snapshot.candles[0].close == 100.0
    assert len(snapshot.candles) == 1440


def test_manifest_preserves_adapter_locator_options_and_provenance_fields() -> None:
    manifest = parse_manifest(
        {
            "experiment_id": "adapter_options_unit",
            "hypothesis": "Dataset adapter fields are parser-neutral.",
            "strategy_name": "sma_with_filter",
            "market": "KRW-BTC",
            "interval": "1m",
            "dataset": {
                "source": "unit_candles_adapter_source",
                "snapshot_id": "adapter_unit",
                "source_uri": "s3://research-bucket/immutable/candles.parquet",
                "source_content_hash": "sha256:content",
                "source_schema_hash": "sha256:schema",
                "locator": {"bucket": "research-bucket", "key": "immutable/candles.parquet"},
                "options": {"timezone": "UTC"},
                "train": {"start": "2023-01-01", "end": "2023-01-01"},
                "validation": {"start": "2023-01-02", "end": "2023-01-02"},
            },
            "parameter_space": {"SMA_SHORT": [2], "SMA_LONG": [4]},
            "cost_model": {"fee_rate": 0.0, "slippage_bps": [0]},
            "acceptance_gate": {
                "min_trade_count": 1,
                "max_mdd_pct": 99,
                "min_profit_factor": 0.1,
                "oos_return_must_be_positive": False,
                "parameter_stability_required": False,
            },
        }
    )

    assert manifest.dataset.source == "unit_candles_adapter_source"
    assert manifest.dataset.source_uri == "s3://research-bucket/immutable/candles.parquet"
    assert manifest.dataset.locator == {"bucket": "research-bucket", "key": "immutable/candles.parquet"}
    assert manifest.dataset.options == {"timezone": "UTC"}


def test_research_readiness_uses_non_sqlite_adapter_without_opening_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default_dataset_adapter_registry().register(UnitCandleAdapter())
    manifest = _manifest("unit_candles_adapter_source")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest.raw), encoding="utf-8")
    monkeypatch.setattr(
        "bithumb_bot.research.data_plane.sqlite3.connect",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sqlite should not be opened")),
    )

    report = build_research_readiness_report(
        manifest_path=manifest_path,
        db_path=tmp_path / "unused.sqlite",
    )

    assert report["status"] == "PASS"
    assert report["dataset_adapter"]["adapter_name"] == "unit_candle_adapter"
    assert report["dataset_adapter"]["quality_backend"] == "adapter_snapshot"
    assert report["splits"]["train"]["scan_method"] is None


def test_unknown_top_of_book_source_fails_at_resolver_not_parser(tmp_path: Path) -> None:
    manifest = _manifest("sqlite_candles", top_source="unknown_top_source")

    assert manifest.dataset.top_of_book is not None
    assert manifest.dataset.top_of_book.source == "unknown_top_source"
    with pytest.raises(UnsupportedDatasetAdapterError, match="unsupported_top_of_book_adapter:unknown_top_source"):
        load_dataset_split(db_path=tmp_path / "unused.sqlite", manifest=manifest, split_name="train")


def test_unknown_dataset_quality_adapter_does_not_fallback(tmp_path: Path) -> None:
    snapshot = DatasetSnapshot(
        snapshot_id="unknown",
        source="unregistered_source",
        market="KRW-BTC",
        interval="1m",
        split_name="train",
        date_range=DateRange(start="2023-01-01", end="2023-01-01"),
        candles=(),
    )

    with pytest.raises(UnsupportedDatasetAdapterError, match="unsupported_dataset_adapter:unregistered_source"):
        build_dataset_quality_report(db_path=tmp_path / "unused.sqlite", snapshot=snapshot)


def test_source_agnostic_quality_report_detects_non_sqlite_candle_defects() -> None:
    snapshot = DatasetSnapshot(
        snapshot_id="quality_non_sqlite",
        source="csv_fixture",
        market="KRW-BTC",
        interval="1m",
        split_name="train",
        date_range=DateRange(start="2023-01-01", end="2023-01-01"),
        candles=(
            Candle(1_672_531_200_000, 100.0, 101.0, 99.0, 100.0, 1.0),
            Candle(1_672_531_200_000, 100.0, 99.0, 101.0, 0.0, -1.0),
        ),
    )

    report = _build_source_agnostic_dataset_quality_report(
        db_path=None,
        snapshot=snapshot,
        adapter_name="csv_fixture_adapter",
        adapter_version="1",
        adapter_provenance={"csv": {"path": "memory"}},
    )

    assert report.quality_gate_status == "FAIL"
    assert "duplicate_candle_keys" in report.quality_gate_reasons
    assert "ohlc_invariant_violation" in report.quality_gate_reasons
    assert "non_positive_price" in report.quality_gate_reasons
    assert "negative_volume" in report.quality_gate_reasons
    assert "non_monotonic_timestamps" in report.quality_gate_reasons
    assert "missing_candles" in report.quality_gate_reasons
    assert report.payload["adapter_provenance"] == {"csv": {"path": "memory"}}
    assert report.payload["adapter_provenance_hash"].startswith("sha256:")
    assert report.payload["db_schema_fingerprint"] is None


def test_production_bound_adapter_provenance_requires_source_hashes() -> None:
    manifest = replace(_manifest("unit_candles_adapter_source"), deployment_tier="paper_candidate")
    snapshot = DatasetSnapshot(
        snapshot_id="quality_non_sqlite",
        source="unit_candles_adapter_source",
        market="KRW-BTC",
        interval="1m",
        split_name="train",
        date_range=DateRange(start="2023-01-01", end="2023-01-01"),
        candles=(Candle(1_672_531_200_000, 100.0, 101.0, 99.0, 100.0, 1.0),),
    )
    report = _build_source_agnostic_dataset_quality_report(
        db_path=None,
        snapshot=snapshot,
        adapter_name="unit_candle_adapter",
        adapter_version="1",
        adapter_provenance={"unit": {"source": "unit_candles_adapter_source"}},
    )
    report.payload["source_content_hash"] = "missing:unit"
    report.payload["source_schema_hash"] = "not_applicable:unit"
    report.payload["content_hash"] = "sha256:test"

    with pytest.raises(ResearchValidationError, match="dataset_adapter_provenance_failed:.*source_content_hash_missing"):
        _validate_dataset_adapter_provenance(manifest=manifest, quality_reports={"train": report})


def test_production_bound_adapter_provenance_rejects_mutable_locator() -> None:
    manifest = replace(_manifest("unit_candles_adapter_source"), deployment_tier="paper_candidate")
    manifest = replace(
        manifest,
        dataset=replace(manifest.dataset, source_uri="latest"),
    )
    snapshot = DatasetSnapshot(
        snapshot_id="quality_non_sqlite",
        source="unit_candles_adapter_source",
        market="KRW-BTC",
        interval="1m",
        split_name="train",
        date_range=DateRange(start="2023-01-01", end="2023-01-01"),
        candles=(Candle(1_672_531_200_000, 100.0, 101.0, 99.0, 100.0, 1.0),),
    )
    report = _build_source_agnostic_dataset_quality_report(
        db_path=None,
        snapshot=snapshot,
        adapter_name="unit_candle_adapter",
        adapter_version="1",
        adapter_provenance={"unit": {"source": "unit_candles_adapter_source"}},
    )
    report.payload["source_schema_hash"] = "sha256:schema"
    report.payload["content_hash"] = "sha256:test"

    with pytest.raises(ResearchValidationError, match="mutable_dataset_locator"):
        _validate_dataset_adapter_provenance(manifest=manifest, quality_reports={"train": report})


def test_production_bound_adapter_provenance_requires_declared_schema_hash() -> None:
    manifest = replace(_manifest("unit_candles_adapter_source"), deployment_tier="paper_candidate")
    manifest = replace(
        manifest,
        dataset=replace(manifest.dataset, source_content_hash="sha256:content"),
    )
    snapshot = DatasetSnapshot(
        snapshot_id="quality_non_sqlite",
        source="unit_candles_adapter_source",
        market="KRW-BTC",
        interval="1m",
        split_name="train",
        date_range=DateRange(start="2023-01-01", end="2023-01-01"),
        candles=(Candle(1_672_531_200_000, 100.0, 101.0, 99.0, 100.0, 1.0),),
    )
    report = _build_source_agnostic_dataset_quality_report(
        db_path=None,
        snapshot=snapshot,
        adapter_name="unit_candle_adapter",
        adapter_version="1",
        adapter_provenance={"unit": {"source": "unit_candles_adapter_source"}},
    )
    report.payload["source_content_hash"] = "sha256:content"
    report.payload["source_schema_hash"] = "sha256:schema"
    report.payload["content_hash"] = "sha256:test"

    with pytest.raises(ResearchValidationError, match="declared_source_schema_hash_missing"):
        _validate_dataset_adapter_provenance(manifest=manifest, quality_reports={"train": report})


def test_production_bound_adapter_provenance_rejects_repo_relative_locator() -> None:
    manifest = replace(_manifest("unit_candles_adapter_source"), deployment_tier="paper_candidate")
    manifest = replace(
        manifest,
        dataset=replace(
            manifest.dataset,
            source_content_hash="sha256:content",
            source_schema_hash="sha256:schema",
            locator={"path": "data/research/candles.parquet"},
        ),
    )
    snapshot = DatasetSnapshot(
        snapshot_id="quality_non_sqlite",
        source="unit_candles_adapter_source",
        market="KRW-BTC",
        interval="1m",
        split_name="train",
        date_range=DateRange(start="2023-01-01", end="2023-01-01"),
        candles=(Candle(1_672_531_200_000, 100.0, 101.0, 99.0, 100.0, 1.0),),
    )
    report = _build_source_agnostic_dataset_quality_report(
        db_path=None,
        snapshot=snapshot,
        adapter_name="unit_candle_adapter",
        adapter_version="1",
        adapter_provenance={"unit": {"source": "unit_candles_adapter_source"}},
    )
    report.payload["source_content_hash"] = "sha256:content"
    report.payload["source_schema_hash"] = "sha256:schema"
    report.payload["content_hash"] = "sha256:test"

    with pytest.raises(ResearchValidationError, match="mutable_dataset_locator"):
        _validate_dataset_adapter_provenance(manifest=manifest, quality_reports={"train": report})


def test_production_bound_adapter_provenance_rejects_declared_hash_mismatch() -> None:
    manifest = replace(_manifest("unit_candles_adapter_source"), deployment_tier="paper_candidate")
    manifest = replace(
        manifest,
        dataset=replace(manifest.dataset, source_content_hash="sha256:declared-other"),
    )
    snapshot = DatasetSnapshot(
        snapshot_id="quality_non_sqlite",
        source="unit_candles_adapter_source",
        market="KRW-BTC",
        interval="1m",
        split_name="train",
        date_range=DateRange(start="2023-01-01", end="2023-01-01"),
        candles=(Candle(1_672_531_200_000, 100.0, 101.0, 99.0, 100.0, 1.0),),
    )
    report = _build_source_agnostic_dataset_quality_report(
        db_path=None,
        snapshot=snapshot,
        adapter_name="unit_candle_adapter",
        adapter_version="1",
        adapter_provenance={"unit": {"source": "unit_candles_adapter_source"}},
    )
    report.payload["source_schema_hash"] = "sha256:schema"
    report.payload["content_hash"] = "sha256:test"

    with pytest.raises(ResearchValidationError, match="source_content_hash_mismatch"):
        _validate_dataset_adapter_provenance(manifest=manifest, quality_reports={"train": report})


def test_production_bound_adapter_provenance_rejects_hash_mismatch() -> None:
    manifest = replace(
        _manifest("unit_candles_adapter_source"),
        deployment_tier="paper_candidate",
    )
    manifest = replace(
        manifest,
        dataset=replace(
            manifest.dataset,
            source_content_hash="sha256:content",
            source_schema_hash="sha256:schema",
        ),
    )
    snapshot = DatasetSnapshot(
        snapshot_id="quality_non_sqlite",
        source="unit_candles_adapter_source",
        market="KRW-BTC",
        interval="1m",
        split_name="train",
        date_range=DateRange(start="2023-01-01", end="2023-01-01"),
        candles=(Candle(1_672_531_200_000, 100.0, 101.0, 99.0, 100.0, 1.0),),
    )
    report = _build_source_agnostic_dataset_quality_report(
        db_path=None,
        snapshot=snapshot,
        adapter_name="unit_candle_adapter",
        adapter_version="1",
        adapter_provenance={"unit": {"source": "unit_candles_adapter_source"}},
    )
    report.payload["source_content_hash"] = "sha256:content"
    report.payload["source_schema_hash"] = "sha256:schema"
    report.payload["adapter_provenance_hash"] = "sha256:wrong"
    report.payload["content_hash"] = "sha256:test"

    with pytest.raises(ResearchValidationError, match="adapter_provenance_hash_mismatch"):
        _validate_dataset_adapter_provenance(manifest=manifest, quality_reports={"train": report})
