from __future__ import annotations

import json

import pytest

from bithumb_bot.research.diagnostic_availability import DiagnosticAvailability
from bithumb_bot.research.forward_diagnostics_failure_report import (
    build_forward_diagnostics_failure_payload,
    validate_forward_diagnostics_failure_flags,
    write_forward_diagnostics_failure_artifact,
)
from tests.test_forward_diagnostics_report import _manager, _manifest


def _availability() -> DiagnosticAvailability:
    return DiagnosticAvailability(
        status="unavailable",
        fail_reasons=("no_forward_targets",),
        warnings=(),
        target_count=0,
        sample_count=0,
        feature_value_count=0,
    )


def test_failure_artifact_is_diagnostic_only() -> None:
    payload = build_forward_diagnostics_failure_payload(
        manifest=_manifest(),
        split_name="train",
        feature_names=("range_ratio",),
        horizon_steps=(1,),
        fail_reasons=("no_forward_targets",),
        availability=_availability(),
    )

    assert payload["artifact_type"] == "forward_return_diagnostic_failure"
    assert payload["diagnostic_only"] is True
    assert payload["promotion_evidence"] is False
    assert payload["approved_profile_evidence"] is False
    assert payload["live_readiness_evidence"] is False
    assert payload["capital_allocation_evidence"] is False


def test_failure_artifact_cannot_be_promotion_evidence() -> None:
    payload = build_forward_diagnostics_failure_payload(
        manifest=_manifest(),
        split_name="train",
        feature_names=("range_ratio",),
        horizon_steps=(1,),
        fail_reasons=("no_forward_targets",),
        availability=_availability(),
    )
    payload["promotion_evidence"] = True

    with pytest.raises(ValueError, match="diagnostic-only"):
        validate_forward_diagnostics_failure_flags(payload)


def test_unavailable_diagnostic_writes_failure_artifact_not_success_report(tmp_path) -> None:
    manager = _manager(tmp_path)
    payload = write_forward_diagnostics_failure_artifact(
        manager=manager,
        manifest=_manifest(),
        split_name="train",
        feature_names=("range_ratio",),
        horizon_steps=(1,),
        fail_reasons=("no_forward_targets",),
        availability=_availability(),
    )

    assert (manager.data_dir() / "reports/research/exp1/forward_diagnostics_failure.json").exists()
    assert not (manager.data_dir() / "reports/research/exp1/forward_diagnostics_report.json").exists()
    assert payload["artifact_type"] == "forward_return_diagnostic_failure"


def test_cli_json_failure_output_is_machine_readable(monkeypatch, capsys, tmp_path) -> None:
    import bithumb_bot.research.forward_diagnostics_cli as cli
    from bithumb_bot.research.forward_diagnostics import ForwardDiagnosticsUnavailableError

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
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["artifact_type"] == "forward_return_diagnostic_failure"
    assert payload["diagnostic_status"] == "unavailable"
    assert payload["fail_reasons"] == ["no_forward_targets"]
