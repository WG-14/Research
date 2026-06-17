from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.resource_planner import ResourceContract
from bithumb_bot.research.validation_protocol import (
    _canonicalize_runner_default_execution,
    run_research_backtest,
)
from bithumb_bot.research.workload_estimate import build_manifest_workload_estimate
from tests.factories.research_reports import DeterministicResearchEvaluator, assert_fast_research_workload
from tests.test_research_backtest_reproducibility import _create_db, _manifest, _research_manager


def _resource_contract() -> ResourceContract:
    return ResourceContract(
        cpu_limit=8,
        memory_limit_mb=12 * 1024,
        swap_limit_mb=None,
        detected_source="test_resource_contract",
        env_worker_cap=None,
        total_process_budget=None,
    )


def _auto_manifest_payload() -> dict[str, object]:
    payload = _manifest()
    payload["experiment_id"] = "auto_execution_policy"
    payload["parameter_space"] = {
        "SMA_SHORT": [2, 3],
        "SMA_LONG": [4, 5, 6, 7],
        "SMA_FILTER_GAP_MIN_RATIO": [0.0],
        "SMA_FILTER_VOL_MIN_RANGE_RATIO": [0.0],
    }
    payload["dataset"] = {
        **dict(payload["dataset"]),
        "final_holdout": None,
    }
    payload["research_run"] = {"resource_limits": {"memory_admission_policy": "cap_workers"}}
    return payload


def _serial_manifest_payload() -> dict[str, object]:
    payload = _auto_manifest_payload()
    payload["experiment_id"] = "explicit_serial_execution_policy"
    payload["research_run"] = {
        **dict(payload["research_run"]),
        "execution": {"mode": "serial", "max_workers": 1},
    }
    return payload


def _parallel_manifest_payload() -> dict[str, object]:
    payload = _auto_manifest_payload()
    payload["experiment_id"] = "explicit_parallel_execution_policy"
    payload["research_run"] = {
        **dict(payload["research_run"]),
        "execution": {"mode": "parallel", "max_workers": 8},
    }
    return payload


def _run_contract_research_backtest(payload: dict[str, object], tmp_path: Path, monkeypatch) -> dict[str, object]:
    monkeypatch.setattr(
        "bithumb_bot.research.resource_planner.detect_resource_contract",
        lambda: _resource_contract(),
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "candles.sqlite"
    _create_db(db_path)
    report = run_research_backtest(
        manifest=parse_manifest(payload),
        db_path=db_path,
        manager=_research_manager(tmp_path, monkeypatch),
        generated_at="2026-06-17T00:00:00+00:00",
        candidate_evaluator=DeterministicResearchEvaluator(),
    )
    assert_fast_research_workload(
        report,
        max_strategy_runs=16,
        max_tick_events=25_000,
        max_matrix_size=16,
        max_artifact_write_count=70,
    )
    return report


def test_backtest_auto_execution_uses_resource_plan_when_execution_block_omitted(tmp_path, monkeypatch) -> None:
    payload = _auto_manifest_payload()
    manifest = parse_manifest(payload)

    assert "execution" not in manifest.raw["research_run"]

    report = _run_contract_research_backtest(payload, tmp_path, monkeypatch)
    observability = report["execution_observability"]

    assert observability["parallel_executor_used"] is False
    assert observability["contract_evaluator_used"] is True
    assert observability["research_max_workers_effective"] == 1
    assert observability["resource_plan"]["requested_execution_mode"] == "auto"
    assert observability["resource_plan"]["effective_execution_mode"] == "parallel"
    assert observability["resource_plan"]["effective_max_workers"] == 8


def test_runner_default_canonicalization_preserves_execution_omission_for_resource_planner() -> None:
    manifest = parse_manifest(_auto_manifest_payload())

    canonical = _canonicalize_runner_default_execution(manifest)

    assert "execution" not in canonical.raw["research_run"]
    assert canonical.manifest_input_provenance.research_run.execution.mode_declared is False


def test_backtest_auto_execution_matches_workload_estimate_when_execution_block_omitted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "bithumb_bot.research.resource_planner.detect_resource_contract",
        lambda: _resource_contract(),
    )
    payload = _auto_manifest_payload()
    manifest = parse_manifest(payload)
    estimate = build_manifest_workload_estimate(manifest)

    report = _run_contract_research_backtest(payload, tmp_path, monkeypatch)
    observed = report["execution_observability"]

    assert estimate["resource_plan"]["effective_execution_mode"] == observed["resource_plan"]["effective_execution_mode"]
    assert estimate["resource_plan"]["effective_max_workers"] == observed["resource_plan"]["effective_max_workers"]
    assert (
        estimate["work_unit_selection"]["effective_work_unit_type"]
        == observed["work_unit_selection"]["effective_work_unit_type"]
    )


