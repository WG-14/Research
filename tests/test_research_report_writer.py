from __future__ import annotations

import json
from pathlib import Path

from bithumb_bot.research.report_writer import (
    ResearchReportPaths,
    build_report_artifacts,
    compute_artifact_write_summary,
    compute_report_hashes,
    persist_final_research_report_observability,
    sync_final_report_observability,
    summarize_candidate_result,
    summarize_derived_candidate,
    summarize_report_candidate,
    write_report_artifacts,
    write_research_report,
)
from tests.test_research_backtest_reproducibility import _research_manager, _summary_report_payload


def _paths(tmp_path: Path) -> ResearchReportPaths:
    return ResearchReportPaths(
        derived_path=tmp_path / "derived_candidates.json",
        report_path=tmp_path / "backtest_report.json",
        candidate_events_path=tmp_path / "candidate_events.jsonl",
        candidate_results_dir=tmp_path / "candidate_results",
        candidate_failures_dir=tmp_path / "candidate_failures",
        trace_manifest_path=tmp_path / "trace_manifest.json",
    )


def _artifact_summary() -> dict[str, object]:
    return {
        "schema_version": 1,
        "derived_candidates_path": "/tmp/derived_candidates.json",
        "derived_candidates_ref": "derived/research/test/derived_candidates.json",
        "derived_candidates_hash": "sha256:" + "0" * 64,
        "derived_candidates_bytes": 17,
        "report_path": "/tmp/backtest_report.json",
        "report_ref": "reports/research/test/backtest_report.json",
        "report_bytes": 0,
        "artifact_file_count": 2,
        "artifact_total_bytes": 17,
        "write_wall_seconds": 0.25,
    }


def test_report_write_stage_timing_payload_matches_artifact_summary(tmp_path: Path) -> None:
    payload = {
        "experiment_id": "contract",
        "candidates": [],
        "execution_observability": {
            "stage_timings": [
                {"stage": "load_split", "wall_seconds": 0.1},
                {"stage": "report_write", "wall_seconds": 0.2},
            ]
        },
    }

    _, summary = persist_final_research_report_observability(
        paths=_paths(tmp_path),
        report_payload=payload,
        artifact_write_summary=_artifact_summary(),
        artifact_total_bytes_base=17,
    )

    report_write = [
        item for item in payload["execution_observability"]["stage_timings"] if item["stage"] == "report_write"
    ][0]
    assert report_write["artifact_total_bytes"] == summary["artifact_total_bytes"]
    assert report_write["artifact_file_count"] == summary["artifact_file_count"]
    assert report_write["derived_candidates_bytes"] == summary["derived_candidates_bytes"]
    assert report_write["report_bytes"] == summary["report_bytes"]


