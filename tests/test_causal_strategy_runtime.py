import pytest
from dataclasses import replace
from market_research.orderbook_depth_store import build_orderbook_depth_snapshot
from market_research.research.causal_market_view import (
    CausalMarketView,
    FutureMarketAccessError,
)
from market_research.research.dataset_snapshot import TopOfBookQuote
from tests.test_common_simulation_engine import _dataset
from market_research.research_composition import builtin_strategy_registry
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.simulation_engine import ExecutionTimelineError
from market_research.research.decision_event import OrderIntent, ResearchDecisionEvent
from market_research.research.experiment_manifest import ExecutionTimingPolicy
from market_research.research.strategy_registry import StrategyRegistry
from market_research.strategy_sdk.runtime import make_event_builder_runtime_factory


def test_strategy_cannot_read_future_candle():
    view = CausalMarketView(_dataset(), 1, 120_000)
    with pytest.raises(FutureMarketAccessError, match="future_candle"):
        view.candle(2)


def test_strategy_cannot_read_quote_after_decision_boundary():
    visible = TopOfBookQuote(100_000, "KRW-BTC", 99, 101, 200, "fixture", 100.0)
    late = TopOfBookQuote(110_000, "KRW-BTC", 99, 101, 200, "fixture", 121.0)
    data = replace(_dataset(), top_of_book_event_quotes=(visible, late))

    view = CausalMarketView(data, 1, 120_000)

    assert view.quotes() == (visible,)
    assert visible.available_at_ms() <= 120_000 < late.available_at_ms()


def test_private_fields_do_not_expose_future_candle_quote_or_depth():
    late_quote = TopOfBookQuote(110_000, "KRW-BTC", 99, 101, 200, "fixture", 121.0)
    late_depth = build_orderbook_depth_snapshot(
        ts=110_000,
        pair="KRW-BTC",
        bid_levels=((99, 1),),
        ask_levels=((101, 1),),
        source="fixture",
        observed_at_epoch_sec=121.0,
    )
    data = replace(
        _dataset(),
        top_of_book_event_quotes=(late_quote,),
        orderbook_depth_snapshots=(late_depth,),
    )

    view = CausalMarketView(data, 1, 120_000)
    snapshot = view._causal_snapshot
    assert len(snapshot.candles) == 2
    assert not snapshot.execution_top_of_book_quotes()
    assert not snapshot.orderbook_depth_snapshots
    assert not hasattr(view, "_dataset")


def test_strategy_snapshot_scrubs_whole_split_identity_and_future_scope() -> None:
    base = _dataset()
    first = replace(
        base,
        source_uri="/external/full-split-a.sqlite",
        source_content_hash="sha256:" + "1" * 64,
        source_schema_hash="sha256:" + "2" * 64,
        artifact_id="future-sensitive-artifact-a",
        artifact_content_hash="sha256:" + "3" * 64,
        artifact_schema_hash="sha256:" + "4" * 64,
        artifact_manifest_hash="sha256:" + "5" * 64,
        source_provenance_hash="sha256:" + "6" * 64,
        locator={"path": "/external/full-split-a.sqlite"},
        options={"future_row_count": 5},
        adapter_provenance={"actual_scope": {"row_count": 5}},
        verification={"actual_scope": {"row_count": 5}},
    )
    changed_candles = (base.candles[0],) + tuple(
        replace(candle, close=candle.close + 10_000) for candle in base.candles[1:]
    )
    second = replace(
        first,
        candles=changed_candles,
        source_uri="/external/full-split-b.sqlite",
        artifact_content_hash="sha256:" + "7" * 64,
        options={"future_row_count": 500},
        verification={"actual_scope": {"row_count": 500}},
    )

    visible_a = CausalMarketView.from_dataset(first, 0, 60_000).causal_snapshot()
    visible_b = CausalMarketView.from_dataset(second, 0, 60_000).causal_snapshot()

    assert visible_a.snapshot_id == visible_b.snapshot_id == "strategy_causal_view"
    assert visible_a.split_name == "causal_visible_prefix"
    assert visible_a.source_uri is visible_a.artifact_content_hash is None
    assert visible_a.locator is visible_a.options is visible_a.verification is None
    assert (
        visible_a.snapshot_fingerprint_hash() == visible_b.snapshot_fingerprint_hash()
    )


def test_current_candle_is_rejected_before_derived_interval_close_availability():
    with pytest.raises(FutureMarketAccessError, match="knowledge_time"):
        CausalMarketView(_dataset(), 1, 119_999)

    view = CausalMarketView(_dataset(), 1, 120_000)
    assert view.current_knowledge_time_evidence() == {
        "schema_version": 1,
        "event_time_ts": 60_000,
        "available_at_ts": 120_000,
        "decision_boundary_ts": 120_000,
        "available_at_lte_decision": True,
        "availability_policy": "ohlcv_interval_close",
        "interval": "1m",
    }


def test_all_production_builtin_plugins_have_runtime_factory():
    registry = builtin_strategy_registry()
    assert set(registry.plugins) == {
        "sma_with_filter",
        "buy_and_hold_baseline",
        "noop_baseline",
        "threshold_research_only",
    }
    assert all(
        plugin.runtime_factory is not None for plugin in registry.plugins.values()
    )


