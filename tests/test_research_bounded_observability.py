from __future__ import annotations

import pytest

from bithumb_bot.research.backtest_engine import BacktestResourceLimits, BacktestRunContext
from tests.test_research_backtest_observability_policy import _run


@pytest.mark.unit
@pytest.mark.resource_guard
@pytest.mark.memory_sensitive
def test_summary_mode_does_not_retain_per_tick_behavior_material() -> None:
    result = _run(
        context=BacktestRunContext(
            report_detail="summary",
            resource_limits=BacktestResourceLimits(
                max_decisions_retained=0,
                max_equity_points_retained=0,
            ),
        ),
        count=10_000,
    )

    usage = result.resource_usage
    assert usage["retained_decision_count"] == 0
    assert len(result.decisions) == 0
    assert usage["decision_hash_material_count"] == 10_000
    assert usage["behavior_hash_material_count"] == 10_000
    assert usage["behavior_hash_material_sample_count"] < 100
    assert usage["behavior_hash_material_retention_policy"].startswith("streaming_digest")
    assert usage["behavior_hash"].startswith("sha256:")


@pytest.mark.unit
@pytest.mark.resource_guard
@pytest.mark.memory_sensitive
def test_behavior_hash_is_stable_after_streaming_digest_refactor() -> None:
    context = BacktestRunContext(
        report_detail="summary",
        resource_limits=BacktestResourceLimits(
            max_decisions_retained=0,
            max_equity_points_retained=0,
        ),
    )

    first = _run(context=context, count=10_000)
    second = _run(context=context, count=10_000)

    assert first.resource_usage["behavior_hash"] == second.resource_usage["behavior_hash"]
    assert first.resource_usage["strategy_behavior_hash"] == second.resource_usage["strategy_behavior_hash"]


@pytest.mark.unit
@pytest.mark.resource_guard
@pytest.mark.memory_sensitive
def test_max_decisions_retained_zero_still_produces_behavior_hash_without_payload_list() -> None:
    result = _run(
        context=BacktestRunContext(
            report_detail="summary",
            resource_limits=BacktestResourceLimits(
                max_decisions_retained=0,
                max_equity_points_retained=0,
            ),
        ),
        count=10_000,
    )

    assert result.decisions == ()
    assert result.resource_usage["strategy_behavior_hash"].startswith("sha256:")
    assert result.resource_usage["behavior_hash_material_count"] == 10_000
    assert len(result.resource_usage.get("stage_trace", ())) <= result.resource_usage["stage_trace_max_retained_traces"]