def test_persist_final_research_report_observability_updates_persisted_payload(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    payload = {
        "experiment_id": "contract",
        "candidates": [],
        "execution_observability": {
            "stage_timings": [
                {"stage": "report_write", "wall_seconds": 0.2},
            ]
        },
    }

    content_hash, summary = persist_final_research_report_observability(
        paths=paths,
        report_payload=payload,
        artifact_write_summary=_artifact_summary(),
        artifact_total_bytes_base=17,
    )

    persisted = json.loads(paths.report_path.read_text(encoding="utf-8"))
    assert persisted["content_hash"] == content_hash
    assert persisted["artifact_write_summary"] == summary
    assert persisted["artifact_observability"]["report_write"] == summary


def test_report_write_observability_records_substage_timings(tmp_path: Path, monkeypatch) -> None:
    manager = _research_manager(tmp_path, monkeypatch)

    result = write_research_report(
        manager=manager,
        experiment_id="report_write_substages",
        report_name="backtest",
        payload=_summary_report_payload(experiment_id="report_write_substages"),
    )

    persisted = json.loads(result.paths.report_path.read_text(encoding="utf-8"))
    report_write = persisted["artifact_observability"]["report_write"]
    substages = {item["stage"]: item for item in report_write["substage_timings"]}
    for stage in {
        "reference_first_payload",
        "report_candidate_summary",
        "derived_candidate_summary",
        "report_hashing",
        "report_byte_count",
        "write_derived",
        "write_report",
        "persist_final_observability",
        "final_report_rewrite",
    }:
        assert stage in substages
        assert substages[stage]["wall_seconds"] >= 0
    assert report_write["file_write_wall_seconds"] >= 0
    stage_names = {item["stage"] for item in persisted["execution_observability"]["stage_timings"]}
    assert "report_write.write_derived" in stage_names
    assert "report_write.write_report" in stage_names
    assert "report_write.persist_final_observability" in stage_names


def test_report_writer_exposes_build_hash_write_sync_steps() -> None:
    assert callable(build_report_artifacts)
    assert callable(compute_report_hashes)
    assert callable(compute_artifact_write_summary)
    assert callable(write_report_artifacts)
    assert callable(sync_final_report_observability)


def test_final_observability_sync_does_not_require_validation_protocol_rewrite(tmp_path: Path, monkeypatch) -> None:
    manager = _research_manager(tmp_path, monkeypatch)

    result = write_research_report(
        manager=manager,
        experiment_id="writer_final_sync",
        report_name="backtest",
        payload=_summary_report_payload(experiment_id="writer_final_sync"),
    )

    persisted = json.loads(result.paths.report_path.read_text(encoding="utf-8"))
    assert persisted["artifact_write_summary"] == result.artifact_write_summary
    assert persisted["content_hash"] == result.content_hash
    assert persisted["artifact_write_summary"]["report_bytes"] == result.paths.report_path.stat().st_size


def test_summary_report_uses_candidate_summary() -> None:
    candidate = {
        "candidate_id": "candidate_001",
        "acceptance_gate_result": "PASS",
        "validation_metrics_v2": {"total_return_pct": 1.0},
        "decisions": [{"ts": 1}],
        "equity_curve": [{"ts": 1, "equity": 1.0}],
    }

    summary = summarize_report_candidate(candidate)

    assert summary["candidate_id"] == "candidate_001"
    assert summary["acceptance_gate_result"] == "PASS"
    assert summary["validation_metrics_v2"] == {"total_return_pct": 1.0}
    assert summary["candidate_payload_hash"].startswith("sha256:")
    assert "decisions" not in summary
    assert "equity_curve" not in summary


def test_summary_derived_candidates_are_bounded() -> None:
    candidate = {
        "parameter_candidate_id": "candidate_001",
        "candidate_profile_hash": "sha256:profile",
        "scenario_results": [
            {
                "scenario_id": "scenario_001",
                "validation_equity_curve": [{"ts": 1, "equity": 1.0}],
                "train_equity_curve": [{"ts": 1, "equity": 1.0}],
                "final_holdout_equity_curve": [{"ts": 1, "equity": 1.0}],
                "equity_curve_hash": "sha256:equity",
                "retained_detail_summary": {"retained_equity_point_count": 0},
            }
        ],
        "decisions": [{"ts": 1}],
    }

    summary = summarize_derived_candidate(candidate, "summary")

    assert summary["derived_detail_policy"] == "summary_bounded"
    assert summary["candidate_result_detail_policy"] == "summary_bounded"
    assert summary["candidate_profile_hash"] == "sha256:profile"
    assert "decisions" not in summary
    scenario = summary["scenario_results"][0]
    assert scenario["train_equity_curve"] == []
    assert scenario["validation_equity_curve"] == []
    assert scenario["final_holdout_equity_curve"] == []
    assert scenario["equity_curve_hash"] == "sha256:equity"
    assert scenario["retained_detail_summary"] == {"retained_equity_point_count": 0}


def test_candidate_result_summary_is_reference_first_bounded() -> None:
    candidate = {
        "parameter_candidate_id": "candidate_001",
        "candidate_profile_hash": "sha256:profile",
        "behavior_hash": "sha256:behavior",
        "scenario_results": [
            {
                "scenario_id": "scenario_001",
                "validation_equity_curve": [{"ts": 1, "equity": 1.0}],
                "validation_execution_metadata": [{"ts": 1, "fill": "large"}],
                "behavior_hash": "sha256:scenario-behavior",
                "equity_curve_hash": "sha256:equity",
                "retained_detail_summary": {"retained_equity_point_count": 0},
            }
        ],
    }

    summary = summarize_candidate_result(candidate, "summary")

    assert summary["candidate_result_detail_policy"] == "summary_bounded"
    assert summary["candidate_profile_hash"] == "sha256:profile"
    assert summary["behavior_hash"] == "sha256:behavior"
    scenario = summary["scenario_results"][0]
    assert scenario["validation_equity_curve"] == []
    assert "validation_execution_metadata" not in scenario
    assert scenario["behavior_hash"] == "sha256:scenario-behavior"
    assert scenario["equity_curve_hash"] == "sha256:equity"


def test_candidate_result_summary_omits_resource_usage_stage_trace() -> None:
    candidate = {
        "parameter_candidate_id": "candidate_001",
        "candidate_profile_hash": "sha256:profile",
        "retained_detail_summary": {"report_detail": "summary"},
        "scenario_results": [
            {
                "scenario_id": "scenario_001",
                "behavior_hash": "sha256:scenario-behavior",
                "equity_curve_hash": "sha256:equity",
                "retained_detail_summary": {"retained_equity_point_count": 0},
                "train_resource_usage": {
                    "behavior_hash": "sha256:train-behavior",
                    "equity_curve_hash": "sha256:train-equity",
                    "stage_trace": [{"stage": "train", "bar_index": 1}],
                    "stage_trace_hash": "sha256:train-stage-trace",
                    "decision_count": 1,
                    "memory_summary": {"peak_rss_mb": 128.0},
                },
                "validation_resource_usage": {
                    "behavior_hash": "sha256:validation-behavior",
                    "equity_curve_hash": "sha256:validation-equity",
                    "stage_trace": [{"stage": "validation", "bar_index": 1}],
                    "stage_trace_hash": "sha256:validation-stage-trace",
                    "trade_count": 1,
                },
                "final_holdout_resource_usage": {
                    "behavior_hash": "sha256:holdout-behavior",
                    "equity_curve_hash": "sha256:holdout-equity",
                    "stage_trace": [{"stage": "final_holdout", "bar_index": 1}],
                    "stage_trace_hash": "sha256:holdout-stage-trace",
                },
            }
        ],
    }

    summary = summarize_candidate_result(candidate, "summary")

    assert summary["candidate_profile_hash"] == "sha256:profile"
    assert summary["retained_detail_summary"] == {"report_detail": "summary"}
    scenario = summary["scenario_results"][0]
    assert scenario["retained_detail_summary"] == {"retained_equity_point_count": 0}
    for key, expected_hash in (
        ("train_resource_usage", "sha256:train-stage-trace"),
        ("validation_resource_usage", "sha256:validation-stage-trace"),
        ("final_holdout_resource_usage", "sha256:holdout-stage-trace"),
    ):
        usage = scenario[key]
        assert "stage_trace" not in usage
        assert usage["stage_trace_count"] == 1
        assert usage["stage_trace_hash"] == expected_hash
        assert usage["behavior_hash"].startswith("sha256:")
        assert usage["equity_curve_hash"].startswith("sha256:")
    assert scenario["train_resource_usage"]["decision_count"] == 1
    assert scenario["train_resource_usage"]["memory_summary"] == {"peak_rss_mb": 128.0}
    assert scenario["validation_resource_usage"]["trade_count"] == 1


def test_summary_derived_candidate_resource_usage_is_bounded() -> None:
    candidate = {
        "parameter_candidate_id": "candidate_001",
        "candidate_profile_hash": "sha256:profile",
        "candidate_behavior_profile_hash": "sha256:behavior-profile",
        "retained_detail_summary": {"report_detail": "summary"},
        "scenario_results": [
            {
                "scenario_id": "scenario_001",
                "train_resource_usage": {
                    "behavior_hash": "sha256:train-behavior",
                    "equity_curve_hash": "sha256:train-equity",
                    "nested": {"stage_trace": [{"stage": "nested"}]},
                    "stage_trace": [{"stage": "train"}],
                    "stage_trace_hash": "sha256:stage-trace",
                },
                "validation_execution_metadata": [{"ts": 1, "fill": "large"}],
                "validation_equity_curve": [{"ts": 1, "equity": 1.0}],
                "equity_curve_hash": "sha256:equity",
                "retained_detail_summary": {"retained_equity_point_count": 0},
            }
        ],
    }

    summary = summarize_derived_candidate(candidate, "summary")

    assert summary["derived_detail_policy"] == "summary_bounded"
    assert summary["candidate_profile_hash"] == "sha256:profile"
    assert summary["candidate_behavior_profile_hash"] == "sha256:behavior-profile"
    assert summary["retained_detail_summary"] == {"report_detail": "summary"}
    scenario = summary["scenario_results"][0]
    usage = scenario["train_resource_usage"]
    assert "stage_trace" not in usage
    assert "stage_trace" not in usage["nested"]
    assert usage["stage_trace_hash"] == "sha256:stage-trace"
    assert usage["nested"]["stage_trace_count"] == 1
    assert scenario["validation_equity_curve"] == []
    assert "validation_execution_metadata" not in scenario
    assert scenario["retained_detail_summary"] == {"retained_equity_point_count": 0}
