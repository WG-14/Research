from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from market_research.research.experiment_manifest import parse_manifest
from market_research.research.experiment_registry import (
    EXPERIMENT_REGISTRY_SCHEMA_VERSION,
    FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
    final_holdout_reuse_key_hash_v2_from_parts,
    load_experiment_registry_rows,
)
from market_research.research.validation_protocol import run_research_backtest
from market_research.research.builtin_registry import builtin_strategy_registry

from .test_frozen_dataset_multi_split_integration import frozen_manifest_and_manager


def test_actual_registry_rows_bind_completed_frozen_artifact_evidence(tmp_path) -> None:
    _, manifest, manager = frozen_manifest_and_manager(tmp_path)
    payload = dict(manifest.raw)
    payload["research_classification"] = "exploratory"
    parsed = parse_manifest(payload)
    parsed = replace(parsed, raw={
        **parsed.raw, "objective_metric": "return", "experiment_family_id": "registry-evidence-family",
        "hypothesis_id": "registry-evidence-hypothesis",
    })
    report = run_research_backtest(manifest=parsed, db_path=None, manager=manager,
                                   strategy_registry=builtin_strategy_registry())
    rows = load_experiment_registry_rows(Path(report["experiment_registry_path"]))
    reservation = next(row for row in rows if row["event_type"] == "research_attempt_reserved")
    completion = next(row for row in rows if row["event_type"] == "research_attempt_completed")
    assert reservation["schema_version"] == EXPERIMENT_REGISTRY_SCHEMA_VERSION
    assert reservation["pre_exposure_reservation_key_hash"].startswith("sha256:")
    assert reservation["final_holdout_reuse_key_hash"] is None
    required = (
        "dataset_artifact_evidence_hash", "final_holdout_query_hash", "final_holdout_data_hash",
        "final_holdout_fingerprint_hash", "final_holdout_quality_hash", "final_holdout_reuse_key_hash",
    )
    assert all(completion[field].startswith("sha256:") for field in required)
    assert completion["final_holdout_reuse_key_schema_version"] == FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
    recomputed = final_holdout_reuse_key_hash_v2_from_parts(
        strategy_name=report["strategy_name"], market=report["market"], interval=report["interval"],
        final_holdout=report["dataset_splits"]["final_holdout"]["requested_range"],
        objective_metric="return", dataset_artifact_evidence_hash=report["dataset_artifact_evidence_hash"],
        final_holdout_query_hash=report["final_holdout_query_hash"],
        final_holdout_data_hash=report["final_holdout_data_hash"],
        final_holdout_fingerprint_hash=report["final_holdout_fingerprint_hash"],
        final_holdout_quality_hash=report["final_holdout_quality_hash"],
    )
    assert completion["final_holdout_reuse_key_hash"] == recomputed == report["final_holdout_reuse_key_hash"]
    assert report["lineage"]["research_freedom_hash"] == report["research_freedom_hash"]
    assert report["lineage"]["experiment_registry_completion_row_hash"] == completion["row_hash"]
    for field in required[:-1]:
        assert report["lineage"][field] == report[field] == completion[field]


def test_old_and_unknown_registry_schemas_fail_closed(tmp_path) -> None:
    path = tmp_path / "registry.jsonl"
    for schema in (1, 2, 999):
        path.write_text('{"schema_version": %d}\n' % schema)
        try:
            load_experiment_registry_rows(path)
        except ValueError as exc:
            assert "schema_version_unsupported" in str(exc)
        else:
            raise AssertionError("legacy registry schema must not be reinterpreted")
