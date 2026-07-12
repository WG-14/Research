from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.experiment_manifest import load_manifest
from market_research.research.reproduction import (
    REPRODUCTION_FINGERPRINT_SCHEMA_VERSION,
    ReproductionContractError,
    build_reproduction_fingerprint,
    compare_reproduction_fingerprints,
    load_reproduction_receipt,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.validation_protocol import run_research_backtest
from market_research.settings import ResearchSettings
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
    digest = lambda value: sha256_prefixed({"value": value})
    fingerprint = {
        "schema_version": REPRODUCTION_FINGERPRINT_SCHEMA_VERSION,
        "manifest_hash": digest("manifest"),
        "research_classification": "research_only",
        "dataset_fingerprint": digest("dataset"),
        "dataset_split_hashes": [{"split_name": "train", "content_hash": digest("train"), "quality_hash": digest("quality"), "snapshot_data_hash": digest("data"), "snapshot_query_hash": digest("query"), "snapshot_fingerprint_hash": digest("fingerprint"), "artifact_id": "artifact", "artifact_manifest_hash": digest("manifest-artifact"), "artifact_content_hash": digest("content-artifact"), "artifact_schema_hash": digest("schema-artifact"), "verification_status": "VERIFIED", "verification": {"overall_status": "VERIFIED"}, "requested_range": {"start": "2026-01-01", "end": "2026-01-01"}}],
        "strategy_contract_hashes": [digest("plugin")],
        "execution_assumption_hashes": [{"name": "cost_model", "hash": digest("cost")}],
        "candidate_fingerprints": [{
            "candidate_id": "candidate_a",
            "effective_strategy_parameters_hash": digest("params"),
            "strategy_spec_hash": digest("spec"),
            "strategy_plugin_contract_hash": digest("plugin"),
            "acceptance_gate_status": "PASS",
            "gate_fail_reasons": [],
            "primary_scenario_id": "base",
            "scenarios": [{
                "scenario_index": 0,
                "scenario_id": "base",
                "scenario_role": "base",
                "behavior_hash": digest("behavior"),
                "strategy_behavior_hash": digest("strategy-behavior"),
                "trade_ledger_hash": digest("ledger"),
                "equity_curve_hash": digest("equity"),
                "metrics_hash": digest("metrics"),
                "composite_behavior_hash": digest("composite"),
                "execution_model_hash": digest("execution"),
                "portfolio_policy_hash": digest("portfolio"),
            }],
        }],
        "final_selection": {"best_candidate_id": "candidate_a", "selected_candidate_id": "candidate_a", "validation_eligibility_status": "PASS", "statistical_gate_result": "PASS", "final_selection_gate_result": "PASS"},
    }
    fingerprint["stable_fingerprint_hash"] = sha256_prefixed(fingerprint)
    actual = copy.deepcopy(fingerprint)
    actual["candidate_fingerprints"][0]["scenarios"][0]["trade_ledger_hash"] = digest("changed")
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


def test_fingerprint_rejects_classification_mismatch_and_invalid_hashes(tmp_path: Path) -> None:
    _, manifest_path, _, report = _run_report(tmp_path)
    manifest = load_manifest(manifest_path)

    changed = copy.deepcopy(report)
    changed["research_classification"] = "validated_candidate"
    with pytest.raises(ReproductionContractError, match="report.research_classification does not match manifest"):
        build_reproduction_fingerprint(changed, manifest=manifest)

    mutations = (
        (changed, "manifest_hash"),
        (changed, "dataset_content_hash"),
        (changed["candidates"][0], "strategy_plugin_contract_hash"),
        (changed["candidates"][0]["scenario_results"][0], "trade_ledger_hash"),
        (changed["candidates"][0]["scenario_results"][0], "metrics_hash"),
    )
    for target, key in mutations:
        invalid = copy.deepcopy(report)
        if target is changed:
            invalid[key] = "sha256:UPPERCASE"
        elif target is changed["candidates"][0]:
            invalid["candidates"][0][key] = "sha256:UPPERCASE"
        else:
            invalid["candidates"][0]["scenario_results"][0][key] = "sha256:UPPERCASE"
        with pytest.raises(ReproductionContractError, match="must be a sha256 hash"):
            build_reproduction_fingerprint(invalid, manifest=manifest)


@pytest.mark.parametrize(
    "key",
    (
        "strategy_plugin_contract_hash",
        "behavior_hash",
        "strategy_behavior_hash",
        "trade_ledger_hash",
        "equity_curve_hash",
        "metrics_hash",
        "composite_behavior_hash",
    ),
)
def test_fingerprint_requires_recorded_candidate_and_result_hashes(tmp_path: Path, key: str) -> None:
    _, manifest_path, _, report = _run_report(tmp_path)
    changed = copy.deepcopy(report)
    if key == "strategy_plugin_contract_hash":
        changed["candidates"][0].pop(key)
    else:
        changed["candidates"][0]["scenario_results"][0].pop(key)

    with pytest.raises(ReproductionContractError, match=rf"{key} is required"):
        build_reproduction_fingerprint(changed, manifest=load_manifest(manifest_path))


def test_receipt_rejects_invalid_stable_fingerprint_hash_format(tmp_path: Path) -> None:
    _, _, _, report = _run_report(tmp_path)
    path = Path(str(report["reproduction_receipt_path"]))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stable_fingerprint_hash"] = "sha256:uppercase"
    payload["stable_fingerprint"]["stable_fingerprint_hash"] = "sha256:uppercase"
    payload["receipt_content_hash"] = sha256_prefixed(
        {key: value for key, value in payload.items() if key != "receipt_content_hash"},
        label="reproduction_receipt_content",
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReproductionContractError, match="stable_fingerprint_hash must be a sha256 hash"):
        load_reproduction_receipt(path)