def test_common_runtime_adapter_invokes_current_only_builder_once_per_candle():
    observed_rows = []

    def event_builder(**values):
        observed_rows.append(len(values["dataset"].candles))
        return ()

    base = builtin_strategy_registry().resolve("noop_baseline")
    plugin = replace(
        base,
        event_builder=event_builder,
        runtime_factory=make_event_builder_runtime_factory(
            event_builder,
            current_candle_only=True,
        ),
    )
    registry = StrategyRegistry.build((plugin,))

    run_common_simulation_backtest(
        plugin=plugin,
        registry=registry,
        dataset=_dataset(),
        parameter_values={},
        fee_rate=0,
        slippage_bps=0,
    )

    assert observed_rows == [1] * len(_dataset().candles)


def test_strategy_callback_cannot_consume_assumed_market_knowledge_time() -> None:
    observed_quote_counts = []

    def event_builder(**values):
        observed_quote_counts.append(
            len(values["dataset"].execution_top_of_book_quotes())
        )
        return ()

    base = builtin_strategy_registry().resolve("noop_baseline")
    plugin = replace(
        base,
        event_builder=event_builder,
        runtime_factory=make_event_builder_runtime_factory(
            event_builder,
            current_candle_only=True,
        ),
    )
    registry = StrategyRegistry.build((plugin,))
    assumed_quote = TopOfBookQuote(60_000, "KRW-BTC", 99, 101, 200, "fixture", None)
    dataset = replace(_dataset(), top_of_book_event_quotes=(assumed_quote,))

    run_common_simulation_backtest(
        plugin=plugin,
        registry=registry,
        dataset=dataset,
        parameter_values={},
        fee_rate=0,
        slippage_bps=0,
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="next_candle_open"
        ),
    )

    assert observed_quote_counts == [0] * len(dataset.candles)


@pytest.mark.parametrize(
    ("candle_offset", "decision_offset", "reason"),
    [
        (0, 0, "strategy_decision_precedes_knowledge_boundary"),
        (-60_000, 60_000, "strategy_decision_candle_mismatch"),
    ],
)
def test_runtime_hold_decision_cannot_bypass_causal_timeline_validation(
    candle_offset: int, decision_offset: int, reason: str
) -> None:
    base = builtin_strategy_registry().resolve("noop_baseline")

    class InvalidHoldRuntime:
        def initialize(self, context):
            return {}

        def on_market_event(self, market, portfolio, state):
            candle = market.current_candle
            return (
                ResearchDecisionEvent(
                    candle_ts=int(candle.ts) + candle_offset,
                    decision_ts=int(candle.ts) + decision_offset,
                    strategy_name=base.name,
                    strategy_version=base.version,
                    raw_signal="HOLD",
                    final_signal="HOLD",
                    reason="invalid-runtime-timeline",
                    feature_snapshot={},
                    strategy_diagnostics={},
                ),
            )

    def runtime_factory(**values):
        return InvalidHoldRuntime()

    plugin = replace(base, runtime_factory=runtime_factory)
    registry = StrategyRegistry.build((plugin,))

    with pytest.raises(ExecutionTimelineError, match=reason):
        run_common_simulation_backtest(
            plugin=plugin,
            registry=registry,
            dataset=_dataset(),
            parameter_values={},
            fee_rate=0,
            slippage_bps=0,
        )


def test_missing_quote_status_is_not_visible_before_wait_deadline() -> None:
    base = builtin_strategy_registry().resolve("buy_and_hold_baseline")
    observed_statuses: list[str | None] = []

    class MissingQuoteRuntime:
        def initialize(self, context):
            return {}

        def on_market_event(self, market, portfolio, state):
            observed_statuses.append(portfolio.last_execution_status)
            candle = market.current_candle
            side = "BUY" if market.current_index == 0 else "HOLD"
            event = ResearchDecisionEvent(
                candle_ts=int(candle.ts),
                decision_ts=int(candle.ts) + 60_000,
                strategy_name=base.name,
                strategy_version=base.version,
                raw_signal=side,
                final_signal=side,
                entry_signal=side if side == "BUY" else None,
                reason="missing-quote-deadline-fixture",
                feature_snapshot={},
                strategy_diagnostics={},
            )
            if side == "BUY":
                event = replace(
                    event,
                    order_intent=OrderIntent.from_decision(
                        decision_id=event.decision_id(),
                        side="BUY",
                        sizing="portfolio_policy_fractional_cash",
                        buy_fraction=1.0,
                        order_intent_ts=event.decision_ts,
                    ),
                )
            return (event,)

    def runtime_factory(**values):
        return MissingQuoteRuntime()

    plugin = replace(base, runtime_factory=runtime_factory)
    registry = StrategyRegistry.build((plugin,))
    run = run_common_simulation_backtest(
        plugin=plugin,
        registry=registry,
        dataset=_dataset(),
        parameter_values={"BUY_HOLD_BUY_INDEX": 0},
        fee_rate=0,
        slippage_bps=0,
        execution_timing_policy=ExecutionTimingPolicy(
            fill_reference_policy="first_orderbook_after_decision",
            max_quote_wait_ms=90_000,
        ),
    )

    assert run.fills[0].execution_reference_deadline_ts == 150_000
    assert run.fills[0].execution_resolution_ts == 150_000
    assert run.fills[0].portfolio_effective_ts == 150_000
    assert observed_statuses[:3] == [None, None, "failed"]
