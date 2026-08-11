from __future__ import annotations

from tests.dataset_provenance_fixture import TEST_SOURCE_PROVENANCE
from tests.clean_provenance_fixture import install_committed_checkout_provenance

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from market_research.paths import ResearchPathManager
from market_research.settings import ResearchSettings
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from market_research.research.experiment_registry import (
    validate_experiment_registry_binding,
)
from market_research.research.final_selection import (
    apply_final_selection_contract,
    build_selection_artifact,
    validate_confirmation_artifact,
    validate_final_selection_report,
)
from market_research.research.hashing import (
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research_composition import (
    parse_builtin_manifest as parse_manifest,
)
from market_research.research.validation_protocol import (
    ResearchValidationError,
    _publish_candidate_result_artifacts,
    resolve_candidate_result_artifact,
    run_final_holdout_confirmation,
    run_research_backtest,
)
from market_research.research.reproduction import load_reproduction_receipt
from market_research.research_composition import builtin_strategy_registry
from market_research.research.validation_pipeline import run_research_validation


def _ts(day: str, minute: int = 0) -> int:
    return (
        int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)
        + minute * 60_000
    )


def frozen_manifest_and_manager(
    tmp_path: Path,
    *,
    walk_forward: bool = False,
    execution_mode: str = "serial",
    final_selection: bool = False,
    strategy_name: str = "noop_baseline",
):
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        for day_index in range(4):
            day = (
                (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=day_index))
                .date()
                .isoformat()
            )
            for minute in range(1440):
                price = 100.0 + day_index + minute / 10_000
                db.execute(
                    "INSERT INTO candles VALUES (?,?,?,?,?,?,?,?)",
                    (
                        "KRW-BTC",
                        "1m",
                        _ts(day, minute),
                        price,
                        price,
                        price,
                        price,
                        1.0,
                    ),
                )
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=_ts("2026-01-01"),
        end_ts=_ts("2026-01-04", 1439),
        out_dir=tmp_path / "frozen",
    )
    payload = {
        "experiment_id": "frozen_integration",
        "hypothesis": "frozen artifact integration",
        "strategy_name": strategy_name,
        "research_classification": "research_only",
        "market": "KRW-BTC",
        "interval": "1m",
        "dataset": {
            "source": "frozen_sqlite_candles",
            "snapshot_id": "frozen-integration",
            "artifact_manifest_uri": frozen["artifact_manifest_uri"],
            "artifact_manifest_hash": frozen["artifact_manifest_hash"],
            "train": {"start": "2026-01-01", "end": "2026-01-01"},
            "validation": {"start": "2026-01-02", "end": "2026-01-03"},
            "final_holdout": {"start": "2026-01-04", "end": "2026-01-04"},
        },
        "parameter_space": (
            {
                "BUY_HOLD_BUY_INDEX": [1],
                "BUY_HOLD_DECISION_REASON": ["frozen_confirmation"],
            }
            if strategy_name == "buy_and_hold_baseline"
            else {"NOOP_DECISION_START_INDEX": [0]}
        ),
        "cost_model": {"fee_rate": 0.0, "slippage_bps": [0.0]},
        "acceptance_gate": {
            "min_trade_count": 1,
            "max_mdd_pct": 100,
            "min_profit_factor": 0.1,
            "oos_return_must_be_positive": False,
            "parameter_stability_required": False,
            "final_holdout_required_for_validation": False,
            "metrics_contract_required": False,
            "reject_open_position_at_end": False,
        },
        "research_run": {
            "execution": {
                "mode": execution_mode,
                "max_workers": 2 if execution_mode == "parallel" else 1,
                "process_start_method": "auto_safe",
                "work_unit": "candidate_scenario",
            }
        },
    }
    if walk_forward:
        payload["walk_forward"] = {
            "train_window_days": 1,
            "test_window_days": 1,
            "step_days": 1,
            "min_windows": 2,
        }
    if final_selection:
        payload["final_selection"] = {
            "schema_version": 2,
            "required_for_validation": False,
            "candidate_universe": "acceptance_gate_passed_required_scenarios",
            "must_pass": {"dataset_quality_gate_status": "PASS"},
            "selection_exposure_policy": {
                "final_holdout_usage": "prohibited_during_selection",
                "counts_as_holdout_reuse": False,
            },
            "method": "lexicographic",
            "null_metric_policy": "fail_if_required_else_worst_rank",
            "ranking": [
                {
                    "metric": "validation.metrics_v2.return_risk.total_return_pct",
                    "order": "desc",
                    "required": True,
                },
                {"metric": "parameter_candidate_id", "order": "asc", "required": True},
            ],
            "unsupported_metric_policy": {
                "sharpe_ratio": "fail_if_required",
                "sortino_ratio": "fail_if_required",
            },
        }
    manifest = parse_manifest(payload)
    settings = ResearchSettings(
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=None,
        max_workers=1,
        random_seed=0,
    )
    return (
        frozen,
        manifest,
        ResearchPathManager.from_settings(settings, project_root=Path.cwd()),
    )


