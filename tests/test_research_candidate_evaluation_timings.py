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


def test_candidate_evaluation_records_parallel_worker_execution_timing(tmp_path, monkeypatch) -> None:
    report = _run_report(tmp_path, monkeypatch, experiment_id="candidate_eval_parallel_timing")

    persisted = json.loads(Path(report["artifact_paths"]["report_path"]).read_text(encoding="utf-8"))
    timings = {
        item["stage"]: item
        for item in persisted["execution_observability"]["stage_timings"]
    }
    assert "candidate_evaluation" in timings
    worker = timings["candidate_evaluation.parallel_worker_execution"]
    assert worker["task_count"] > 0
    assert worker["max_workers"] >= 1
    assert worker["wall_seconds"] >= 0


def test_candidate_evaluation_records_candidate_result_artifact_write_timing(tmp_path, monkeypatch) -> None:
    report = _run_report(tmp_path, monkeypatch, experiment_id="candidate_eval_artifact_timing")

    persisted = json.loads(Path(report["artifact_paths"]["report_path"]).read_text(encoding="utf-8"))
    timings = {
        item["stage"]: item
        for item in persisted["execution_observability"]["stage_timings"]
    }
    assert "candidate_evaluation.candidate_payload_aggregation" in timings
    artifact_write = timings["candidate_evaluation.candidate_result_artifact_write"]
    assert artifact_write["candidate_result_file_count"] > 0
    assert artifact_write["candidate_result_total_bytes"] > 0
