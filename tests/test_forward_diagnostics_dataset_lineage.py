from __future__ import annotations

from dataclasses import replace

import pytest

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.forward_diagnostics import ForwardDiagnosticsDatasetQuality, forward_diagnostics_dataset_quality
from tests.test_forward_diagnostics_report import _dataset_quality, _manager, _manifest, _result
from bithumb_bot.research.forward_diagnostics_report import write_forward_diagnostics_report


def _snapshot(*, source_uri: str | None = None) -> DatasetSnapshot:
    candles = (
        Candle(ts=1, open=100.0, high=101.0, low=99.0, close=100.5, volume=1.0),
        Candle(ts=2, open=100.5, high=102.0, low=100.0, close=101.0, volume=1.0),
    )
    return DatasetSnapshot(
        snapshot_id="snapshot",
        source="sqlite_candles",
        market="BTC_KRW",
        interval="1m",
        split_name="train",
        date_range=DateRange("2026-01-01", "2026-01-01"),
        candles=candles,
        source_uri=source_uri,
        source_content_hash=None,
        locator={"db_path": source_uri},
    )


def test_report_includes_dataset_quality_report_ref(tmp_path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())
    quality = report["dataset_quality"]

    assert quality["dataset_quality_report_hash"].startswith("sha256:")
    assert quality["dataset_quality_report_payload"]["content_hash"] == quality["dataset_quality_report_hash"]


def test_report_includes_source_hash_status_when_source_hash_missing(tmp_path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    assert report["dataset"]["source_content_hash"] is None
    assert report["dataset"]["source_content_hash_status"] == "derived_from_materialized_snapshot"
    assert report["dataset_quality"]["source_content_hash_status"] == "derived_from_materialized_snapshot"
    assert report["dataset_quality"]["source_locator_policy"] == "source_locator_excluded_from_dataset_hash"


def test_report_content_hash_changes_when_dataset_quality_payload_changes(tmp_path) -> None:
    first_quality = _dataset_quality(report_hash="sha256:" + "1" * 64)
    changed_payload = dict(first_quality.dataset_quality_report_payload)
    changed_payload["quality_gate_reasons"] = ["unit_changed_payload"]
    second_quality = ForwardDiagnosticsDatasetQuality(
        quality_gate_status=first_quality.quality_gate_status,
        quality_gate_reasons=first_quality.quality_gate_reasons,
        dataset_quality_report_hash="sha256:" + "2" * 64,
        dataset_quality_report_payload=changed_payload,
        dataset_content_hash=first_quality.dataset_content_hash,
        canonical_snapshot_hash=first_quality.canonical_snapshot_hash,
        source_content_hash_status=first_quality.source_content_hash_status,
        source_schema_hash_status=first_quality.source_schema_hash_status,
        source_locator_policy=first_quality.source_locator_policy,
    )
    first = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "a"),
        manifest=_manifest(),
        result=_result(dataset_quality=first_quality),
    )
    second = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "b"),
        manifest=_manifest(),
        result=_result(dataset_quality=second_quality),
    )

    assert first["content_hash"] != second["content_hash"]


def test_runtime_db_path_does_not_change_dataset_content_hash() -> None:
    first = _snapshot(source_uri="/runtime/a/paper.sqlite")
    second = replace(first, source_uri="/runtime/b/paper.sqlite", locator={"db_path": "/runtime/b/paper.sqlite"})

    assert first.content_hash() == second.content_hash()


def test_dataset_quality_requires_source_hash_status_when_source_hash_missing() -> None:
    snapshot = _snapshot()
    quality = forward_diagnostics_dataset_quality(snapshot=snapshot)

    assert quality.source_content_hash_status == "derived_from_materialized_snapshot"
    with pytest.raises(ValueError, match="source_content_hash_status"):
        ForwardDiagnosticsDatasetQuality(
            quality_gate_status="PASS",
            quality_gate_reasons=(),
            dataset_quality_report_hash=quality.dataset_quality_report_hash,
            dataset_quality_report_payload=quality.dataset_quality_report_payload,
            dataset_content_hash=quality.dataset_content_hash,
            canonical_snapshot_hash=quality.canonical_snapshot_hash,
            source_content_hash_status="",
            source_schema_hash_status=quality.source_schema_hash_status,
            source_locator_policy=quality.source_locator_policy,
        )