def _prepare_confirmable_single_candidate_report(
    *,
    report: dict,
    manifest,
    manager: ResearchPathManager,
) -> None:
    """Replace a compact failed candidate with a verified, republished PASS row."""

    compact_candidate = report["candidates"][0]
    candidate = deepcopy(
        resolve_candidate_result_artifact(
            manager=manager,
            compact_candidate=compact_candidate,
            expected_experiment_id=manifest.experiment_id,
            expected_manifest_hash=manifest.manifest_hash(),
            expected_dataset_snapshot_id=str(report.get("dataset_snapshot_id") or ""),
            expected_dataset_content_hash=str(report.get("dataset_content_hash") or ""),
        )
    )
    candidate.pop("final_selection_input", None)
    candidate["aggregate_acceptance_gate_result"] = "PASS"
    candidate["acceptance_gate_result"] = "PASS"
    _publish_candidate_result_artifacts(
        report={"candidates": [candidate]},
        manifest=manifest,
        manager=manager,
        artifact_context=None,
    )
    selection = apply_final_selection_contract(
        contract=manifest.final_selection,
        candidates=[candidate],
        report_context={"dataset_quality_gate_status": "PASS"},
        validation_required=False,
    )
    report.update(
        {
            "final_selection_contract": selection["final_selection_contract"],
            "final_selection_contract_hash": selection["final_selection_contract_hash"],
            "final_selection_gate_result": selection["gate_result"],
            "final_selection_fail_reasons": selection["fail_reasons"],
            "selected_candidate_id": selection["selected_candidate_id"],
            "best_candidate_id": selection["selected_candidate_id"],
            "selected_candidate_score_hash": selection["selected_candidate_score_hash"],
            "candidate_final_scores_hash": selection["candidate_final_scores_hash"],
            "candidate_final_scores": selection["candidate_final_scores"],
            "candidates": [candidate],
        }
    )
    selection_artifact = build_selection_artifact(
        manifest_hash=manifest.manifest_hash(),
        selection_result=selection,
        candidates=[candidate],
    )
    assert selection_artifact is not None
    report["selection_artifact"] = selection_artifact
    report["selection_artifact_hash"] = selection_artifact["content_hash"]
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))


