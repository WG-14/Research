from __future__ import annotations

from dataclasses import replace

import pytest

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import (
    DateRange,
    ExecutionTimingPolicy,
    legacy_research_portfolio_policy,
)
from bithumb_bot.research.strategies.noop_baseline_events import (
    build_noop_baseline_events,
)
from bithumb_bot.research.strategies.noop_baseline_kernel import (
    run_noop_baseline_backtest,
)

from tests.research_noop_success_fixture import PRICES


def _dataset() -> DatasetSnapshot:
    return DatasetSnapshot(
        "noop_equivalence",
        "fixture",
        "KRW-BTC",
        "1m",
        "validation",
        DateRange("2026-01-01", "2026-01-01"),
        tuple(
            Candle(index * 60_000, price, price, price, price, 1.0)
            for index, price in enumerate(PRICES)
        ),
    )


def test_noop_research_kernel_matches_fixed_legacy_behavior_golden() -> None:
    dataset = _dataset()
    policy = legacy_research_portfolio_policy()
    params = {"NOOP_DECISION_START_INDEX": 1, "NOOP_DECISION_REASON": "golden"}
    events = build_noop_baseline_events(
        dataset=dataset,
        parameter_values=params,
        fee_rate=0.001,
        slippage_bps=10.0,
        execution_timing_policy=ExecutionTimingPolicy(),
        portfolio_policy=policy,
    )
    assert [
        (
            event.candle_ts,
            event.decision_ts,
            event.reason,
            event.raw_signal,
            event.entry_signal,
            event.exit_signal,
            event.final_signal,
            event.strategy_diagnostics["hold_decision_count"],
        )
        for event in events
    ] == [
        (60_000, 120_000, "golden", "HOLD", "HOLD", "HOLD", "HOLD", 1),
        (120_000, 180_000, "golden", "HOLD", "HOLD", "HOLD", "HOLD", 2),
        (180_000, 240_000, "golden", "HOLD", "HOLD", "HOLD", "HOLD", 3),
        (240_000, 300_000, "golden", "HOLD", "HOLD", "HOLD", "HOLD", 4),
    ]

    result = run_noop_baseline_backtest(
        dataset,
        params,
        0.001,
        10.0,
        portfolio_policy=policy,
    )

    assert len(result.decisions) == 4
    assert [item["candle_ts"] for item in result.decisions] == [60_000, 120_000, 180_000, 240_000]
    assert [item["decision_ts"] for item in result.decisions] == [120_000, 180_000, 240_000, 300_000]
    assert [item["reason"] for item in result.decisions] == ["golden"] * 4
    for key in ("raw_signal", "entry_signal", "exit_signal", "final_signal"):
        assert [item[key] for item in result.decisions] == ["HOLD"] * 4
    assert [item["strategy_diagnostics"]["hold_decision_count"] for item in result.decisions] == [1, 2, 3, 4]
    assert result.trades == ()
    assert result.execution_event_summary == {
        "execution_attempt_count": 0,
        "filled_execution_count": 0,
        "portfolio_applied_trade_count": 0,
    }
    assert result.metrics_v2 is not None
    assert result.metrics_v2.return_risk.total_return_pct == 0.0
    assert result.metrics_v2.return_risk.max_drawdown_pct == 0.0
    assert result.metrics_v2.trade_quality.execution_count == 0
    assert result.resource_usage["strategy_behavior_hash"].startswith("sha256:")
    assert result.resource_usage["behavior_hash"] == result.resource_usage["composite_behavior_hash"]


def test_noop_event_defaults_and_negative_start_index_preserve_legacy_semantics() -> None:
    events = build_noop_baseline_events(
        dataset=_dataset(),
        parameter_values={"NOOP_DECISION_START_INDEX": -3},
        fee_rate=0.0,
        slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(),
        portfolio_policy=legacy_research_portfolio_policy(),
    )

    assert [event.feature_snapshot["candle_index"] for event in events] == [0, 1, 2, 3, 4]
    assert all(event.feature_snapshot["start_index"] == 0 for event in events)
    assert [event.reason for event in events] == ["noop_baseline_hold"] * 5


def test_noop_marks_an_initial_position_without_changing_it() -> None:
    policy = replace(legacy_research_portfolio_policy(), initial_position_qty=10.0)
    result = run_noop_baseline_backtest(
        _dataset(),
        {"NOOP_DECISION_START_INDEX": 1, "NOOP_DECISION_REASON": "initial_position"},
        0.001,
        10.0,
        portfolio_policy=policy,
    )

    assert result.metrics_v2 is not None
    assert result.resource_usage["final_cash"] == 1_000_000.0
    assert result.resource_usage["final_asset_qty"] == 10.0
    assert result.resource_usage["final_marked_equity"] == 1_001_200.0
    assert result.metrics.return_pct == pytest.approx(0.12)
    assert result.metrics.max_drawdown_pct > 0.0
    assert all(point.cash == 1_000_000.0 and point.asset_qty == 10.0 for point in result.equity_curve)
