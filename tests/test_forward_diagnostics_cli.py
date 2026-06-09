from __future__ import annotations

import json

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


def test_cli_final_holdout_policy_denial_returns_code_1(monkeypatch, tmp_path) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli
    from tests.test_forward_diagnostics_report import _manager, _manifest

    monkeypatch.setattr(cli, "PATH_MANAGER", _manager(tmp_path))
    monkeypatch.setattr(cli, "load_manifest", lambda path: _manifest())

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="final_holdout",
        features=("sma_gap",),
        horizons=(1,),
        bucket="quantile:10",
        allow_final_holdout_diagnostics=False,
    )

    assert code == 1


def test_cli_json_final_holdout_without_override_outputs_policy_denial_json(monkeypatch, capsys, tmp_path) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli
    from tests.test_forward_diagnostics_report import _manager, _manifest

    manager = _manager(tmp_path)
    monkeypatch.setattr(cli, "PATH_MANAGER", manager)
    monkeypatch.setattr(cli, "load_manifest", lambda path: _manifest())

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="final_holdout",
        features=("sma_gap",),
        horizons=(1,),
        bucket="quantile:10",
        as_json=True,
        allow_final_holdout_diagnostics=False,
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 1
    assert payload["artifact_type"] == "forward_return_diagnostic_policy_denial"
    assert payload["diagnostic_status"] == "policy_denied"
    assert payload["reason"] == "final_holdout_diagnostic_override_required"
    assert payload["split_name"] == "final_holdout"
    assert payload["diagnostic_only"] is True
    assert payload["promotion_evidence"] is False
    assert payload["artifact_paths"]["policy_denial"] == str(
        manager.data_dir() / "reports/research/exp1/forward_diagnostics_policy_denial.json"
    )
    assert (manager.data_dir() / "reports/research/exp1/forward_diagnostics_policy_denial.json").exists()


def test_cli_json_final_holdout_policy_denial_has_no_human_prefix(monkeypatch, capsys, tmp_path) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli
    from tests.test_forward_diagnostics_report import _manager, _manifest

    monkeypatch.setattr(cli, "PATH_MANAGER", _manager(tmp_path))
    monkeypatch.setattr(cli, "load_manifest", lambda path: _manifest())

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="final_holdout",
        features=("sma_gap",),
        horizons=(1,),
        bucket="quantile:10",
        as_json=True,
        allow_final_holdout_diagnostics=False,
    )
    output = capsys.readouterr().out

    assert code == 1
    assert not output.startswith("[RESEARCH-FORWARD-DIAGNOSTICS]")
    json.loads(output)


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


def test_cli_json_success_outputs_parseable_json(monkeypatch, capsys) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli

    monkeypatch.setattr(cli, "load_manifest", lambda path: type("Manifest", (), {"experiment_id": "exp1", "manifest_hash": lambda self: "sha256:" + "1" * 64})())
    monkeypatch.setattr(cli, "run_forward_diagnostics", lambda **kwargs: object())
    monkeypatch.setattr(
        cli,
        "write_forward_diagnostics_report",
        lambda **kwargs: {
            "artifact_type": "forward_return_diagnostic_report",
            "diagnostic_status": "available",
            "artifact_paths": {"report": "/tmp/report.json"},
        },
    )

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="train",
        features=("range_ratio",),
        horizons=(1,),
        bucket="quantile:1",
        as_json=True,
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["artifact_type"] == "forward_return_diagnostic_report"
    assert payload["diagnostic_status"] == "available"


def test_cli_json_failure_outputs_parseable_json(monkeypatch, capsys, tmp_path) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli
    from bithumb_bot.research.forward_diagnostics import ForwardDiagnosticsUnavailableError
    from tests.test_forward_diagnostics_report import _manager, _manifest

    monkeypatch.setattr(cli, "PATH_MANAGER", _manager(tmp_path))
    monkeypatch.setattr(cli, "load_manifest", lambda path: _manifest())
    monkeypatch.setattr(
        cli,
        "run_forward_diagnostics",
        lambda **kwargs: (_ for _ in ()).throw(ForwardDiagnosticsUnavailableError(("no_forward_targets",))),
    )

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="train",
        features=("range_ratio",),
        horizons=(1,),
        bucket="quantile:1",
        as_json=True,
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 1
    assert payload["artifact_type"] == "forward_return_diagnostic_failure"
    assert payload["diagnostic_status"] == "unavailable"
    assert payload["fail_reasons"] == ["no_forward_targets"]


