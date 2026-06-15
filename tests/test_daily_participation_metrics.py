from __future__ import annotations

from bithumb_bot.research.metrics_contract import (
    ClosedTradeRecord,
    ExecutionRecord,
    build_participation_metrics,
)


def test_participation_metrics_count_kst_days() -> None:
    metrics = build_participation_metrics(
        period_start_ts=1_704_031_200_000,
        period_end_ts=1_704_117_600_000,
        execution_records=(ExecutionRecord("BUY", "filled", 1.0, 100.0, ts=1_704_031_200_000),),
    )

    assert metrics.calendar_day_count == 2
    assert metrics.days_with_filled_execution == 1


def test_zero_trade_days_detected() -> None:
    metrics = build_participation_metrics(
        period_start_ts=1_704_031_200_000,
        period_end_ts=1_704_204_000_000,
        execution_records=(ExecutionRecord("BUY", "filled", 1.0, 100.0, ts=1_704_031_200_000),),
    )

    assert metrics.zero_filled_days == 2
    assert metrics.max_consecutive_zero_filled_days == 2


def test_one_day_many_trades_does_not_satisfy_all_days() -> None:
    metrics = build_participation_metrics(
        period_start_ts=1_704_031_200_000,
        period_end_ts=1_704_204_000_000,
        execution_records=tuple(
            ExecutionRecord("BUY", "filled", 1.0, 100.0, ts=1_704_031_200_000 + index * 60_000)
            for index in range(30)
        ),
    )

    assert metrics.calendar_day_count == 3
    assert metrics.days_with_filled_execution == 1
    assert metrics.zero_filled_days == 2


def test_participation_metrics_include_count_basis_breakdown() -> None:
    metrics = build_participation_metrics(
        period_start_ts=1_704_031_200_000,
        period_end_ts=1_704_117_600_000,
        decision_records=({"decision_ts": 1_704_031_200_000, "final_signal": "BUY"},),
        execution_records=(ExecutionRecord("BUY", "filled", 1.0, 100.0, ts=1_704_031_200_000),),
        closed_trades=(ClosedTradeRecord(exit_ts=1_704_117_600_000, net_pnl=1.0),),
        count_basis="filled",
    )

    payload = metrics.as_dict()
    assert payload["days_with_intent"] == 1
    assert payload["days_with_submitted"] == 1
    assert payload["days_with_filled_execution"] == 1
    assert payload["days_with_closed_trade"] == 1

