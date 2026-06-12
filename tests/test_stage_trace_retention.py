from __future__ import annotations

import pytest

from bithumb_bot.research.backtest_stages import StageTrace
from bithumb_bot.research.stage_trace_recorder import StageTraceRecorder
from tests.test_research_backtest_observability_policy import _run
from bithumb_bot.research.backtest_engine import BacktestRunContext


def _trace(index: int) -> StageTrace:
    return StageTrace(
        stage_id="strategy",
        input_hash=f"sha256:input-{index}",
        output_hash=f"sha256:output-{index}",
        reason_code="OK",
        payload={"index": index},
    )


@pytest.mark.unit
@pytest.mark.resource_guard
@pytest.mark.memory_sensitive
def test_stage_trace_recorder_keeps_only_bounded_latest_traces() -> None:
    recorder = StageTraceRecorder(max_retained_traces=32)
    for index in range(1_000):
        recorder.record(_trace(index))

    assert recorder.trace_count == 1_000
    assert len(recorder.traces) <= 32
    evidence = recorder.compact_evidence()
    assert evidence["stage_trace_count"] == 1_000
    assert evidence["stage_trace_sample_count"] <= 32
    assert evidence["stage_trace_hash"].startswith("sha256:")


@pytest.mark.unit
@pytest.mark.resource_guard
@pytest.mark.memory_sensitive
def test_backtest_resource_usage_does_not_embed_full_stage_trace_array() -> None:
    result = _run(context=BacktestRunContext(report_detail="summary"), count=1_000)
    usage = result.resource_usage

    assert usage["stage_trace_count"] >= 1_000
    assert usage["stage_trace_hash"].startswith("sha256:")
    assert len(usage.get("stage_trace", ())) <= usage["stage_trace_max_retained_traces"]
    assert len(usage["stage_trace_sample"]) <= usage["stage_trace_max_retained_traces"]


@pytest.mark.unit
@pytest.mark.resource_guard
@pytest.mark.memory_sensitive
def test_stage_trace_hash_changes_when_trace_order_changes() -> None:
    first = StageTraceRecorder(max_retained_traces=8)
    second = StageTraceRecorder(max_retained_traces=8)
    for index in range(10):
        first.record(_trace(index))
    for index in reversed(range(10)):
        second.record(_trace(index))

    assert first.trace_count == second.trace_count == 10
    assert first.trace_digest != second.trace_digest
