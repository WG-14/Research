from __future__ import annotations

import json
from pathlib import Path

from bithumb_bot.research import validation_protocol
from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.report_writer import write_research_report
from tests.factories.research_reports import DeterministicResearchEvaluator
from tests.test_research_backtest_reproducibility import _manifest
from tests.test_research_execution_plan import _manager, _quality_report, _snapshot


def test_parent_serial_stage_summary_includes_named_stages(tmp_path: Path, monkeypatch) -> None:
    manifest = parse_manifest(_manifest())
    snapshots = {name: _snapshot(name) for name in ("train", "validation", "final_holdout")}
    quality_reports = {name: _quality_report(name) for name in snapshots}

    result = validation_protocol._evaluate_candidates(
        manifest=manifest,
        manager=_manager(tmp_path, monkeypatch),
        snapshots=snapshots,
        quality_reports=quality_reports,
        include_walk_forward=False,
        execution_calibration=None,
        candidate_evaluator=DeterministicResearchEvaluator(),
    )
    summary = validation_protocol.collect_parent_serial_stage_summary(result.substage_timings)
    stages = {item["stage"] for item in summary["parent_serial_stage_timings"]}

    assert {
        "pre_parallel_run_dataset_fingerprint",
        "pre_parallel_hash_materialization",
        "build_work_tasks",
        "append_candidate_start_events",
    }.issubset(stages)
    assert all("wall_seconds" in item for item in summary["parent_serial_stage_timings"])


def test_parent_serial_bottleneck_reason_names_dominant_stage() -> None:
    payload = validation_protocol._execution_observability_payload(
        manifest=parse_manifest(_manifest()),
        stage_timings=[
            {"stage": "pre_parallel_hash_materialization", "wall_seconds": 3.0},
            {"stage": "build_work_tasks", "wall_seconds": 1.0},
            {"stage": "parallel_worker_execution", "wall_seconds": 1.0},
        ],
        work_unit_observability=[],
        execution_boundary={
            "actual_worker_context_mode": "test",
            "actual_parallel_task_count": 1,
            "parallel_executor_used": True,
            "research_max_workers_requested": 2,
            "research_max_workers_effective": 2,
            "available_parallel_work_tasks": 1,
        },
        snapshots={"train": _snapshot("train")},
    )

    assert payload["parent_serial_bottleneck_reasons"] == [
        "parent_serial_stage_dominates_wall_time:pre_parallel_hash_materialization"
    ]


def test_parent_serial_stage_summary_includes_report_write() -> None:
    payload = validation_protocol._execution_observability_payload(
        manifest=parse_manifest(_manifest()),
        stage_timings=[
            {"stage": "pre_parallel_hash_materialization", "wall_seconds": 0.1},
            {"stage": "parallel_worker_execution", "wall_seconds": 0.2},
            {"stage": "report_write", "wall_seconds": 0.3},
        ],
        work_unit_observability=[],
        execution_boundary={
            "actual_worker_context_mode": "test",
            "actual_parallel_task_count": 1,
            "parallel_executor_used": True,
            "research_max_workers_requested": 2,
            "research_max_workers_effective": 2,
            "available_parallel_work_tasks": 1,
        },
        snapshots={"train": _snapshot("train")},
    )

    stages = {item["stage"] for item in payload["parent_serial_stage_timings"]}
    assert "report_write" in stages
    assert payload["parent_serial_wall_seconds"] == 0.4
    assert payload["parent_serial_bottleneck_reasons"] == [
        "parent_serial_stage_dominates_wall_time:report_write"
    ]


def test_persisted_report_parent_serial_includes_report_write(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    report = {
        "experiment_id": "parent_serial_report_write",
        "research_run": {"report_detail": "summary"},
        "candidates": [],
        "execution_observability": {
            "stage_timings": [
                {"stage": "parallel_worker_execution", "wall_seconds": 0.001},
            ],
            "parallel_worker_execution_wall_seconds": 0.001,
            "parent_serial_stage_timings": [],
            "parent_serial_wall_seconds": 0.0,
            "parent_serial_bottleneck_reasons": [],
            "worker_observation_warning_reasons": [],
        },
    }

    result = write_research_report(
        manager=manager,
        experiment_id="parent_serial_report_write",
        report_name="backtest",
        payload=report,
    )
    persisted = json.loads(result.paths.report_path.read_text(encoding="utf-8"))
    observed = persisted["execution_observability"]
    report_write = persisted["artifact_observability"]["report_write"]

    stages = {item["stage"] for item in observed["parent_serial_stage_timings"]}
    assert "report_write" in stages
    assert observed["parent_serial_wall_seconds"] >= round(report_write["write_wall_seconds"], 6)
    assert observed["parent_serial_bottleneck_reasons"]