def test_backtest_candidate_search_does_not_materialize_final_holdout(
    tmp_path, monkeypatch
) -> None:
    install_committed_checkout_provenance(monkeypatch)
    frozen, manifest, manager = frozen_manifest_and_manager(tmp_path)
    report = run_research_backtest(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )
    splits = report["dataset_splits"]
    assert set(splits) == {"train", "validation"}
    assert {
        splits[name]["artifact_manifest_hash"] for name in ("train", "validation")
    } == {frozen["artifact_manifest_hash"]}
    assert all(splits[name]["verification_status"] == "VERIFIED" for name in splits)
    adapter_evidence = report["dataset_adapter_provenance"][
        "adapter_provenance_by_split"
    ]
    assert {
        evidence["source_provenance"]["semantics"]["observation_calendar"]
        for evidence in adapter_evidence.values()
    } == {"continuous_24x7"}
    assert {
        tuple(evidence["source_provenance"]["source_priority"])
        for evidence in adapter_evidence.values()
    } == {("test-provider",)}
    assert all(
        splits[name]["source_provenance_hash"].startswith("sha256:") for name in splits
    )
    assert report["reproduction_receipt_path"]
    receipt = load_reproduction_receipt(report["reproduction_receipt_path"])
    receipt_splits = {
        item["split_name"]: item
        for item in receipt["stable_fingerprint"]["dataset_split_hashes"]
    }
    assert set(receipt_splits) == {"train", "validation"}
    for split_name, row in report["dataset_splits"].items():
        for field in (
            "artifact_id",
            "artifact_manifest_hash",
            "artifact_content_hash",
            "artifact_schema_hash",
            "requested_range",
            "snapshot_data_hash",
            "snapshot_query_hash",
            "snapshot_fingerprint_hash",
            "quality_hash",
            "verification_status",
            "verification",
        ):
            assert receipt_splits[split_name][field] == row[field]


def test_parallel_frozen_backtest_without_db(tmp_path) -> None:
    _, manifest, manager = frozen_manifest_and_manager(
        tmp_path, execution_mode="parallel"
    )
    assert (
        run_research_backtest(
            manifest=manifest,
            db_path=None,
            manager=manager,
            strategy_registry=builtin_strategy_registry(),
        )["dataset_splits"]["train"]["verification_status"]
        == "VERIFIED"
    )


