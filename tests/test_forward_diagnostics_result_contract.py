from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.forward_diagnostics import (
    ForwardDiagnosticsResult,
    run_forward_diagnostics,
    run_forward_diagnostics_on_snapshot,
)
import bithumb_bot.research.forward_diagnostics as forward_diagnostics
from bithumb_bot.research.forward_diagnostics_report import write_forward_diagnostics_report
from tests.test_forward_diagnostics_report import _manager, _manifest


def _snapshot() -> DatasetSnapshot:
    candles = tuple(
        Candle(ts=index, open=100 + index, high=102 + index, low=99 + index, close=101 + index, volume=10 + index)
        for index in range(30)
    )
    return DatasetSnapshot(
        snapshot_id="snapshot",
        source="test",
        market="BTC_KRW",
        interval="1m",
        split_name="train",
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=candles,
    )


def test_run_forward_diagnostics_returns_typed_result(monkeypatch) -> None:
    monkeypatch.setattr(forward_diagnostics, "load_dataset_split", lambda **kwargs: _snapshot())
    monkeypatch.setattr(
        forward_diagnostics,
        "build_dataset_quality_report",
        lambda **kwargs: type(
            "QualityReport",
            (),
            {
                "quality_gate_status": "PASS",
                "quality_gate_reasons": (),
                "content_hash": "sha256:" + "4" * 64,
                "payload": {"dataset_content_hash": _snapshot().content_hash()},
            },
        )(),
    )

    result = run_forward_diagnostics(
        manifest=SimpleNamespace(experiment_id="exp1"),
        db_path="/tmp/test.sqlite",
        split_name="train",
        feature_names=("rolling_return",),
        horizon_steps=(1,),
        bucket_method="quantile:2",
        min_bucket_count=1,
    )

    assert isinstance(result, ForwardDiagnosticsResult)


def test_result_as_dict_is_serialization_boundary_only() -> None:
    result = run_forward_diagnostics_on_snapshot(
        snapshot=_snapshot(),
        feature_names=("rolling_return",),
        horizon_steps=(1,),
        bucket_method="quantile:2",
        min_bucket_count=1,
    )
    payload = result.as_dict()

    assert isinstance(payload, dict)
    assert payload["dataset"]["content_hash"] == result.dataset.content_hash


def test_forward_diagnostics_cli_does_not_rehydrate_result_from_dict() -> None:
    source = Path("src/bithumb_bot/research/forward_diagnostics_cli.py").read_text(encoding="utf-8")

    assert "_metric_from_payload" not in source
    assert "ForwardDiagnosticsResult(" not in source
    assert "result_payload" not in source
    assert '["feature_bucket_metrics"]' not in source


def test_forward_diagnostics_report_writer_accepts_only_typed_result(tmp_path) -> None:
    result = run_forward_diagnostics_on_snapshot(
        snapshot=_snapshot(),
        feature_names=("rolling_return",),
        horizon_steps=(1,),
        bucket_method="quantile:2",
        min_bucket_count=1,
    )

    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=result)
    assert report["artifact_type"] == "forward_return_diagnostic_report"

    try:
        write_forward_diagnostics_report(manager=_manager(tmp_path / "bad"), manifest=_manifest(), result=result.as_dict())  # type: ignore[arg-type]
    except TypeError as exc:
        assert "ForwardDiagnosticsResult" in str(exc)
    else:
        raise AssertionError("dict result payload must not be accepted by report writer")
