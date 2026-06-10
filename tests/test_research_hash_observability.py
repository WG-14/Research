from __future__ import annotations

import json

from bithumb_bot.research.report_writer import write_research_report
from tests.test_research_backtest_reproducibility import _research_manager, _summary_report_payload


def _write_report(tmp_path, monkeypatch, *, experiment_id: str):
    manager = _research_manager(tmp_path, monkeypatch)
    return write_research_report(
        manager=manager,
        experiment_id=experiment_id,
        report_name="backtest",
        payload=_summary_report_payload(experiment_id=experiment_id),
    )


def test_report_write_records_hash_call_count(tmp_path, monkeypatch) -> None:
    result = _write_report(tmp_path, monkeypatch, experiment_id="hash_call_count")

    persisted = json.loads(result.paths.report_path.read_text(encoding="utf-8"))
    report_write = persisted["artifact_observability"]["report_write"]
    assert report_write["hash_call_count"] > 0
    assert report_write["observed_hash_call_count"] == report_write["hash_call_count"]


def test_report_write_records_observed_hash_payload_bytes(tmp_path, monkeypatch) -> None:
    result = _write_report(tmp_path, monkeypatch, experiment_id="hash_payload_bytes")

    persisted = json.loads(result.paths.report_path.read_text(encoding="utf-8"))
    report_write = persisted["artifact_observability"]["report_write"]
    assert report_write["observed_hash_payload_bytes"] > 0
    assert report_write["observed_hash_payload_bytes"] >= report_write["largest_hash_payload_bytes"]


def test_largest_hash_payload_label_is_recorded(tmp_path, monkeypatch) -> None:
    result = _write_report(tmp_path, monkeypatch, experiment_id="hash_payload_label")

    persisted = json.loads(result.paths.report_path.read_text(encoding="utf-8"))
    report_write = persisted["artifact_observability"]["report_write"]
    assert report_write["largest_hash_payload_bytes"] > 0
    assert report_write["largest_hash_label"]
