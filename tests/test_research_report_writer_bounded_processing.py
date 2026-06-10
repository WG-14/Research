from __future__ import annotations

import json

from bithumb_bot.research.report_writer import (
    scenario_evidence_hash_inputs,
    summarize_derived_candidate,
    write_research_report,
)
from tests.test_research_backtest_reproducibility import _research_manager, _summary_report_payload


class _UnhashableIfTraversed:
    pass


def _contains_key(payload: object, key: str) -> bool:
    if isinstance(payload, dict):
        return key in payload or any(_contains_key(value, key) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_key(value, key) for value in payload)
    return False


def _payload_with_large_stage_trace(*, experiment_id: str) -> dict[str, object]:
    payload = _summary_report_payload(experiment_id=experiment_id)
    candidate = payload["candidates"][0]  # type: ignore[index]
    scenario = {
        "scenario_id": "scenario_001",
        "behavior_hash": "sha256:scenario-behavior",
        "equity_curve_hash": "sha256:scenario-equity",
        "train_resource_usage": {
            "behavior_hash": "sha256:train-behavior",
            "stage_trace_hash": "sha256:train-stage-trace",
            "stage_trace": [_UnhashableIfTraversed()],
        },
        "validation_resource_usage": {
            "behavior_hash": "sha256:validation-behavior",
            "stage_trace_hash": "sha256:validation-stage-trace",
            "stage_trace": [{"stage": "validation", "i": i} for i in range(5_000)],
        },
        "retained_detail_summary": {"retained_equity_point_count": 0},
    }
    candidate["scenario_results"] = [scenario]
    return payload


def test_summary_report_does_not_hash_full_candidate_payload(tmp_path, monkeypatch) -> None:
    manager = _research_manager(tmp_path, monkeypatch)

    result = write_research_report(
        manager=manager,
        experiment_id="bounded_candidate_hash",
        report_name="backtest",
        payload=_payload_with_large_stage_trace(experiment_id="bounded_candidate_hash"),
    )

    persisted = json.loads(result.paths.report_path.read_text(encoding="utf-8"))
    derived = json.loads(result.paths.derived_path.read_text(encoding="utf-8"))
    assert not _contains_key(persisted, "stage_trace")
    assert not _contains_key(derived, "stage_trace")
    assert persisted["artifact_observability"]["report_write"]["observed_hash_payload_bytes"] < 80_000


def test_summary_derived_candidate_reuses_stage_trace_hash() -> None:
    candidate = _payload_with_large_stage_trace(experiment_id="derived_stage_trace")["candidates"][0]

    summary = summarize_derived_candidate(candidate, "summary")

    usage = summary["scenario_results"][0]["validation_resource_usage"]
    assert usage["stage_trace_count"] == 5_000
    assert usage["stage_trace_hash"] == "sha256:validation-stage-trace"
    assert "stage_trace" not in usage


def test_summary_scenario_hash_uses_evidence_hash_tree() -> None:
    scenario = _payload_with_large_stage_trace(experiment_id="scenario_tree")["candidates"][0]["scenario_results"][0]

    evidence = scenario_evidence_hash_inputs(scenario)

    assert evidence["validation_resource_usage_hashes"]["stage_trace_count"] == 5_000
    assert evidence["validation_resource_usage_hashes"]["stage_trace_hash"] == "sha256:validation-stage-trace"
    assert not _contains_key(evidence, "stage_trace")
