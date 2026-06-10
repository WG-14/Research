from __future__ import annotations

import json
from pathlib import Path

from bithumb_bot.research.experiment_manifest import parse_manifest
from tests.test_research_backtest_reproducibility import (
    _create_db,
    _manifest,
    _research_manager,
    _run_contract_research_backtest,
)


def _contains_key(payload: object, key: str) -> bool:
    if isinstance(payload, dict):
        return key in payload or any(_contains_key(value, key) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_key(value, key) for value in payload)
    return False


def _run_report(tmp_path, monkeypatch, *, experiment_id: str) -> dict[str, object]:
    db_path = tmp_path / f"{experiment_id}.sqlite"
    _create_db(db_path)
    payload = _manifest()
    payload["experiment_id"] = experiment_id
    payload["research_run"] = {"report_detail": "summary", "execution": {"mode": "serial"}}
    return _run_contract_research_backtest(
        manifest=parse_manifest(payload),
        db_path=db_path,
        manager=_research_manager(tmp_path, monkeypatch),
        generated_at="2026-05-03T00:00:00+00:00",
    )


def test_candidate_result_write_timing_is_recorded(tmp_path, monkeypatch) -> None:
    report = _run_report(tmp_path, monkeypatch, experiment_id="candidate_artifact_timing")

    persisted = json.loads(Path(report["artifact_paths"]["report_path"]).read_text(encoding="utf-8"))
    artifact_obs = persisted["artifact_observability"]["candidate_results"]
    assert artifact_obs["candidate_result_write_wall_seconds"] >= 0
    stages = {item["stage"] for item in persisted["execution_observability"]["stage_timings"]}
    assert "candidate_evaluation" in stages
    assert "candidate_evaluation.candidate_result_artifact_write" in stages


def test_candidate_result_file_count_and_bytes_are_recorded(tmp_path, monkeypatch) -> None:
    report = _run_report(tmp_path, monkeypatch, experiment_id="candidate_artifact_bytes")

    persisted = json.loads(Path(report["artifact_paths"]["report_path"]).read_text(encoding="utf-8"))
    artifact_obs = persisted["artifact_observability"]["candidate_results"]
    result_dir = Path(persisted["artifact_paths"]["candidate_results_dir"])
    result_paths = sorted(result_dir.glob("candidate_*.json"))
    assert artifact_obs["candidate_result_file_count"] == len(result_paths)
    assert artifact_obs["candidate_result_total_bytes"] == sum(path.stat().st_size for path in result_paths)
    for result_path in result_paths:
        candidate_result = json.loads(result_path.read_text(encoding="utf-8"))
        assert not _contains_key(candidate_result, "stage_trace")


def test_summary_candidate_result_retains_compact_strategy_diagnostics(tmp_path, monkeypatch) -> None:
    report = _run_report(tmp_path, monkeypatch, experiment_id="candidate_artifact_diagnostics")
    result_dir = Path(report["artifact_paths"]["candidate_results_dir"])
    candidate_result = json.loads(sorted(result_dir.glob("candidate_*.json"))[0].read_text(encoding="utf-8"))

    diagnostics = candidate_result["validation_strategy_diagnostics"]
    assert diagnostics is not None
    assert "strategy_diagnostics_namespace" in diagnostics
    assert "blocked_filter_distribution" in diagnostics
    assert isinstance(diagnostics["blocked_filter_distribution"], dict)
    assert candidate_result["scenario_results"][0]["validation_strategy_diagnostics"] is not None


def test_summary_candidate_result_does_not_retain_full_decisions_or_stage_trace(tmp_path, monkeypatch) -> None:
    report = _run_report(tmp_path, monkeypatch, experiment_id="candidate_artifact_bounded")
    result_dir = Path(report["artifact_paths"]["candidate_results_dir"])
    candidate_result = json.loads(sorted(result_dir.glob("candidate_*.json"))[0].read_text(encoding="utf-8"))

    assert not _contains_key(candidate_result, "decisions")
    assert not _contains_key(candidate_result, "stage_trace")


def test_report_detail_levels_do_not_write_stage_trace_to_candidate_result() -> None:
    from bithumb_bot.research.report_writer import summarize_candidate_result

    candidate = {
        "parameter_candidate_id": "candidate_001",
        "validation_resource_usage": {"stage_trace": [{"stage": "validation"}]},
        "scenario_results": [
            {"scenario_id": "scenario_001", "validation_resource_usage": {"stage_trace": [{"stage": "s"}]}}
        ],
    }

    for detail in ("index", "summary", "standard", "full"):
        payload = summarize_candidate_result(candidate, detail)
        assert not _contains_key(payload, "stage_trace")
