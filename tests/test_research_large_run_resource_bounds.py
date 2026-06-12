from __future__ import annotations

import pytest

from bithumb_bot.research.backtest_engine import BacktestResourceLimits, BacktestRunContext
from bithumb_bot.research.workload_estimate import build_manifest_workload_estimate
from tests.test_research_backtest_observability_policy import _run
from tests.test_research_memory_admission import _manifest_with_workers


@pytest.mark.resource_guard
@pytest.mark.memory_sensitive
def test_channel_breakout_summary_mode_large_run_keeps_bounded_observability() -> None:
    result = _run(
        context=BacktestRunContext(
            report_detail="summary",
            resource_limits=BacktestResourceLimits(max_decisions_retained=0, max_equity_points_retained=0),
        ),
        count=10_000,
    )

    usage = result.resource_usage
    assert usage["retained_decision_count"] == 0
    assert usage["stage_trace_count"] >= 10_000
    assert len(usage.get("stage_trace", ())) <= usage["stage_trace_max_retained_traces"]
    assert usage["behavior_hash_material_count"] == 10_000
    assert usage["behavior_hash"].startswith("sha256:")
    assert usage["stage_trace_hash"].startswith("sha256:")


@pytest.mark.resource_guard
@pytest.mark.memory_sensitive
def test_delayed_confirmation_large_run_does_not_retain_per_tick_feature_payloads() -> None:
    result = _run(
        context=BacktestRunContext(
            report_detail="summary",
            resource_limits=BacktestResourceLimits(max_decisions_retained=0, max_equity_points_retained=0),
        ),
        count=10_000,
    )

    assert result.decisions == ()
    assert result.resource_usage["behavior_hash_material_sample_count"] < 100
    assert result.resource_usage["strategy_behavior_hash"].startswith("sha256:")


@pytest.mark.resource_guard
@pytest.mark.memory_sensitive
def test_large_run_resource_usage_reports_evidence_counts_and_hashes() -> None:
    result = _run(
        context=BacktestRunContext(
            report_detail="summary",
            resource_limits=BacktestResourceLimits(max_decisions_retained=0, max_equity_points_retained=0),
        ),
        count=10_000,
    )

    usage = result.resource_usage
    assert usage["decision_hash_material_count"] == 10_000
    assert usage["behavior_hash_material_count"] == 10_000
    assert usage["behavior_hash"].startswith("sha256:")
    assert usage["stage_trace_hash"].startswith("sha256:")


@pytest.mark.nightly
@pytest.mark.memory_sensitive
def test_nightly_large_run_workers_8_uses_memory_admission_or_batches() -> None:
    estimate = build_manifest_workload_estimate(
        _manifest_with_workers(
            8,
            entry_modes=["immediate_breakout", "delayed_confirmation"],
            max_total_memory_mb=1.0,
        )
    )

    assert estimate["max_in_flight_tasks"] == 16
    assert estimate["safe_max_workers_by_memory_budget"] < 8
    assert estimate["memory_budget_status"] == "WARN"
