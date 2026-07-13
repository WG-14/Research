from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from market_research.research_composition import parse_builtin_manifest as parse_manifest
from market_research.research.experiment_registry import (
    EXPERIMENT_REGISTRY_SCHEMA_VERSION,
    FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
    final_holdout_reuse_key_hash_v2_from_parts,
    load_experiment_registry_rows,
    reserve_research_attempt_checked,
)
from market_research.research.final_selection import apply_final_selection_contract, build_selection_artifact
from market_research.research.validation_protocol import run_final_holdout_confirmation, run_research_backtest
from market_research.research_composition import builtin_strategy_registry

from .test_frozen_dataset_multi_split_integration import frozen_manifest_and_manager


def test_actual_registry_rows_bind_completed_frozen_artifact_evidence(tmp_path) -> None:
    _, parsed, manager = frozen_manifest_and_manager(
        tmp_path,
        final_selection=True,
        strategy_name="buy_and_hold_baseline",
    )
    parsed = replace(parsed, raw={
        **parsed.raw, "objective_metric": "return", "experiment_family_id": "registry-evidence-family",
        "hypothesis_id": "registry-evidence-hypothesis",
    })
    report = run_research_backtest(manifest=parsed, db_path=None, manager=manager,
                                   strategy_registry=builtin_strategy_registry())
    candidate = report["candidates"][0]
    candidate["aggregate_acceptance_gate_result"] = "PASS"
    candidate["acceptance_gate_result"] = "PASS"
    selection = apply_final_selection_contract(
        contract=parsed.final_selection,
        candidates=report["candidates"],
        report_context={"dataset_quality_gate_status": "PASS"},
        validation_required=False,
    )
    report["selection_artifact"] = build_selection_artifact(
        manifest_hash=parsed.manifest_hash(), selection_result=selection, candidates=report["candidates"]
    )
    confirmation = run_final_holdout_confirmation(
        manifest=parsed,
        selection_report=report,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )
    rows = load_experiment_registry_rows(Path(confirmation["experiment_registry_path"]))
    reservation = next(row for row in rows if row["event_type"] == "research_attempt_reserved")
    completion = next(row for row in rows if row["event_type"] == "research_attempt_completed")
    assert reservation["schema_version"] == EXPERIMENT_REGISTRY_SCHEMA_VERSION
    assert reservation["pre_exposure_reservation_key_hash"].startswith("sha256:")
    assert reservation.get("final_holdout_reuse_key_hash") is None
    required = (
        "dataset_artifact_evidence_hash", "final_holdout_query_hash", "final_holdout_data_hash",
        "final_holdout_fingerprint_hash", "final_holdout_quality_hash", "final_holdout_reuse_key_hash",
    )
    assert all(completion[field].startswith("sha256:") for field in required)
    assert completion["final_holdout_reuse_key_schema_version"] == FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
    recomputed = final_holdout_reuse_key_hash_v2_from_parts(
        strategy_name=parsed.strategy_name, market=parsed.market, interval=parsed.interval,
        final_holdout=confirmation["dataset_evidence"]["requested_range"],
        objective_metric="return", dataset_artifact_evidence_hash=confirmation["dataset_artifact_evidence_hash"],
        final_holdout_query_hash=confirmation["final_holdout_query_hash"],
        final_holdout_data_hash=confirmation["final_holdout_data_hash"],
        final_holdout_fingerprint_hash=confirmation["final_holdout_fingerprint_hash"],
        final_holdout_quality_hash=confirmation["final_holdout_quality_hash"],
    )
    assert completion["final_holdout_reuse_key_hash"] == recomputed == confirmation["final_holdout_reuse_key_hash"]
    for field in required[:-1]:
        assert confirmation[field] == completion[field]


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


def test_completed_holdout_blocks_second_pre_exposure_reservation(tmp_path) -> None:
    _, _, manager = frozen_manifest_and_manager(tmp_path)
    base = {
        "experiment_family_id": "family",
        "hypothesis_id": "hypothesis",
        "pre_exposure_reservation_key_hash": "sha256:" + "a" * 64,
        "selection_artifact_hash": "sha256:" + "b" * 64,
        "selected_candidate_id": "candidate-a",
    }
    contract = {"gates": {"max_holdout_reuse_count": 0}}

    first = reserve_research_attempt_checked(
        manager=manager,
        base_payload=base,
        statistical_validation_contract=contract,
    )
    second = reserve_research_attempt_checked(
        manager=manager,
        base_payload={**base, "selection_artifact_hash": "sha256:" + "c" * 64},
        statistical_validation_contract=contract,
    )

    assert first["accepted"] is True
    assert first["row"]["selection_artifact_hash"] == base["selection_artifact_hash"]
    assert second["accepted"] is False
    assert "holdout_reuse_budget_exceeded" in second["reasons"]


def test_concurrent_same_holdout_reservations_allow_exactly_one(tmp_path) -> None:
    _, _, manager = frozen_manifest_and_manager(tmp_path)
    base = {
        "experiment_family_id": "concurrent-family",
        "hypothesis_id": "concurrent-hypothesis",
        "pre_exposure_reservation_key_hash": "sha256:" + "d" * 64,
        "selection_artifact_hash": "sha256:" + "e" * 64,
        "selected_candidate_id": "candidate-a",
    }
    contract = {"gates": {"max_holdout_reuse_count": 0}}

    def reserve():
        return reserve_research_attempt_checked(
            manager=manager,
            base_payload=base,
            statistical_validation_contract=contract,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: reserve(), range(2)))

    assert sum(result["accepted"] is True for result in results) == 1
