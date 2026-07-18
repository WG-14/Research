from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

from market_research.research.hashing import sha256_prefixed
from market_research.research.reproduction import (
    ReproductionContractError,
    _dataset_split_hashes,
    build_reproduction_fingerprint,
    compare_reproduction_fingerprints,
    create_reproduction_receipt,
)
from market_research.research.validation_protocol import run_research_backtest
from market_research.research_composition import builtin_strategy_registry

from .clean_provenance_fixture import committed_checkout_provenance
from .test_frozen_dataset_multi_split_integration import frozen_manifest_and_manager


def test_receipt_projection_preserves_artifact_and_split_hashes() -> None:
    digest = "sha256:" + "a" * 64
    rows = _dataset_split_hashes(
        {
            "dataset_splits": {
                "train": {
                    "content_hash": digest,
                    "quality_hash": digest,
                    "snapshot_data_hash": digest,
                    "snapshot_query_hash": digest,
                    "snapshot_fingerprint_hash": digest,
                    "artifact_id": "a",
                    "artifact_manifest_hash": digest,
                    "artifact_content_hash": digest,
                    "artifact_schema_hash": digest,
                    "verification_status": "VERIFIED",
                    "verification": {"overall_status": "VERIFIED"},
                    "requested_range": {"start": "2026-01-01", "end": "2026-01-01"},
                }
            }
        }
    )
    assert rows[0]["artifact_id"] == "a"


def _valid_report_and_fingerprint(tmp_path):
    _, manifest, manager = frozen_manifest_and_manager(tmp_path)

    with patch(
        "market_research.research.execution_plan.collect_code_provenance",
        side_effect=committed_checkout_provenance,
    ):
        report = run_research_backtest(
            manifest=manifest,
            db_path=None,
            manager=manager,
            strategy_registry=builtin_strategy_registry(),
        )
    return (
        report,
        manifest,
        build_reproduction_fingerprint(report, manifest=manifest).as_dict(),
    )


def _recompute_stable_fingerprint_hash(fingerprint: dict[str, object]) -> None:
    material = {
        key: value
        for key, value in fingerprint.items()
        if key != "stable_fingerprint_hash"
    }
    fingerprint["stable_fingerprint_hash"] = sha256_prefixed(
        material, label="reproduction_stable_fingerprint"
    )


def _split(fingerprint: dict[str, object], split_name: str) -> dict[str, object]:
    return next(
        split
        for split in fingerprint["dataset_split_hashes"]
        if split["split_name"] == split_name
    )


def test_artifact_and_split_drift_report_distinct_reproduction_paths(tmp_path) -> None:
    _, _, baseline = _valid_report_and_fingerprint(tmp_path)

    artifact_only = copy.deepcopy(baseline)
    _split(artifact_only, "validation")["artifact_content_hash"] = sha256_prefixed(
        {"artifact": "tampered-content"}
    )
    _recompute_stable_fingerprint_hash(artifact_only)
    artifact_comparison = compare_reproduction_fingerprints(baseline, artifact_only)
    artifact_paths = {item["path"] for item in artifact_comparison.mismatches}
    assert artifact_comparison.status == "DRIFT"
    assert any("artifact_content_hash" in path for path in artifact_paths)
    assert not any("requested_range" in path for path in artifact_paths)

    split_only = copy.deepcopy(baseline)
    original_artifact = {
        field: _split(split_only, "validation")[field]
        for field in (
            "artifact_id",
            "artifact_manifest_hash",
            "artifact_content_hash",
            "artifact_schema_hash",
        )
    }
    changed_split = _split(split_only, "validation")
    changed_split["requested_range"] = {"start": "2026-01-03", "end": "2026-01-03"}
    changed_split["snapshot_query_hash"] = sha256_prefixed({"split": "changed-query"})
    changed_split["snapshot_fingerprint_hash"] = sha256_prefixed(
        {"split": "changed-fingerprint"}
    )
    _recompute_stable_fingerprint_hash(split_only)
    split_comparison = compare_reproduction_fingerprints(baseline, split_only)
    split_paths = {item["path"] for item in split_comparison.mismatches}
    assert split_comparison.status == "DRIFT"
    assert {
        field: changed_split[field] for field in original_artifact
    } == original_artifact
    assert any(
        path.endswith("requested_range")
        or path.endswith("snapshot_query_hash")
        or path.endswith("snapshot_fingerprint_hash")
        for path in split_paths
    )
    assert not any("artifact_content_hash" in path for path in split_paths)
    assert artifact_paths != split_paths
    assert any("artifact_content_hash" in path for path in artifact_paths)


@pytest.mark.parametrize(
    "field",
    (
        "artifact_id",
        "artifact_manifest_hash",
        "artifact_content_hash",
        "artifact_schema_hash",
    ),
)
def test_missing_artifact_evidence_rejects_receipt_creation(
    tmp_path, field: str
) -> None:
    report, manifest, _ = _valid_report_and_fingerprint(tmp_path)
    incomplete = copy.deepcopy(report)
    incomplete["dataset_splits"]["train"].pop(field)

    with pytest.raises(
        ReproductionContractError, match=rf"dataset_splits\.train\.{field} is required"
    ):
        create_reproduction_receipt(
            report=incomplete,
            manifest=manifest,
            receipt_path=tmp_path / f"missing-{field}.receipt.json",
        )
