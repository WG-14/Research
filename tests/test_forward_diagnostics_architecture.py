from __future__ import annotations

from pathlib import Path

import pytest

from bithumb_bot.cli.registry import command_registry
from bithumb_bot.evidence_safety import diagnostic_feature_mining_taxonomy
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.forward_diagnostics_failure_report import validate_forward_diagnostics_failure_flags
from bithumb_bot.research.forward_diagnostics import run_forward_diagnostics_on_snapshot
from bithumb_bot.research.forward_diagnostics_report import validate_forward_diagnostics_report_flags
from bithumb_bot.research.split_usage_policy import SplitUsagePolicyError
from bithumb_bot.research.strategy_registry import list_research_strategy_plugins


ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _forward_diagnostics_contract_payload() -> dict[str, object]:
    return {
        "artifact_type": "forward_return_diagnostic_report",
        "diagnostic_only": True,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "live_readiness_evidence": False,
        "capital_allocation_evidence": False,
        **diagnostic_feature_mining_taxonomy(),
        "measurement_contract": {
            "return_basis": "gross_forward_return",
            "cost_adjustment": "none",
            "diagnostic_cost_model": "none",
            "execution_simulation": False,
            "fill_simulation": False,
            "order_lifecycle_simulation": False,
            "operator_interpretation": "feature_mining_only_not_expected_pnl",
        },
    }


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
        **_forward_diagnostics_contract_payload(),
        "calculation_policy": {
            "entry_price_mode": "signal_close",
            "path_start_policy": "next_candle_after_signal_close",
            "intrabar_included": False,
            "mfe_mae_basis": "ohlc_future_candles_only",
        },
    }

    validate_forward_diagnostics_report_flags(payload)


def test_forward_diagnostics_report_cannot_be_promotion_evidence() -> None:
    base_payload = _forward_diagnostics_contract_payload()
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
        **diagnostic_feature_mining_taxonomy(),
    }

    validate_forward_diagnostics_failure_flags(payload)


def test_forward_diagnostics_failure_artifact_uses_same_diagnostic_taxonomy() -> None:
    payload = {
        "artifact_type": "forward_return_diagnostic_failure",
        "diagnostic_only": True,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "live_readiness_evidence": False,
        "capital_allocation_evidence": False,
        **diagnostic_feature_mining_taxonomy(),
    }

    for key, value in diagnostic_feature_mining_taxonomy().items():
        assert payload[key] == value
    validate_forward_diagnostics_failure_flags(payload)


def test_forward_diagnostics_coverage_artifact_remains_diagnostic_only() -> None:
    payload = {
        **_forward_diagnostics_contract_payload(),
        "coverage": {"feature_horizon": []},
    }

    validate_forward_diagnostics_report_flags(payload)


def test_forward_diagnostics_command_remains_read_only() -> None:
    spec = command_registry()["research-forward-diagnostics"]

    assert spec.domain == "research"
    assert spec.read_only is True


def test_final_holdout_policy_is_not_cli_only() -> None:
    candles = tuple(
        Candle(ts=index, open=100 + index, high=102 + index, low=99 + index, close=101 + index, volume=10 + index)
        for index in range(25)
    )
    snapshot = DatasetSnapshot(
        snapshot_id="snapshot-final-holdout",
        source="test",
        market="BTC_KRW",
        interval="1m",
        split_name="final_holdout",
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=candles,
    )

    with pytest.raises(SplitUsagePolicyError):
        run_forward_diagnostics_on_snapshot(
            snapshot=snapshot,
            feature_names=("sma_gap",),
            horizon_steps=(1,),
            bucket_method="quantile:1",
            final_holdout_diagnostic_override=False,
            min_bucket_count=1,
        )


def test_core_direct_call_cannot_bypass_final_holdout_policy() -> None:
    test_final_holdout_policy_is_not_cli_only()
