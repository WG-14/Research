from __future__ import annotations

from bithumb_bot.research.diagnostic_availability import DiagnosticAvailability
from tests.test_forward_diagnostics_report import _result


def _degraded_result(*, override: bool):
    return _result(
        availability=DiagnosticAvailability(
            status="degraded",
            fail_reasons=(),
            warnings=("dataset_quality_failed",),
            target_count=1,
            sample_count=1,
            feature_value_count=1,
        ),
        warnings=({"reason": "dataset_quality_failed"},),
        degraded_override=override,
    )


def test_cli_returns_nonzero_for_degraded_without_override(monkeypatch) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli

    monkeypatch.setattr(cli, "load_manifest", lambda path: type("Manifest", (), {"experiment_id": "exp1", "manifest_hash": lambda self: "sha256:" + "1" * 64})())
    monkeypatch.setattr(cli, "run_forward_diagnostics", lambda **kwargs: _degraded_result(override=False))
    monkeypatch.setattr(cli, "write_forward_diagnostics_report", lambda **kwargs: {"artifact_paths": {"report": "/tmp/report.json"}})

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="train",
        features=("sma_gap",),
        horizons=(1,),
        bucket="quantile:1",
    )

    assert code == 1


def test_cli_returns_zero_for_degraded_with_explicit_override(monkeypatch) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli

    calls: dict[str, object] = {}
    monkeypatch.setattr(cli, "load_manifest", lambda path: type("Manifest", (), {"experiment_id": "exp1", "manifest_hash": lambda self: "sha256:" + "1" * 64})())

    def fake_run_forward_diagnostics(**kwargs):
        calls.update(kwargs)
        return _degraded_result(override=True)

    monkeypatch.setattr(cli, "run_forward_diagnostics", fake_run_forward_diagnostics)
    monkeypatch.setattr(cli, "write_forward_diagnostics_report", lambda **kwargs: {"artifact_paths": {"report": "/tmp/report.json"}})

    code = cli.cmd_research_forward_diagnostics(
        manifest_path="manifest.json",
        split_name="train",
        features=("sma_gap",),
        horizons=(1,),
        bucket="quantile:1",
        allow_degraded_diagnostics=True,
    )

    assert code == 0
    assert calls["degraded_override"] is True


def test_degraded_report_records_override_false() -> None:
    result = _degraded_result(override=False)

    assert result.degraded_override is False
    assert result.degraded_exit_policy["allow_degraded_diagnostics"] is False


def test_degraded_report_records_override_true() -> None:
    result = _degraded_result(override=True)

    assert result.degraded_override is True
    assert result.degraded_exit_policy["allow_degraded_diagnostics"] is True
