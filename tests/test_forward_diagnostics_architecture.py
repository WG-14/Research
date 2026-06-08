from __future__ import annotations

from pathlib import Path

import pytest

from bithumb_bot.cli.registry import command_registry
from bithumb_bot.research.forward_diagnostics_failure_report import validate_forward_diagnostics_failure_flags
from bithumb_bot.research.forward_diagnostics_report import validate_forward_diagnostics_report_flags
from bithumb_bot.research.strategy_registry import list_research_strategy_plugins


ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_forward_diagnostics_not_registered_as_strategy_plugin() -> None:
    plugin_names = {plugin.name for plugin in list_research_strategy_plugins()}

    assert "forward_return_diagnostics" not in plugin_names
    assert not (ROOT / "src/bithumb_bot/strategy_plugins/forward_return_diagnostics.py").exists()
    assert not (ROOT / "src/bithumb_bot/strategy_plugins/forward_diagnostics.py").exists()


def test_forward_diagnostics_not_registered_as_strategy_plugin_after_coverage_refactor() -> None:
    test_forward_diagnostics_not_registered_as_strategy_plugin()


def test_forward_diagnostics_not_added_to_backtest_pipeline() -> None:
    source = _source("src/bithumb_bot/research/backtest_pipeline.py")

    assert "forward_diagnostics" not in source
    assert "forward_return_diagnostic" not in source


def test_forward_diagnostics_not_added_to_backtest_pipeline_after_availability_refactor() -> None:
    test_forward_diagnostics_not_added_to_backtest_pipeline()


def test_forward_diagnostics_not_registered_under_strategy_cli() -> None:
    source = _source("src/bithumb_bot/cli/commands/strategy.py")

    assert "research-forward-diagnostics" not in source


def test_forward_diagnostics_not_registered_under_runtime_cli() -> None:
    source = _source("src/bithumb_bot/cli/commands/runtime.py")

    assert "research-forward-diagnostics" not in source


def test_forward_diagnostics_modules_live_under_research_namespace() -> None:
    registry = command_registry()

    assert registry["research-forward-diagnostics"].domain == "research"
    for relative in (
        "src/bithumb_bot/research/forward_diagnostics.py",
        "src/bithumb_bot/research/forward_targets.py",
        "src/bithumb_bot/research/feature_diagnostic_features.py",
        "src/bithumb_bot/research/feature_bucket_metrics.py",
        "src/bithumb_bot/research/diagnostic_availability.py",
        "src/bithumb_bot/research/diagnostic_coverage.py",
        "src/bithumb_bot/research/forward_diagnostics_report.py",
        "src/bithumb_bot/research/forward_diagnostics_failure_report.py",
    ):
        assert (ROOT / relative).exists()


def test_forward_diagnostics_report_remains_diagnostic_only_after_policy_fields_added() -> None:
    payload = {
        "diagnostic_only": True,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "live_readiness_evidence": False,
        "capital_allocation_evidence": False,
        "calculation_policy": {
            "entry_price_mode": "signal_close",
            "path_start_policy": "next_candle_after_signal_close",
            "intrabar_included": False,
            "mfe_mae_basis": "ohlc_future_candles_only",
        },
    }

    validate_forward_diagnostics_report_flags(payload)


def test_forward_diagnostics_report_cannot_be_promotion_evidence() -> None:
    base_payload = {
        "diagnostic_only": True,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "live_readiness_evidence": False,
        "capital_allocation_evidence": False,
    }
    for field in (
        "promotion_evidence",
        "approved_profile_evidence",
        "live_readiness_evidence",
        "capital_allocation_evidence",
    ):
        payload = dict(base_payload)
        payload[field] = True
        with pytest.raises(ValueError, match="diagnostic-only"):
            validate_forward_diagnostics_report_flags(payload)


def test_forward_diagnostics_failure_artifact_remains_diagnostic_only() -> None:
    payload = {
        "artifact_type": "forward_return_diagnostic_failure",
        "diagnostic_only": True,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "live_readiness_evidence": False,
        "capital_allocation_evidence": False,
    }

    validate_forward_diagnostics_failure_flags(payload)


def test_forward_diagnostics_coverage_artifact_remains_diagnostic_only() -> None:
    payload = {
        "artifact_type": "forward_return_diagnostic_report",
        "diagnostic_only": True,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "live_readiness_evidence": False,
        "capital_allocation_evidence": False,
        "coverage": {"feature_horizon": []},
    }

    validate_forward_diagnostics_report_flags(payload)


def test_forward_diagnostics_command_remains_read_only() -> None:
    spec = command_registry()["research-forward-diagnostics"]

    assert spec.domain == "research"
    assert spec.read_only is True