def test_serial_and_process_parallel_stochastic_backtest_are_causally_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduling identity may change evidence, never bounded market behavior."""

    install_committed_checkout_provenance(monkeypatch)
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    _, base_manifest, _ = frozen_manifest_and_manager(
        fixture_root,
        strategy_name="buy_and_hold_baseline",
    )
    base_payload = deepcopy(base_manifest.raw)
    base_payload["execution_model"] = {
        "scenario_policy": "must_pass_base_and_survive_stress",
        "scenarios": [
            {
                "type": "fixed_bps",
                "scenario_role": "base",
                "label": "serial_parallel_base",
                "fee_rate": 0.0,
                "fee_source": "immutable_test_fixture",
                "fee_authority_policy": "research_declared_reference",
                "slippage_bps": 0.0,
                "slippage_source": "immutable_test_fixture",
                "validation_eligible_as_base": True,
                "latency_ms": 0,
                "partial_fill_rate": 0.0,
                "order_failure_rate": 0.0,
                "market_order_extra_cost_bps": 0.0,
                "seed": 7,
            },
            {
                "type": "stress",
                "scenario_role": "stress",
                "label": "serial_parallel_stochastic_stress",
                "fee_rate": 0.0,
                "fee_source": "immutable_test_fixture",
                "fee_authority_policy": "research_declared_reference",
                "slippage_bps": 5.0,
                "slippage_source": "immutable_test_fixture",
                "validation_eligible_as_base": False,
                "latency_ms": 7,
                "partial_fill_rate": 0.65,
                "order_failure_rate": 0.2,
                "market_order_extra_cost_bps": 3.0,
                "seed": 11,
            },
        ],
    }

    reports: dict[str, Any] = {}
    manifests = {}
    managers: dict[str, ResearchPathManager] = {}
    for mode, max_workers in (("serial", 1), ("parallel", 2)):
        payload = deepcopy(base_payload)
        payload["research_run"]["execution"] = {
            "mode": mode,
            "max_workers": max_workers,
            "process_start_method": "auto_safe",
            "work_unit": "candidate_scenario",
        }
        manifest = parse_manifest(payload)
        settings = ResearchSettings(
            data_root=tmp_path / mode / "data",
            artifact_root=tmp_path / mode / "artifacts",
            report_root=tmp_path / mode / "reports",
            cache_root=tmp_path / mode / "cache",
            db_path=None,
            max_workers=max_workers,
            random_seed=0,
        )
        manager = ResearchPathManager.from_settings(
            settings,
            project_root=Path.cwd(),
        )
        reports[mode] = run_research_backtest(
            manifest=manifest,
            db_path=None,
            manager=manager,
            strategy_registry=builtin_strategy_registry(),
        )
        manifests[mode] = manifest
        managers[mode] = manager

    candidates: dict[str, Any] = {
        mode: resolve_candidate_result_artifact(
            manager=managers[mode],
            compact_candidate=report["candidates"][0],
            expected_experiment_id=manifests[mode].experiment_id,
            expected_manifest_hash=manifests[mode].manifest_hash(),
            expected_dataset_snapshot_id=str(report["dataset_snapshot_id"]),
            expected_dataset_content_hash=str(report["dataset_content_hash"]),
        )
        for mode, report in reports.items()
    }
    serial_candidate = candidates["serial"]
    parallel_candidate = candidates["parallel"]

    assert (
        serial_candidate["candidate_behavior_profile_hash"]
        == parallel_candidate["candidate_behavior_profile_hash"]
    )
    causal_scenario_fields = (
        "behavior_hash",
        "common_decision_behavior_hash",
        "decision_behavior_hash",
        "strategy_behavior_hash",
        "composite_behavior_hash",
        "trade_ledger_hash",
        "train_behavior_hash",
        "validation_behavior_hash",
        "train_execution_metadata",
        "validation_execution_metadata",
        "train_execution_event_summary",
        "validation_execution_event_summary",
        "train_metrics",
        "validation_metrics",
        "train_metrics_v2",
        "validation_metrics_v2",
        "train_equity_curve",
        "validation_equity_curve",
    )
    serial_scenarios = {
        row["scenario_id"]: row for row in serial_candidate["scenario_results"]
    }
    parallel_scenarios = {
        row["scenario_id"]: row for row in parallel_candidate["scenario_results"]
    }
    assert set(serial_scenarios) == set(parallel_scenarios)
    assert any(row["scenario_type"] == "stress" for row in serial_scenarios.values())
    for scenario_id in sorted(serial_scenarios):
        serial_scenario = serial_scenarios[scenario_id]
        parallel_scenario = parallel_scenarios[scenario_id]
        assert {
            field: serial_scenario.get(field) for field in causal_scenario_fields
        } == {
            field: parallel_scenario.get(field) for field in causal_scenario_fields
        }

    def derived_seeds(candidate: dict[str, Any]) -> list[tuple[object, ...]]:
        rows: list[tuple[object, ...]] = []
        for scenario in candidate["scenario_results"]:
            for split_name in ("train", "validation"):
                for fill in scenario[f"{split_name}_execution_metadata"]:
                    if fill.get("derived_seed_hash") is not None:
                        rows.append(
                            (
                                scenario["scenario_id"],
                                split_name,
                                fill.get("decision_id"),
                                fill.get("intent_id"),
                                fill.get("fill_status"),
                                fill.get("base_seed"),
                                fill.get("derived_seed_hash"),
                                fill.get("seed_derivation_inputs"),
                            )
                        )
        return rows

    serial_seeds = derived_seeds(serial_candidate)
    assert serial_seeds
    assert derived_seeds(parallel_candidate) == serial_seeds

    serial_report = reports["serial"]
    parallel_report = reports["parallel"]
    assert serial_report["manifest_hash"] != parallel_report["manifest_hash"]
    assert serial_report["content_hash"] != parallel_report["content_hash"]
    assert (
        serial_report["candidates"][0]["candidate_result_artifact_hash"]
        != parallel_report["candidates"][0]["candidate_result_artifact_hash"]
    )
    for mode, report in reports.items():
        plan = report["execution_plan"]
        execution = report["execution_observability"]
        assert plan["manifest_hash"] == report["manifest_hash"]
        assert plan["execution_mode"] == mode
        assert execution["requested_execution_mode"] == mode

    serial_execution = serial_report["execution_observability"]
    parallel_execution = parallel_report["execution_observability"]
    assert serial_execution["actual_execution_mode"] == "serial_validation_evaluator"
    assert serial_execution["parallel_executor_used"] is False
    assert serial_execution["worker_pid_set"] == []
    assert parallel_execution["actual_execution_mode"] == "parallel_worker_initializer"
    assert parallel_execution["parallel_executor_used"] is True
    assert parallel_execution["effective_process_start_method"] in {
        "forkserver",
        "spawn",
    }
    assert parallel_execution["observed_worker_count"] == 2
    parallel_worker_pids = set(parallel_execution["worker_pid_set"])
    assert len(parallel_worker_pids) == 2
    assert parallel_execution["parent_pid"] not in parallel_worker_pids

    serial_work = {
        row["work_unit"]["scenario_id"]: row
        for row in serial_execution["work_units"]
    }
    parallel_work = {
        row["work_unit"]["scenario_id"]: row
        for row in parallel_execution["work_units"]
    }
    assert set(serial_work) == set(parallel_work)
    for scenario_id in sorted(serial_work):
        serial_row = serial_work[scenario_id]
        parallel_row = parallel_work[scenario_id]
        assert serial_row["work_unit"] == parallel_row["work_unit"]
        assert serial_row["content_hash"] == parallel_row["content_hash"]
        assert serial_row["worker_process_evidence"]["input_hash"] == (
            parallel_row["worker_process_evidence"]["input_hash"]
        )
        assert serial_row["worker_process_evidence"]["output_hash"] == (
            parallel_row["worker_process_evidence"]["output_hash"]
        )
        assert serial_row["worker_process_evidence"]["worker_pid"] is None
        assert parallel_row["worker_process_evidence"]["worker_pid"] in (
            parallel_worker_pids
        )


def test_final_holdout_confirmation_executes_only_receipt_candidate(
    tmp_path, monkeypatch
) -> None:
    _, manifest, manager = frozen_manifest_and_manager(
        tmp_path,
        final_selection=True,
        strategy_name="buy_and_hold_baseline",
    )
    manifest = replace(
        manifest, raw={**manifest.raw, "objective_metric": "total_return_pct"}
    )
    report = run_research_backtest(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )
    _prepare_confirmable_single_candidate_report(
        report=report,
        manifest=manifest,
        manager=manager,
    )

    confirmation = run_final_holdout_confirmation(
        manifest=manifest,
        selection_report=report,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )

    assert len(confirmation["candidate_results"]) == 1
    assert (
        confirmation["candidate_results"][0]["candidate_id"]
        == report["selection_artifact"]["selected_candidate_id"]
    )
    assert confirmation["selection_artifact_hash"] == report["selection_artifact_hash"]
    assert (
        confirmation["experiment_registry_row_hash"]
        == confirmation["authorization_row_hash"]
    )
    assert (
        confirmation["experiment_registry_completion_row_hash"]
        == confirmation["completion_row_hash"]
    )
    assert (
        validate_confirmation_artifact(
            confirmation,
            selection_artifact=report["selection_artifact"],
        )
        == []
    )
    assert (
        validate_experiment_registry_binding(
            report=confirmation,
            require_complete=True,
        )
        == []
    )
    assert confirmation["declared_attempt_index"] is None
    assert confirmation["computed_attempt_index"] == 1
    assert confirmation["declared_holdout_reuse_count"] is None
    assert confirmation["computed_holdout_reuse_count"] == 0
    assert confirmation["selection_attempt_index"] == 1
    assert confirmation["selection_holdout_reuse_count"] == 0

    changed_confirmation = {
        **confirmation,
        "candidate_results": [
            {
                **confirmation["candidate_results"][0],
                "compiled_strategy_contract_hash": "sha256:" + "f" * 64,
            }
        ],
    }
    changed_material = {
        key: value
        for key, value in changed_confirmation.items()
        if key not in {"content_hash", "confirmation_artifact_path"}
    }
    changed_confirmation["content_hash"] = sha256_prefixed(
        changed_material,
        label="final_holdout_confirmation",
    )
    assert "final_holdout_confirmation_compiled_contract_hash_mismatch" in (
        validate_confirmation_artifact(
            changed_confirmation,
            selection_artifact=report["selection_artifact"],
        )
    )

    monkeypatch.setattr(
        "market_research.research.validation_protocol.load_dataset_split",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("holdout loaded before authorization")
        ),
    )
    with pytest.raises(
        ResearchValidationError, match="pre_exposure_authorization_failed"
    ):
        run_final_holdout_confirmation(
            manifest=manifest,
            selection_report=report,
            db_path=None,
            manager=manager,
            strategy_registry=builtin_strategy_registry(),
        )


def test_selection_report_contains_no_final_holdout_metrics(tmp_path) -> None:
    _, manifest, manager = frozen_manifest_and_manager(
        tmp_path,
        final_selection=True,
        strategy_name="buy_and_hold_baseline",
    )
    report = run_research_backtest(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )

    def keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield str(key)
                yield from keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from keys(item)

    assert all(
        "final_holdout" not in key
        for candidate in report["candidates"]
        for key in keys(candidate)
    )
    assert "best_final_holdout_metrics_v2" not in report


def test_research_validate_executes_final_holdout_exactly_once(
    tmp_path, monkeypatch
) -> None:
    _, manifest, manager = frozen_manifest_and_manager(
        tmp_path,
        final_selection=True,
        strategy_name="buy_and_hold_baseline",
    )
    manifest = replace(
        manifest, raw={**manifest.raw, "objective_metric": "total_return_pct"}
    )
    selection_report = run_research_backtest(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )
    _prepare_confirmable_single_candidate_report(
        report=selection_report,
        manifest=manifest,
        manager=manager,
    )
    calls = []
    actual_confirmation = run_final_holdout_confirmation

    monkeypatch.setattr(
        "market_research.research.validation_pipeline.run_research_backtest",
        lambda **_kwargs: selection_report,
    )

    def confirm_once(**kwargs):
        calls.append(
            kwargs["selection_report"]["selection_artifact"]["selected_candidate_id"]
        )
        return actual_confirmation(**kwargs)

    monkeypatch.setattr(
        "market_research.research.validation_pipeline.run_final_holdout_confirmation",
        confirm_once,
    )

    summary = run_research_validation(
        manifest=manifest,
        db_path=None,
        manager=manager,
        manifest_path=str(tmp_path / "manifest.json"),
        strategy_registry=builtin_strategy_registry(),
    )

    assert calls == [selection_report["selection_artifact"]["selected_candidate_id"]]
    assert (
        summary["final_holdout_confirmation"]["candidate_results"][0]["candidate_id"]
        == calls[0]
    )
    assert summary["schema_version"] == 3
    assert summary["artifact_type"] == "validated_research_result"
    assert (
        summary["final_selection_contract"]
        == selection_report["final_selection_contract"]
    )
    assert summary["content_hash"] == sha256_prefixed(
        report_content_hash_payload(summary)
    )
    assert validate_final_selection_report(summary) == []
    validation_path = manager.report_path(
        "research", manifest.experiment_id, "validation_summary.json"
    )
    assert json.loads(validation_path.read_text(encoding="utf-8")) == summary
    report_path = manager.report_path(
        "research", manifest.experiment_id, "research_candidate_report.json"
    )
    assert report_path.is_file()
    decision_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert decision_report["content_hash"] == summary["research_candidate_report_hash"]
    assert (
        decision_report["sections"]["research_conclusion"]["operational_permission"]
        is False
    )
    assert not (
        manager.artifact_root / "reports" / "research" / manifest.experiment_id
    ).exists()
