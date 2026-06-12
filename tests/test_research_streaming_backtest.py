from __future__ import annotations

import pytest

from bithumb_bot.research.backtest_engine import BacktestRunContext
from bithumb_bot.research.backtest_kernel import run_decision_event_backtest
from bithumb_bot.research.backtest_pipeline import BacktestPipelineState, DefaultMarketReplayClock
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.decision_event import ResearchDecisionEvent
from bithumb_bot.research.experiment_manifest import DateRange


def _dataset(count: int = 12) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id="streaming_backtest",
        source="unit",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=tuple(
            Candle(index * 60_000, 100.0 + index, 101.0 + index, 99.0 + index, 100.0 + index, 1.0)
            for index in range(count + 1)
        ),
    )


def _events(dataset: DatasetSnapshot) -> tuple[ResearchDecisionEvent, ...]:
    return tuple(
        ResearchDecisionEvent(
            candle_ts=dataset.candles[index].ts,
            decision_ts=dataset.candles[index].ts + 60_000,
            strategy_name="buy_and_hold_baseline",
            strategy_version="buy_and_hold_baseline.research_contract.v1",
            raw_signal="BUY" if index == 1 else "HOLD",
            final_signal="BUY" if index == 1 else "HOLD",
            reason="streaming_backtest",
            feature_snapshot={"candle_index": index, "close": dataset.candles[index].close},
            strategy_diagnostics={"schema_version": 1, "index": index},
            entry_signal="BUY" if index == 1 else "HOLD",
            order_intent={"side": "BUY"} if index == 1 else None,
        )
        for index in range(1, len(dataset.candles))
    )


class _OneShotEvents:
    def __init__(self, events: tuple[ResearchDecisionEvent, ...]) -> None:
        self._events = events
        self.iterated = False

    def __iter__(self):  # type: ignore[no-untyped-def]
        if self.iterated:
            raise AssertionError("streaming events were consumed more than once")
        self.iterated = True
        yield from self._events


@pytest.mark.unit
@pytest.mark.contract
@pytest.mark.resource_guard
def test_streaming_event_builder_is_not_forced_to_tuple() -> None:
    dataset = _dataset()
    events = _OneShotEvents(_events(dataset))

    result = run_decision_event_backtest(
        dataset=dataset,
        strategy_name="buy_and_hold_baseline",
        parameter_values={"BUY_HOLD_BUY_INDEX": 1, "BUY_HOLD_DECISION_REASON": "streaming_backtest"},
        fee_rate=0.001,
        slippage_bps=5.0,
        decision_events=events,
        context=BacktestRunContext(report_detail="summary"),
    )

    assert result.resource_usage["decision_count"] == len(dataset.candles) - 1
    assert events.iterated is True


@pytest.mark.unit
@pytest.mark.contract
@pytest.mark.resource_guard
def test_streaming_replay_clock_does_not_materialize_all_ticks() -> None:
    dataset = _dataset(count=1_000)
    state = DefaultMarketReplayClock().run(
        BacktestPipelineState(
            dataset=dataset,
            strategy_name="buy_and_hold_baseline",
            parameter_values={},
            fee_rate=0.001,
            slippage_bps=5.0,
            decision_events=(event for event in _events(dataset)),
        )
    )

    assert not isinstance(state.ticks, (list, tuple))
    assert sum(1 for _ in state.ticks) == 1_000


@pytest.mark.unit
@pytest.mark.contract
@pytest.mark.resource_guard
def test_streaming_and_tuple_backtest_results_match_for_same_dataset() -> None:
    dataset = _dataset(count=30)
    events = _events(dataset)
    kwargs = {
        "dataset": dataset,
        "strategy_name": "buy_and_hold_baseline",
        "parameter_values": {"BUY_HOLD_BUY_INDEX": 1, "BUY_HOLD_DECISION_REASON": "streaming_backtest"},
        "fee_rate": 0.001,
        "slippage_bps": 5.0,
        "context": BacktestRunContext(report_detail="summary"),
    }

    tuple_result = run_decision_event_backtest(decision_events=events, **kwargs)
    streaming_result = run_decision_event_backtest(decision_events=(event for event in events), **kwargs)

    assert tuple_result.metrics == streaming_result.metrics
    assert tuple_result.resource_usage["trade_ledger_hash"] == streaming_result.resource_usage["trade_ledger_hash"]
    assert tuple_result.resource_usage["decision_behavior_hash"] == streaming_result.resource_usage[
        "decision_behavior_hash"
    ]
