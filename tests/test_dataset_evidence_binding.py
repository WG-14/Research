from __future__ import annotations
from market_research.research.dataset_snapshot import Candle, DatasetSnapshot, build_dataset_quality_report
from market_research.research.experiment_manifest import DateRange
from market_research.research.experiment_registry import (
    EXPERIMENT_REGISTRY_SCHEMA_VERSION,
    FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
    final_holdout_reuse_key_hash_v2_from_parts,
    research_freedom_hash,
)


def test_report_binds_artifact_and_split_evidence() -> None:
    snapshot = DatasetSnapshot("s", "frozen_sqlite_candles", "KRW-BTC", "1m", "train", DateRange("2026-01-01","2026-01-01"), (Candle(1767225600000,1,1,1,1,1),), artifact_id="a", artifact_content_hash="sha256:"+"a"*64, artifact_schema_hash="sha256:"+"b"*64, artifact_manifest_hash="sha256:"+"c"*64)
    report = build_dataset_quality_report(db_path="/unused", snapshot=snapshot).payload
    assert report["artifact_manifest_hash"] == snapshot.artifact_manifest_hash
    assert report["dataset_content_hash_semantics"] == "snapshot_fingerprint_compatibility_alias"


def test_registry_reuse_key_binds_artifact_and_holdout_evidence_without_paths() -> None:
    common = dict(strategy_name="noop_baseline", market="KRW-BTC", interval="1m", final_holdout={"start":"2026-01-01","end":"2026-01-01"}, objective_metric="return", dataset_artifact_evidence_hash="sha256:" + "a" * 64, final_holdout_query_hash="sha256:" + "b" * 64, final_holdout_data_hash="sha256:" + "c" * 64, final_holdout_fingerprint_hash="sha256:" + "d" * 64, final_holdout_quality_hash="sha256:" + "e" * 64)
    base = final_holdout_reuse_key_hash_v2_from_parts(**common)
    assert EXPERIMENT_REGISTRY_SCHEMA_VERSION == 3
    assert FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION == 4
    assert base != final_holdout_reuse_key_hash_v2_from_parts(**{**common, "dataset_artifact_evidence_hash": "sha256:" + "f" * 64})
    assert base != final_holdout_reuse_key_hash_v2_from_parts(**{**common, "final_holdout_quality_hash": "sha256:" + "f" * 64})
    payload = {"dataset_artifact_evidence_hash": common["dataset_artifact_evidence_hash"], "final_holdout_query_hash": common["final_holdout_query_hash"], "final_holdout_data_hash": common["final_holdout_data_hash"], "final_holdout_fingerprint_hash": common["final_holdout_fingerprint_hash"], "final_holdout_quality_hash": common["final_holdout_quality_hash"], "experiment_registry_path": "/one/path"}
    assert research_freedom_hash(payload) == research_freedom_hash({**payload, "experiment_registry_path": "/another/path"})


def test_completed_reuse_key_rejects_missing_materialized_evidence() -> None:
    assert final_holdout_reuse_key_hash_v2_from_parts(
        strategy_name="noop_baseline", market="KRW-BTC", interval="1m",
        final_holdout={"start": "2026-01-01", "end": "2026-01-01"}, objective_metric="return",
        dataset_artifact_evidence_hash="sha256:" + "a" * 64,
        final_holdout_query_hash="sha256:" + "b" * 64,
        final_holdout_data_hash=None,
        final_holdout_fingerprint_hash="sha256:" + "d" * 64,
        final_holdout_quality_hash="sha256:" + "e" * 64,
    ) is None