def test_cli_json_failure_does_not_prefix_human_readable_label(monkeypatch, capsys, tmp_path) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli
    from bithumb_bot.research.forward_diagnostics import ForwardDiagnosticsUnavailableError
    from tests.test_forward_diagnostics_report import _manager, _manifest

    monkeypatch.setattr(cli, "PATH_MANAGER", _manager(tmp_path))
    monkeypatch.setattr(cli, "load_manifest", lambda path: _manifest())
    monkeypatch.setattr(
        cli,
        "run_forward_diagnostics",
        lambda **kwargs: (_ for _ in ()).throw(ForwardDiagnosticsUnavailableError(("no_forward_targets",))),
    )

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="train",
        features=("range_ratio",),
        horizons=(1,),
        bucket="quantile:1",
        as_json=True,
    )
    output = capsys.readouterr().out

    assert code == 1
    assert not output.startswith("[RESEARCH-FORWARD-DIAGNOSTICS]")
    json.loads(output)


def _assert_generic_json_failure(output: str, *, code: int, error_type: str) -> dict[str, object]:
    assert code == 1
    assert not output.startswith("[RESEARCH-FORWARD-DIAGNOSTICS]")
    payload = json.loads(output)
    assert payload["artifact_type"] == "forward_return_diagnostic_failure"
    assert payload["diagnostic_status"] == "unavailable"
    assert payload["error_type"] == error_type
    assert payload["fail_reasons"]
    return payload


def test_cli_json_value_error_outputs_parseable_json(capsys) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="train",
        features=(),
        horizons=(1,),
        bucket="quantile:1",
        as_json=True,
    )
    output = capsys.readouterr().out
    payload = _assert_generic_json_failure(output, code=code, error_type="ValueError")

    assert payload["fail_reasons"] == ["value_error"]


def test_cli_json_manifest_validation_error_outputs_parseable_json(monkeypatch, capsys) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli
    from bithumb_bot.research.experiment_manifest import ManifestValidationError

    monkeypatch.setattr(
        cli,
        "load_manifest",
        lambda path: (_ for _ in ()).throw(ManifestValidationError("bad manifest")),
    )

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="train",
        features=("range_ratio",),
        horizons=(1,),
        bucket="quantile:1",
        as_json=True,
    )
    output = capsys.readouterr().out
    payload = _assert_generic_json_failure(output, code=code, error_type="ManifestValidationError")

    assert payload["fail_reasons"] == ["manifest_validation_error"]


def test_cli_json_os_error_outputs_parseable_json(monkeypatch, capsys) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli

    monkeypatch.setattr(
        cli,
        "load_manifest",
        lambda path: (_ for _ in ()).throw(OSError("cannot read manifest")),
    )

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="train",
        features=("range_ratio",),
        horizons=(1,),
        bucket="quantile:1",
        as_json=True,
    )
    output = capsys.readouterr().out
    payload = _assert_generic_json_failure(output, code=code, error_type="OSError")

    assert payload["fail_reasons"] == ["os_error"]


def test_cli_json_index_error_outputs_parseable_json(monkeypatch, capsys) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli

    monkeypatch.setattr(cli, "load_manifest", lambda path: object())
    monkeypatch.setattr(
        cli,
        "run_forward_diagnostics",
        lambda **kwargs: (_ for _ in ()).throw(IndexError("empty feature matrix")),
    )

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="train",
        features=("range_ratio",),
        horizons=(1,),
        bucket="quantile:1",
        as_json=True,
    )
    output = capsys.readouterr().out
    payload = _assert_generic_json_failure(output, code=code, error_type="IndexError")

    assert payload["fail_reasons"] == ["index_error"]


def test_cli_json_generic_failure_does_not_prefix_human_readable_label(capsys) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="train",
        features=("range_ratio",),
        horizons=(),
        bucket="quantile:1",
        as_json=True,
    )
    output = capsys.readouterr().out

    _assert_generic_json_failure(output, code=code, error_type="ValueError")


def test_forward_diagnostics_cli_does_not_rehydrate_result_from_dict() -> None:
    from pathlib import Path

    source = Path("src/bithumb_bot/research/forward_diagnostics_cli.py").read_text(encoding="utf-8")

    assert "_metric_from_payload" not in source
    assert 'result_payload["feature_bucket_metrics"]' not in source
