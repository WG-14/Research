from __future__ import annotations

import pytest

from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.execution_plan import build_research_execution_plan
from bithumb_bot.research import validation_protocol
from bithumb_bot.strategy_plugins.channel_breakout_research import estimate_channel_breakout_complexity
from tests.test_research_backtest_reproducibility import _create_db, _manifest


@pytest.mark.unit
@pytest.mark.contract
def test_channel_breakout_delayed_confirmation_has_higher_payload_estimate_than_immediate() -> None:
    immediate = estimate_channel_breakout_complexity(
        strategy_name="channel_breakout_with_regime_filter",
        parameter_space={"ENTRY_MODE": ("immediate_breakout",)},
        report_detail="summary",
        expected_candle_count=10_000,
    )
    delayed = estimate_channel_breakout_complexity(
        strategy_name="channel_breakout_with_regime_filter",
        parameter_space={"ENTRY_MODE": ("immediate_breakout", "delayed_confirmation")},
        report_detail="summary",
        expected_candle_count=10_000,
    )

    assert delayed["expected_us_per_candle"] > immediate["expected_us_per_candle"]
    assert delayed["expected_decision_payload_bytes_per_event"] > immediate[
        "expected_decision_payload_bytes_per_event"
    ]


@pytest.mark.unit
@pytest.mark.contract
def test_complexity_estimate_includes_observability_policy() -> None:
    summary = estimate_channel_breakout_complexity(
        strategy_name="channel_breakout_with_regime_filter",
        parameter_space={"ENTRY_MODE": ("immediate_breakout",)},
        report_detail="summary",
    )
    full = estimate_channel_breakout_complexity(
        strategy_name="channel_breakout_with_regime_filter",
        parameter_space={"ENTRY_MODE": ("immediate_breakout",)},
        report_detail="full",
    )

    assert full["expected_decision_payload_bytes_per_event"] > summary[
        "expected_decision_payload_bytes_per_event"
    ]
    assert "full_observability_payloads" in full["complexity_reasons"]


@pytest.mark.unit
@pytest.mark.contract
def test_workload_plan_uses_parameter_aware_complexity_estimate(tmp_path) -> None:
    db_path = tmp_path / "candles.sqlite"
    _create_db(db_path)
    payload = _manifest()
    payload["strategy_name"] = "channel_breakout_with_regime_filter"
    payload["parameter_space"] = {
        "CHANNEL_BREAKOUT_LOOKBACK": [3],
        "CHANNEL_BREAKOUT_RANGE_WINDOW": [3],
        "CHANNEL_BREAKOUT_VOLUME_WINDOW": [3],
        "ENTRY_MODE": ["immediate_breakout", "delayed_confirmation"],
    }
    manifest = parse_manifest(payload)
    snapshots = {
        split_name: validation_protocol.load_dataset_split(db_path=db_path, manifest=manifest, split_name=split_name)
        for split_name in ("train", "validation", "final_holdout")
    }
    quality_reports = validation_protocol._quality_reports(db_path=db_path, snapshots=snapshots)

    plan = build_research_execution_plan(
        manifest=manifest,
        snapshots=snapshots,
        quality_reports=quality_reports,
        db_path=db_path,
        repository_version="unit",
        created_at="2026-05-03T00:00:00+00:00",
    ).as_dict()

    assert plan["plugin_complexity"]["expected_us_per_candle"] > 25
    assert "delayed_confirmation_pending_state" in plan["plugin_complexity"]["complexity_reasons"]
    assert plan["workload_estimate"]["plugin_complexity"] == plan["plugin_complexity"]
