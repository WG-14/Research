from __future__ import annotations

import json

from bithumb_bot.research.report_writer import write_research_report
from tests.test_research_backtest_reproducibility import _research_manager, _summary_report_payload


def _contains_key(payload: object, key: str) -> bool:
    if isinstance(payload, dict):
        return key in payload or any(_contains_key(value, key) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_key(value, key) for value in payload)
    return False


def _large_summary_payload(*, experiment_id: str) -> dict[str, object]:
    payload = _summary_report_payload(experiment_id=experiment_id)
    candidate = payload["candidates"][0]  # type: ignore[index]
    large_trace = [{"stage": "validation", "bar_index": index, "value": index % 7} for index in range(20_000)]
    candidate["scenario_results"] = [
        {
            "scenario_id": "scenario_001",
            "behavior_hash": "sha256:scenario-behavior",
            "equity_curve_hash": "sha256:scenario-equity",
            "train_resource_usage": {
                "behavior_hash": "sha256:train-behavior",
                "stage_trace_hash": "sha256:train-stage-trace",
                "stage_trace": large_trace,
            },
            "validation_resource_usage": {
                "behavior_hash": "sha256:validation-behavior",
                "stage_trace_hash": "sha256:validation-stage-trace",
                "stage_trace": large_trace,
            },
            "retained_detail_summary": {"retained_equity_point_count": 0},
        }
    ]
    return payload


def test_summary_report_finalization_does_not_hash_large_stage_trace(tmp_path, monkeypatch) -> None:
    manager = _research_manager(tmp_path, monkeypatch)

    result = write_research_report(
        manager=manager,
        experiment_id="large_trace_hash",
        report_name="backtest",
        payload=_large_summary_payload(experiment_id="large_trace_hash"),
    )

    report_write = json.loads(result.paths.report_path.read_text(encoding="utf-8"))["artifact_observability"][
        "report_write"
    ]
    assert report_write["largest_hash_payload_bytes"] < 80_000


def test_summary_report_observed_hash_payload_bytes_stays_bounded(tmp_path, monkeypatch) -> None:
    manager = _research_manager(tmp_path, monkeypatch)

    result = write_research_report(
        manager=manager,
        experiment_id="large_trace_observed",
        report_name="backtest",
        payload=_large_summary_payload(experiment_id="large_trace_observed"),
    )

    persisted = json.loads(result.paths.report_path.read_text(encoding="utf-8"))
    assert persisted["artifact_observability"]["report_write"]["observed_hash_payload_bytes"] < 350_000
    assert not _contains_key(persisted, "stage_trace")
    derived = json.loads(result.paths.derived_path.read_text(encoding="utf-8"))
    assert not _contains_key(derived, "stage_trace")


def test_summary_report_write_substages_are_recorded_for_large_payload(tmp_path, monkeypatch) -> None:
    manager = _research_manager(tmp_path, monkeypatch)

    result = write_research_report(
        manager=manager,
        experiment_id="large_trace_substages",
        report_name="backtest",
        payload=_large_summary_payload(experiment_id="large_trace_substages"),
    )

    persisted = json.loads(result.paths.report_path.read_text(encoding="utf-8"))
    stage_names = {
        item["stage"]
        for item in persisted["artifact_observability"]["report_write"]["substage_timings"]
    }
    assert "report_hashing" in stage_names
    assert "report_byte_count" in stage_names
    assert "persist_final_observability" in stage_names
