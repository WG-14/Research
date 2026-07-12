from __future__ import annotations
from market_research.research.dataset_snapshot import Candle, DatasetSnapshot, build_dataset_quality_report
from market_research.research.experiment_manifest import DateRange


def test_report_binds_artifact_and_split_evidence() -> None:
    snapshot = DatasetSnapshot("s", "frozen_sqlite_candles", "KRW-BTC", "1m", "train", DateRange("2026-01-01","2026-01-01"), (Candle(1767225600000,1,1,1,1,1),), artifact_id="a", artifact_content_hash="sha256:"+"a"*64, artifact_schema_hash="sha256:"+"b"*64, artifact_manifest_hash="sha256:"+"c"*64)
    report = build_dataset_quality_report(db_path="/unused", snapshot=snapshot).payload
    assert report["artifact_manifest_hash"] == snapshot.artifact_manifest_hash
    assert report["dataset_content_hash_semantics"] == "snapshot_fingerprint_compatibility_alias"
