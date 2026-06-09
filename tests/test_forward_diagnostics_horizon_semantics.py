from __future__ import annotations

from pathlib import Path

from bithumb_bot.research.forward_diagnostics_report import write_forward_diagnostics_report
from bithumb_bot.research.forward_targets import build_horizon_durations
from tests.test_forward_diagnostics_report import _manager, _manifest, _result


def test_report_records_horizon_steps_and_duration_for_one_minute_interval(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(
        manager=_manager(tmp_path),
        manifest=_manifest(),
        result=_result(horizon_steps=(5,), interval="1m"),
    )

    assert report["horizon_steps"] == [5]
    assert report["horizon_durations"] == [
        {
            "horizon_steps": 5,
            "horizon_label": "5c",
            "interval": "1m",
            "horizon_duration_ms": 300_000,
            "horizon_duration_label": "5m",
        }
    ]


def test_report_records_horizon_steps_and_duration_for_five_minute_interval(tmp_path: Path) -> None:
    report = write_forward_diagnostics_report(
        manager=_manager(tmp_path),
        manifest=_manifest(),
        result=_result(horizon_steps=(5,), interval="5m"),
    )

    assert report["horizon_durations"][0]["horizon_label"] == "5c"
    assert report["horizon_durations"][0]["horizon_duration_ms"] == 1_500_000
    assert report["horizon_durations"][0]["horizon_duration_label"] == "25m"


def test_cli_help_describes_horizons_as_candle_steps() -> None:
    source = Path("src/bithumb_bot/cli/commands/research.py").read_text(encoding="utf-8")

    assert "candle-step integers" in source
    assert "duration strings are not accepted" in source


def test_horizon_label_remains_candle_step_not_wall_clock_duration() -> None:
    durations = build_horizon_durations(interval="5m", horizon_steps=(5,))

    assert durations[0].horizon_label == "5c"
    assert durations[0].horizon_duration_label == "25m"


def test_report_content_hash_changes_when_horizon_duration_changes(tmp_path: Path) -> None:
    first = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "a"),
        manifest=_manifest(),
        result=_result(horizon_steps=(5,), interval="1m"),
    )
    second = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "b"),
        manifest=_manifest(),
        result=_result(horizon_steps=(5,), interval="5m"),
    )

    assert first["content_hash"] != second["content_hash"]
