from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from bithumb_research.paths import ResearchPathManager
from bithumb_research.research.experiment_manifest import load_manifest
from bithumb_research.research.reproduction import (
    ReproductionContractError,
    build_reproduction_fingerprint,
    compare_reproduction_fingerprints,
    load_reproduction_receipt,
)
from bithumb_research.research.validation_protocol import run_research_backtest
from bithumb_research.settings import ResearchSettings
from tests.research_sma_success_fixture import create_success_fixture


def _run_report(tmp_path: Path) -> tuple[Path, Path, ResearchPathManager, dict[str, object]]:
    db_path, manifest_path = create_success_fixture(tmp_path)
    settings = ResearchSettings(
        data_root=tmp_path / "datasets",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=db_path,
        max_workers=1,
        random_seed=0,
    )
    manager = ResearchPathManager.from_settings(settings, project_root=Path.cwd())
    report = run_research_backtest(
        manifest=load_manifest(manifest_path),
        db_path=db_path,
        manager=manager,
        manifest_path=str(manifest_path),
    )
    return db_path, manifest_path, manager, report


def test_receipt_binds_completed_backtest_to_stable_evidence(tmp_path: Path) -> None:
    _, manifest_path, _, report = _run_report(tmp_path)

    receipt = load_reproduction_receipt(report["reproduction_receipt_path"])

    assert receipt["manifest_hash"] == load_manifest(manifest_path).manifest_hash()
    assert receipt["source_report_hash"] == report["content_hash"]
    assert receipt["stable_fingerprint_hash"] == receipt["stable_fingerprint"]["stable_fingerprint_hash"]


def test_fingerprint_ignores_nondeterministic_fields_and_collection_order(tmp_path: Path) -> None:
    _, manifest_path, _, report = _run_report(tmp_path)
    manifest = load_manifest(manifest_path)
    changed = copy.deepcopy(report)
    changed.update({"generated_at": "2099-01-01T00:00:00+00:00", "wall_seconds": 99.0, "pid": 1234})
    changed["artifact_paths"] = {"report_path": "/another/absolute/path"}
    assert build_reproduction_fingerprint(report, manifest=manifest).stable_fingerprint_hash == build_reproduction_fingerprint(
        changed, manifest=manifest
    ).stable_fingerprint_hash

    second = copy.deepcopy(report["candidates"][0])
    second["parameter_candidate_id"] = "candidate_z"
    first_order = copy.deepcopy(report)
    first_order["candidates"] = [report["candidates"][0], second]
    reversed_order = copy.deepcopy(first_order)
    reversed_order["candidates"].reverse()
    assert build_reproduction_fingerprint(first_order, manifest=manifest).stable_fingerprint_hash == build_reproduction_fingerprint(
        reversed_order, manifest=manifest
    ).stable_fingerprint_hash


def test_comparator_reports_exact_result_hash_path_and_is_order_independent() -> None:
    fingerprint = {
        "schema_version": 1,
        "manifest_hash": "sha256:manifest",
        "research_classification": "research_only",
        "dataset_fingerprint": "sha256:data",
        "dataset_split_hashes": [{"split_name": "train", "content_hash": "sha256:train", "quality_hash": "sha256:quality"}],
        "strategy_contract_hashes": ["sha256:strategy"],
        "execution_assumption_hashes": [{"name": "cost_model", "hash": "sha256:cost"}],
        "candidate_fingerprints": [{
            "candidate_id": "candidate_a",
            "effective_strategy_parameters_hash": "sha256:params",
            "acceptance_gate_status": "PASS",
            "gate_fail_reasons": [],
            "primary_scenario_id": "base",
            "strategy_contract_hash": "sha256:strategy",
            "scenarios": [{
                "scenario_index": 0,
                "scenario_id": "base",
                "scenario_role": "base",
                "behavior_hash": "sha256:behavior",
                "strategy_behavior_hash": "sha256:strategy-behavior",
                "trade_ledger_hash": "sha256:ledger",
                "equity_curve_hash": "sha256:equity",
                "metrics_hash": "sha256:metrics",
                "composite_behavior_hash": "sha256:composite",
                "execution_model_hash": "sha256:execution",
                "portfolio_policy_hash": "sha256:portfolio",
            }],
        }],
        "final_selection": {"best_candidate_id": "candidate_a", "selected_candidate_id": "candidate_a", "validation_eligibility_status": "PASS", "statistical_gate_result": "PASS", "final_selection_gate_result": "PASS"},
    }
    from bithumb_research.research.hashing import sha256_prefixed

    fingerprint["stable_fingerprint_hash"] = sha256_prefixed(fingerprint)
    actual = copy.deepcopy(fingerprint)
    actual["candidate_fingerprints"][0]["scenarios"][0]["trade_ledger_hash"] = "sha256:changed"
    actual_without_hash = {key: value for key, value in actual.items() if key != "stable_fingerprint_hash"}
    actual["stable_fingerprint_hash"] = sha256_prefixed(actual_without_hash)

    comparison = compare_reproduction_fingerprints(fingerprint, actual)

    assert comparison.status == "DRIFT"
    assert any(item["path"] == "candidate_fingerprints[0].scenarios[0].trade_ledger_hash" for item in comparison.mismatches)


@pytest.mark.parametrize("mutation", ("hash", "missing", "schema"))
def test_receipt_validation_fails_closed_when_tampered(tmp_path: Path, mutation: str) -> None:
    _, _, _, report = _run_report(tmp_path)
    path = Path(str(report["reproduction_receipt_path"]))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "hash":
        payload["receipt_content_hash"] = "sha256:tampered"
    elif mutation == "missing":
        payload.pop("stable_fingerprint")
    else:
        payload["schema_version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReproductionContractError):
        load_reproduction_receipt(path)
