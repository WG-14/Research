from __future__ import annotations

from bithumb_bot.research.backtest_engine import BacktestRunContext
from tests.test_research_backtest_observability_policy import _run


def test_empty_and_invariant_hashes_are_precomputed_outside_tick_loop(monkeypatch) -> None:
    from bithumb_bot.research import backtest_stage_runner

    labels: list[str] = []
    real_hash = backtest_stage_runner.canonical_payload_hash

    def spy(value, *, label="canonical_payload"):  # type: ignore[no-untyped-def]
        labels.append(str(label))
        return real_hash(value, label=label)

    monkeypatch.setattr(backtest_stage_runner, "canonical_payload_hash", spy)

    _run(context=BacktestRunContext(report_detail="summary", diagnostic_mode="exploratory"), count=6)

    assert labels.count("invariant_empty_fill") == 1
    assert labels.count("invariant_empty_order_rules") == 1
    assert labels.count("invariant_execution_timing_policy") == 1


def test_tick_variant_hashes_still_change_with_tick_state() -> None:
    result = _run(context=BacktestRunContext(report_detail="summary", diagnostic_mode="exploratory"), count=4)
    replay_hashes = [
        trace["payload"]["replay_tick_hash"]
        for trace in result.resource_usage["stage_trace"]
        if trace["stage_id"] == "strategy"
    ]

    assert len(replay_hashes) == 4
    assert len(set(replay_hashes)) == 4
