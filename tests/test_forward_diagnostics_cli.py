from __future__ import annotations

import pytest

from bithumb_bot.cli.parser import build_parser
from bithumb_bot.cli.registry import command_registry


def _parser():
    return build_parser(command_registry())


def test_research_forward_diagnostics_help_exposes_required_options(capsys: pytest.CaptureFixture[str]) -> None:
    parser = _parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["research-forward-diagnostics", "--help"])

    assert exc.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    for option in (
        "--manifest",
        "--split",
        "--features",
        "--horizons",
        "--bucket",
        "--entry-price",
        "--min-bucket-count",
        "--allow-final-holdout-diagnostics",
        "--out",
        "--json",
    ):
        assert option in output


def test_research_forward_diagnostics_help_describes_signal_close_limit(capsys: pytest.CaptureFixture[str]) -> None:
    parser = _parser()

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["research-forward-diagnostics", "--help"])

    assert exc.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "signal_close is diagnostic convenience only" in output
    assert "OHLC MFE/MAE uses the next candle path" in output
    assert "intrabar" in output


def test_research_forward_diagnostics_defaults_to_train_split() -> None:
    args = _parser().parse_args(
        [
            "research-forward-diagnostics",
            "--manifest",
            "manifest.json",
            "--features",
            "sma_gap",
            "--horizons",
            "1",
            "--bucket",
            "quantile:10",
        ]
    )

    assert args.split == "train"


def test_research_forward_diagnostics_defaults_to_next_open_entry_price() -> None:
    args = _parser().parse_args(
        [
            "research-forward-diagnostics",
            "--manifest",
            "manifest.json",
            "--features",
            "sma_gap",
            "--horizons",
            "1",
            "--bucket",
            "quantile:10",
        ]
    )

    assert args.entry_price == "next_open"


def test_research_forward_diagnostics_rejects_unknown_split() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "research-forward-diagnostics",
                "--manifest",
                "manifest.json",
                "--split",
                "unknown",
                "--features",
                "sma_gap",
                "--horizons",
                "1",
                "--bucket",
                "quantile:10",
            ]
        )


def test_research_forward_diagnostics_rejects_empty_feature_list() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "research-forward-diagnostics",
                "--manifest",
                "manifest.json",
                "--features",
                "",
                "--horizons",
                "1",
                "--bucket",
                "quantile:10",
            ]
        )


def test_research_forward_diagnostics_rejects_empty_horizon_list() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "research-forward-diagnostics",
                "--manifest",
                "manifest.json",
                "--features",
                "sma_gap",
                "--horizons",
                "",
                "--bucket",
                "quantile:10",
            ]
        )


def test_research_forward_diagnostics_requires_manifest() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "research-forward-diagnostics",
                "--features",
                "sma_gap",
                "--horizons",
                "1",
                "--bucket",
                "quantile:10",
            ]
        )


def test_research_forward_diagnostics_is_registered() -> None:
    assert "research-forward-diagnostics" in command_registry()


def test_research_forward_diagnostics_rejects_final_holdout_without_override() -> None:
    from bithumb_bot.research.forward_diagnostics_cli import cmd_research_forward_diagnostics

    code = cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="final_holdout",
        features=("sma_gap",),
        horizons=(1,),
        bucket="quantile:10",
    )

    assert code == 1


def test_research_forward_diagnostics_accepts_final_holdout_with_explicit_override(monkeypatch) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli

    calls: dict[str, object] = {}

    def fake_load_manifest(path):
        return type("Manifest", (), {"experiment_id": "exp1", "manifest_hash": lambda self: "sha256:" + "1" * 64})()

    def fake_run_forward_diagnostics(**kwargs):
        calls.update(kwargs)
        return object()

    def fake_write_forward_diagnostics_report(*, manager, manifest, result):
        calls["reported_result"] = result
        return {"artifact_paths": {"report": "/tmp/report.json"}}

    monkeypatch.setattr(cli, "load_manifest", fake_load_manifest)
    monkeypatch.setattr(cli, "run_forward_diagnostics", fake_run_forward_diagnostics)
    monkeypatch.setattr(cli, "write_forward_diagnostics_report", fake_write_forward_diagnostics_report)

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="final_holdout",
        features=("sma_gap",),
        horizons=(1,),
        bucket="quantile:10",
        allow_final_holdout_diagnostics=True,
    )

    assert code == 0
    assert calls["split_name"] == "final_holdout"
    assert calls["final_holdout_diagnostic_override"] is True
    assert calls["reported_result"] is not None


def test_forward_diagnostics_cli_does_not_rehydrate_result_from_dict() -> None:
    from pathlib import Path

    source = Path("src/bithumb_bot/research/forward_diagnostics_cli.py").read_text(encoding="utf-8")

    assert "_metric_from_payload" not in source
    assert 'result_payload["feature_bucket_metrics"]' not in source
