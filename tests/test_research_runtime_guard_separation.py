from __future__ import annotations

import json
from pathlib import Path

from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.executor import ResearchWorkResult
from bithumb_bot.research.validation_protocol import EvaluationContext, run_research_backtest
from tests.factories.research_reports import minimal_candidate_base_result
from tests.test_research_backtest_reproducibility import _create_db, _manifest, _research_manager


class RuntimeLimitEvaluator:
    def evaluate(self, work_unit, context: EvaluationContext) -> ResearchWorkResult:
        base = minimal_candidate_base_result(
            index=context.candidate_index,
            candidate_id=work_unit.candidate_id,
            parameter_values=context.params,
            include_final_holdout="final_holdout" in context.snapshots,
            include_walk_forward=context.include_walk_forward,
        )
        for key in (
            "train_closed_trades",
            "validation_closed_trades",
            "final_holdout_closed_trades",
        ):
            base[key] = []
        base["candidate_failed"] = True
        base["failure_reason"] = "candidate_resource_limit_exceeded"
        evidence = {
            "stage": "heartbeat",
            "split": "train",
            "reasons": ["max_runtime_exceeded"],
            "candles_processed": 10,
            "elapsed_s": 99.0,
        }
        base["resource_guard"] = dict(evidence)
        return ResearchWorkResult(
            work_unit=work_unit,
            work_unit_hash=work_unit.work_unit_hash,
            candidate_index=context.candidate_index,
            candidate_id=work_unit.candidate_id,
            scenario_index=context.scenario_index,
            scenario_id=context.scenario_id,
            status="failed",
            base_result=base,
            failure_reason="candidate_resource_limit_exceeded",
            failure_evidence=evidence,
            observability={
                "work_unit": work_unit.as_dict(),
                "status": "failed",
                "failure_reason": "candidate_resource_limit_exceeded",
                "resource_guard": dict(evidence),
                "resource_limit_reasons": ["max_runtime_exceeded"],
            },
        )


def _run_runtime_limit_report(tmp_path, monkeypatch) -> dict[str, object]:
    db_path = tmp_path / "runtime_limit.sqlite"
    _create_db(db_path)
    payload = _manifest()
    payload["experiment_id"] = "runtime_guard_separation"
    payload["research_run"] = {"report_detail": "summary", "execution": {"mode": "serial"}}
    return run_research_backtest(
        manifest=parse_manifest(payload),
        db_path=db_path,
        manager=_research_manager(tmp_path, monkeypatch),
        generated_at="2026-05-03T00:00:00+00:00",
        candidate_evaluator=RuntimeLimitEvaluator(),
    )


def test_work_unit_runtime_guard_is_recorded_separately_from_candidate_profile_hash(tmp_path, monkeypatch) -> None:
    report = _run_runtime_limit_report(tmp_path, monkeypatch)
    persisted = json.loads(Path(report["artifact_paths"]["report_path"]).read_text(encoding="utf-8"))

    work_unit = persisted["execution_observability"]["work_units"][0]
    stage_names = {item["stage"] for item in persisted["execution_observability"]["stage_timings"]}

    assert work_unit["failure_reason"] == "candidate_resource_limit_exceeded"
    assert work_unit["resource_limit_reasons"] == ["max_runtime_exceeded"]
    assert work_unit["resource_guard"]["stage"] == "heartbeat"
    assert work_unit["resource_guard"]["split"] == "train"
    assert "candidate_profile_hash.profile_build" in stage_names
    assert "candidate_profile_hash.profile_hash" in stage_names


def test_candidate_profile_hash_stage_does_not_modify_work_unit_failure_reason(tmp_path, monkeypatch) -> None:
    report = _run_runtime_limit_report(tmp_path, monkeypatch)
    persisted = json.loads(Path(report["artifact_paths"]["report_path"]).read_text(encoding="utf-8"))

    profile_stages = [
        item for item in persisted["execution_observability"]["stage_timings"]
        if str(item["stage"]).startswith("candidate_profile_hash")
    ]

    assert profile_stages
    assert persisted["execution_observability"]["work_units"][0]["failure_reason"] == (
        "candidate_resource_limit_exceeded"
    )
    assert all(item.get("failure_reason") != "candidate_resource_limit_exceeded" for item in profile_stages)