def test_memory_admission_cap_records_cap_reason_for_auto_backtest(tmp_path, monkeypatch) -> None:
    payload = _auto_manifest_payload()
    payload["experiment_id"] = "auto_execution_memory_cap"
    research_run = dict(payload["research_run"])
    resource_limits = dict(research_run["resource_limits"])
    resource_limits["max_total_memory_mb"] = 1
    resource_limits["memory_admission_policy"] = "cap_workers"
    research_run["resource_limits"] = resource_limits
    payload["research_run"] = research_run

    report = _run_contract_research_backtest(payload, tmp_path, monkeypatch)
    observability = report["execution_observability"]
    memory_admission = observability["memory_admission"]

    assert observability["research_max_workers_effective"] == 1
    assert memory_admission["action"] == "cap_workers"
    assert memory_admission["safe_max_workers_by_memory_budget"] == 1
    assert "estimated_parent_and_worker_bytes_exceed_memory_budget" in memory_admission["memory_budget_reasons"]


def test_explicit_serial_execution_is_not_auto_parallelized(tmp_path, monkeypatch) -> None:
    report = _run_contract_research_backtest(_serial_manifest_payload(), tmp_path, monkeypatch)
    observability = report["execution_observability"]

    assert observability["parallel_executor_used"] is False
    assert observability["research_max_workers_effective"] == 1
    assert observability["resource_plan"]["requested_execution_mode"] == "serial"
    assert observability["resource_plan"]["effective_execution_mode"] == "serial"


def test_explicit_parallel_execution_still_requests_parallel_plan(tmp_path, monkeypatch) -> None:
    report = _run_contract_research_backtest(_parallel_manifest_payload(), tmp_path, monkeypatch)
    observability = report["execution_observability"]

    assert observability["parallel_executor_used"] is False
    assert observability["contract_evaluator_used"] is True
    assert observability["research_max_workers_effective"] == 1
    assert observability["resource_plan"]["requested_execution_mode"] == "parallel"
    assert observability["resource_plan"]["effective_execution_mode"] == "parallel"


def test_auto_manifest_reports_resource_planner_policy_source(tmp_path, monkeypatch) -> None:
    report = _run_contract_research_backtest(_auto_manifest_payload(), tmp_path, monkeypatch)
    resource_plan = report["execution_observability"]["resource_plan"]

    assert resource_plan["execution_mode_source"] == "resource_planner"
    assert resource_plan["max_workers_source"] == "resource_planner"


def test_explicit_serial_reports_user_explicit_policy_source(tmp_path, monkeypatch) -> None:
    report = _run_contract_research_backtest(_serial_manifest_payload(), tmp_path, monkeypatch)
    resource_plan = report["execution_observability"]["resource_plan"]

    assert resource_plan["execution_mode_source"] == "user_explicit"
    assert resource_plan["max_workers_source"] == "user_explicit"


def test_auto_and_explicit_serial_produce_different_outcomes(tmp_path, monkeypatch) -> None:
    auto_report = _run_contract_research_backtest(_auto_manifest_payload(), tmp_path / "auto", monkeypatch)
    serial_report = _run_contract_research_backtest(_serial_manifest_payload(), tmp_path / "serial", monkeypatch)

    assert auto_report["execution_observability"]["resource_plan"]["effective_execution_mode"] == "parallel"
    assert serial_report["execution_observability"]["parallel_executor_used"] is False
    assert serial_report["execution_observability"]["resource_plan"]["effective_execution_mode"] == "serial"


def test_canonicalized_raw_default_does_not_override_omitted_provenance(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "bithumb_bot.research.resource_planner.detect_resource_contract",
        lambda: _resource_contract(),
    )
    manifest = parse_manifest(_auto_manifest_payload())
    raw = dict(manifest.raw)
    raw["research_run"] = {
        **dict(raw["research_run"]),
        "execution": {"mode": "serial", "max_workers": 1},
    }
    manifest = replace(manifest, raw=raw)

    estimate = build_manifest_workload_estimate(manifest)

    assert estimate["resource_plan"]["requested_execution_mode"] == "auto"
    assert estimate["resource_plan"]["effective_execution_mode"] == "parallel"
