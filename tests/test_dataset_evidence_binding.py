from __future__ import annotations

import pytest

from market_research.research.dataset_snapshot import (
    Candle,
    DatasetSnapshot,
    build_dataset_quality_report,
)
from market_research.research.experiment_manifest import DateRange
from market_research.research.experiment_registry import (
    EXPERIMENT_REGISTRY_SCHEMA_VERSION,
    FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
    final_holdout_reuse_key_hash_v2_from_parts,
    research_freedom_hash,
)


def test_report_binds_artifact_and_split_evidence() -> None:
    snapshot = DatasetSnapshot(
        "s",
        "frozen_sqlite_candles",
        "KRW-BTC",
        "1m",
        "train",
        DateRange("2026-01-01", "2026-01-01"),
        (Candle(1767225600000, 1, 1, 1, 1, 1),),
        artifact_id="a",
        artifact_content_hash="sha256:" + "a" * 64,
        artifact_schema_hash="sha256:" + "b" * 64,
        artifact_manifest_hash="sha256:" + "c" * 64,
    )
    report = build_dataset_quality_report(db_path="/unused", snapshot=snapshot).payload
    assert report["artifact_manifest_hash"] == snapshot.artifact_manifest_hash
    assert (
        report["dataset_content_hash_semantics"]
        == "snapshot_fingerprint_compatibility_alias"
    )


_COMPLETED_REUSE_EVIDENCE_FIELDS = (
    "dataset_artifact_evidence_hash",
    "final_holdout_query_hash",
    "final_holdout_data_hash",
    "final_holdout_fingerprint_hash",
    "final_holdout_quality_hash",
)


def _complete_reuse_key_arguments() -> dict[str, object]:
    return {
        "strategy_name": "noop_baseline",
        "market": "KRW-BTC",
        "interval": "1m",
        "final_holdout": {"start": "2026-01-01", "end": "2026-01-01"},
        "objective_metric": "return",
        "dataset_artifact_evidence_hash": "sha256:" + "a" * 64,
        "final_holdout_query_hash": "sha256:" + "b" * 64,
        "final_holdout_data_hash": "sha256:" + "c" * 64,
        "final_holdout_fingerprint_hash": "sha256:" + "d" * 64,
        "final_holdout_quality_hash": "sha256:" + "e" * 64,
    }


def _canonical_research_freedom_payload() -> dict[str, object]:
    common = _complete_reuse_key_arguments()
    digest = "sha256:" + "9" * 64
    return {
        "experiment_family_id": "canonical-family",
        "hypothesis_id": "canonical-hypothesis",
        "hypothesis_status": "pre_registered",
        "dataset_snapshot_id": "canonical-snapshot",
        "dataset_artifact_evidence_hash": common["dataset_artifact_evidence_hash"],
        "train_split_hash": digest,
        "validation_split_hash": digest,
        "final_holdout_split_hash": digest,
        "final_holdout_fingerprint": digest,
        "final_holdout_identity_hash": digest,
        "final_holdout_content_hash": digest,
        "final_holdout_query_hash": common["final_holdout_query_hash"],
        "final_holdout_data_hash": common["final_holdout_data_hash"],
        "final_holdout_fingerprint_hash": common["final_holdout_fingerprint_hash"],
        "final_holdout_quality_hash": common["final_holdout_quality_hash"],
        "final_holdout_reuse_key_hash": digest,
        "final_holdout_reuse_key_hash_v1": digest,
        "final_holdout_reuse_key_schema_version": FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
        "pre_exposure_reservation_key_hash": digest,
        "pre_exposure_reservation_key_schema_version": 1,
        "objective_metric": "return",
        "parameter_space_hash": digest,
        "computed_attempt_index": 1,
        "computed_holdout_reuse_count": 0,
        "experiment_registry_prior_hash": digest,
        "experiment_registry_row_hash": digest,
        "experiment_registry_path": "/one/path",
    }


@pytest.mark.parametrize("field", _COMPLETED_REUSE_EVIDENCE_FIELDS)
def test_registry_reuse_key_changes_for_each_completed_evidence_field(
    field: str,
) -> None:
    common = _complete_reuse_key_arguments()
    base = final_holdout_reuse_key_hash_v2_from_parts(**common)
    assert EXPERIMENT_REGISTRY_SCHEMA_VERSION == 3
    assert FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION == 4
    assert base is not None
    assert base != final_holdout_reuse_key_hash_v2_from_parts(
        **{**common, field: "sha256:" + "f" * 64}
    )


@pytest.mark.parametrize("field", _COMPLETED_REUSE_EVIDENCE_FIELDS)
def test_completed_reuse_key_rejects_missing_materialized_evidence(field: str) -> None:
    common = _complete_reuse_key_arguments()
    assert final_holdout_reuse_key_hash_v2_from_parts(**{**common, field: None}) is None


@pytest.mark.parametrize("field", _COMPLETED_REUSE_EVIDENCE_FIELDS)
def test_research_freedom_hash_changes_for_each_canonical_evidence_field(
    field: str,
) -> None:
    payload = _canonical_research_freedom_payload()
    assert research_freedom_hash(payload) != research_freedom_hash(
        {**payload, field: "sha256:" + "f" * 64}
    )


def test_research_freedom_hash_ignores_absolute_registry_path() -> None:
    payload = _canonical_research_freedom_payload()
    assert research_freedom_hash(payload) == research_freedom_hash(
        {**payload, "experiment_registry_path": "/another/path"}
    )
