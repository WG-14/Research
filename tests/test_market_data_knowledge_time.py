from __future__ import annotations

from dataclasses import replace
import sqlite3
from types import SimpleNamespace

import pytest

from market_research.orderbook_depth_store import (
    build_orderbook_depth_snapshot,
    load_orderbook_depth_snapshot_after_or_equal,
    summarize_orderbook_depth_evidence,
    upsert_orderbook_depth_snapshot,
)
from market_research.research.causal_market_view import CausalMarketView
from market_research.research.dataset_snapshot import (
    TopOfBookQuote,
    _load_orderbook_depth_event_snapshots,
    _orderbook_depth_summary_from_snapshot,
)
from market_research.research.data_plane import _top_of_book_split_sql
from market_research.research.execution_timing import (
    build_signal_event,
    resolve_execution_reference,
)
from market_research.research.execution_model import DepthWalkExecutionModel
from market_research.research.execution_model import FixedBpsExecutionModel
from market_research.research.execution_evidence import (
    ExecutionEvidenceError,
    validate_execution_evidence,
)
from market_research.research.experiment_manifest import (
    ExecutionTimingPolicy,
    TopOfBookDatasetSpec,
    legacy_research_portfolio_policy,
)
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy
from market_research.orderbook_top_store import build_orderbook_top_snapshot
from tests.test_common_simulation_engine import _dataset


def test_quote_event_time_cannot_bypass_later_observation_time() -> None:
    quote = TopOfBookQuote(
        ts=60_500,
        pair="KRW-BTC",
        bid_price=99.0,
        ask_price=101.0,
        spread_bps=200.0,
        source="fixture",
        observed_at_epoch_sec=61.0,
    )
    dataset = replace(_dataset(), top_of_book_event_quotes=(quote,))

    causal = CausalMarketView.from_dataset(dataset, 0, 60_000)
    assert causal.quotes() == ()

    policy = ExecutionTimingPolicy(
        fill_reference_policy="first_orderbook_after_decision",
        max_quote_wait_ms=2_000,
        allow_same_candle_close_fill=False,
    )
    signal = build_signal_event(
        candle=dataset.candles[0],
        interval=dataset.interval,
        side="BUY",
        policy=policy,
        feature_snapshot={},
        regime_snapshot={},
    )
    reference = resolve_execution_reference(
        dataset=dataset,
        signal=signal,
        signal_index=0,
        policy=policy,
    )

    assert reference.quote_ts == 60_500
    assert reference.quote_available_at_ts == 61_000
    assert reference.fill_reference_ts == 61_000
    assert reference.quote_age_ms == 1_000


def test_market_observation_time_cannot_precede_event_time() -> None:
    with pytest.raises(
        ValueError, match="orderbook_top_observation_time_precedes_event_time"
    ):
        TopOfBookQuote(
            ts=60_001,
            pair="KRW-BTC",
            bid_price=99.0,
            ask_price=101.0,
            spread_bps=200.0,
            source="fixture",
            observed_at_epoch_sec=60.0,
        )
    with pytest.raises(
        ValueError, match="orderbook_top_observation_time_precedes_event_time"
    ):
        build_orderbook_top_snapshot(
            ts=60_001,
            pair="KRW-BTC",
            bid_price=99.0,
            ask_price=101.0,
            source="fixture",
            observed_at_epoch_sec=60.0,
        )
    with pytest.raises(
        ValueError, match="orderbook_depth_observation_time_precedes_event_time"
    ):
        build_orderbook_depth_snapshot(
            ts=60_001,
            pair="KRW-BTC",
            bid_levels=((99.0, 1.0),),
            ask_levels=((101.0, 1.0),),
            source="fixture",
            observed_at_epoch_sec=60.0,
        )


def test_depth_selection_and_age_use_knowledge_time_not_event_time() -> None:
    depth = build_orderbook_depth_snapshot(
        ts=61_500,
        pair="KRW-BTC",
        bid_levels=((99.0, 1.0),),
        ask_levels=((101.0, 1.0),),
        source="fixture",
        observed_at_epoch_sec=62.0,
    )
    dataset = replace(_dataset(), orderbook_depth_snapshots=(depth,))

    assert (
        dataset.first_depth_snapshot_after_or_equal(
            target_ts=60_000,
            max_wait_ms=1_999,
        )
        is None
    )
    assert (
        dataset.first_depth_snapshot_after_or_equal(
            target_ts=60_000,
            max_wait_ms=2_000,
        )
        is depth
    )
    assert depth.ts == 61_500
    assert depth.available_at_ms() == 62_000


def test_feature_availability_is_fail_closed_at_exact_boundary() -> None:
    view = CausalMarketView.from_dataset(_dataset(), 0, 60_000)

    with pytest.raises(IndexError, match="future_feature"):
        view.feature("future", available_at=60_001)
    assert view.feature("available", available_at=60_000) == "available"


def test_depth_fill_applies_only_after_every_market_input_is_available() -> None:
    quote = TopOfBookQuote(
        ts=60_500,
        pair="KRW-BTC",
        bid_price=99.0,
        ask_price=101.0,
        spread_bps=200.0,
        source="fixture",
        observed_at_epoch_sec=61.0,
    )
    depth = build_orderbook_depth_snapshot(
        ts=61_500,
        pair="KRW-BTC",
        bid_levels=((99.0, 10.0),),
        ask_levels=((101.0, 10.0),),
        source="fixture",
        observed_at_epoch_sec=62.0,
    )
    dataset = replace(
        _dataset(),
        top_of_book_event_quotes=(quote,),
        orderbook_depth_snapshots=(depth,),
    )
    timing = ExecutionTimingPolicy(
        fill_reference_policy="first_orderbook_after_decision",
        max_quote_wait_ms=5_000,
        allow_same_candle_close_fill=False,
    )

    run = run_common_simulation_backtest(
        plugin=resolve_builtin_strategy("buy_and_hold_baseline"),
        dataset=dataset,
        parameter_values={"BUY_HOLD_BUY_INDEX": 0},
        fee_rate=0.0,
        slippage_bps=0.0,
        execution_model=DepthWalkExecutionModel(fee_rate=0.0),
        execution_timing_policy=timing,
        portfolio_policy=legacy_research_portfolio_policy(),
    )

    request = run.execution_requests[0]
    fill = run.fills[0]
    assert request.quote_ts == 60_500
    assert request.quote_available_at_ts == request.fill_reference_ts == 61_000
    assert request.depth_snapshot_ts == 61_500
    assert request.depth_snapshot_available_at_ts == 62_000
    assert fill.portfolio_effective_ts == 62_000


def test_validation_bound_quote_requires_observation_time_evidence() -> None:
    quote = TopOfBookQuote(
        ts=60_000,
        pair="KRW-BTC",
        bid_price=99.0,
        ask_price=101.0,
        spread_bps=200.0,
        source="fixture",
    )
    dataset = replace(_dataset(), top_of_book_event_quotes=(quote,))
    timing = ExecutionTimingPolicy(
        fill_reference_policy="first_orderbook_after_decision",
        max_quote_wait_ms=1_000,
        allow_same_candle_close_fill=False,
    )
    model = FixedBpsExecutionModel(fee_rate=0.0, slippage_bps=0.0)
    run = run_common_simulation_backtest(
        plugin=resolve_builtin_strategy("buy_and_hold_baseline"),
        dataset=dataset,
        parameter_values={"BUY_HOLD_BUY_INDEX": 0},
        fee_rate=0.0,
        slippage_bps=0.0,
        execution_model=model,
        execution_timing_policy=timing,
        portfolio_policy=legacy_research_portfolio_policy(),
    )

    assert run.execution_requests[0].quote_availability_basis == (
        "event_time_as_knowledge_time_assumption"
    )
    assert run.execution_event_summary["market_knowledge_time_assumption_count"] == 1
    assert (
        validate_execution_evidence(
            run=run, timing=timing, model=model, validation_bound=False
        )["status"]
        == "INSUFFICIENT_EVIDENCE"
    )
    with pytest.raises(
        ExecutionEvidenceError, match="event_time_as_knowledge_time_assumption"
    ):
        validate_execution_evidence(run=run, timing=timing, model=model)


def test_validation_bound_depth_requires_observation_time_evidence() -> None:
    quote = TopOfBookQuote(
        ts=60_000,
        pair="KRW-BTC",
        bid_price=99.0,
        ask_price=101.0,
        spread_bps=200.0,
        source="fixture",
        observed_at_epoch_sec=60.0,
    )
    depth = build_orderbook_depth_snapshot(
        ts=60_000,
        pair="KRW-BTC",
        bid_levels=((99.0, 10.0),),
        ask_levels=((101.0, 10.0),),
        source="fixture",
    )
    dataset = replace(
        _dataset(),
        top_of_book_event_quotes=(quote,),
        orderbook_depth_snapshots=(depth,),
    )
    timing = ExecutionTimingPolicy(
        fill_reference_policy="first_orderbook_after_decision",
        max_quote_wait_ms=1_000,
        allow_same_candle_close_fill=False,
    )
    model = DepthWalkExecutionModel(fee_rate=0.0)
    run = run_common_simulation_backtest(
        plugin=resolve_builtin_strategy("buy_and_hold_baseline"),
        dataset=dataset,
        parameter_values={"BUY_HOLD_BUY_INDEX": 0},
        fee_rate=0.0,
        slippage_bps=0.0,
        execution_model=model,
        execution_timing_policy=timing,
        portfolio_policy=legacy_research_portfolio_policy(),
    )

    assert run.execution_requests[0].depth_snapshot_availability_basis == (
        "event_time_as_knowledge_time_assumption"
    )
    with pytest.raises(
        ExecutionEvidenceError, match="event_time_as_knowledge_time_assumption"
    ):
        validate_execution_evidence(run=run, timing=timing, model=model)


def test_predecision_market_events_are_not_execution_references() -> None:
    stale_quote = TopOfBookQuote(
        ts=59_999,
        pair="KRW-BTC",
        bid_price=99.0,
        ask_price=101.0,
        spread_bps=200.0,
        source="stale",
        observed_at_epoch_sec=61.0,
    )
    fresh_quote = replace(stale_quote, ts=60_500, source="fresh")
    stale_depth = build_orderbook_depth_snapshot(
        ts=60_999,
        pair="KRW-BTC",
        bid_levels=((99.0, 1.0),),
        ask_levels=((101.0, 1.0),),
        source="stale",
        observed_at_epoch_sec=62.0,
    )
    fresh_depth = build_orderbook_depth_snapshot(
        ts=61_500,
        pair="KRW-BTC",
        bid_levels=((99.0, 1.0),),
        ask_levels=((101.0, 1.0),),
        source="fresh",
        observed_at_epoch_sec=62.0,
    )
    dataset = replace(
        _dataset(),
        top_of_book_event_quotes=(stale_quote, fresh_quote),
        orderbook_depth_snapshots=(stale_depth, fresh_depth),
    )

    assert (
        dataset.first_quote_after_or_equal(target_ts=60_000, max_wait_ms=2_000)
        is fresh_quote
    )
    assert (
        dataset.first_depth_snapshot_after_or_equal(target_ts=61_000, max_wait_ms=2_000)
        is fresh_depth
    )


def test_fixed_bps_ignores_unconsumed_optional_depth_evidence() -> None:
    quote = TopOfBookQuote(
        ts=60_000,
        pair="KRW-BTC",
        bid_price=99.0,
        ask_price=101.0,
        spread_bps=200.0,
        source="fixture",
        observed_at_epoch_sec=60.0,
    )
    irrelevant_depth = build_orderbook_depth_snapshot(
        ts=60_000,
        pair="KRW-BTC",
        bid_levels=((99.0, 1.0),),
        ask_levels=((101.0, 1.0),),
        source="unconsumed",
    )
    dataset = replace(
        _dataset(),
        top_of_book_event_quotes=(quote,),
        orderbook_depth_snapshots=(irrelevant_depth,),
    )
    timing = ExecutionTimingPolicy(
        fill_reference_policy="first_orderbook_after_decision",
        max_quote_wait_ms=1_000,
    )
    model = FixedBpsExecutionModel(fee_rate=0.0, slippage_bps=0.0)
    run = run_common_simulation_backtest(
        plugin=resolve_builtin_strategy("buy_and_hold_baseline"),
        dataset=dataset,
        parameter_values={"BUY_HOLD_BUY_INDEX": 0},
        fee_rate=0.0,
        slippage_bps=0.0,
        execution_model=model,
        execution_timing_policy=timing,
        portfolio_policy=legacy_research_portfolio_policy(),
    )

    assert run.execution_requests[0].depth_snapshot_ts is None
    assert run.execution_event_summary["market_knowledge_time_assumption_count"] == 0
    assert (
        validate_execution_evidence(run=run, timing=timing, model=model)["status"]
        == "PASS"
    )


def test_sql_depth_selector_matches_ceil_knowledge_time_boundary() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE orderbook_depth_levels (
            ts INTEGER NOT NULL,
            pair TEXT NOT NULL,
            side TEXT NOT NULL,
            level_index INTEGER NOT NULL,
            price REAL NOT NULL,
            size REAL NOT NULL,
            cumulative_size REAL NOT NULL,
            cumulative_notional REAL NOT NULL,
            source TEXT NOT NULL,
            observed_at_epoch_sec REAL,
            PRIMARY KEY (ts, pair, side, level_index, source)
        )
        """
    )
    snapshot = build_orderbook_depth_snapshot(
        ts=60_001,
        pair="KRW-BTC",
        bid_levels=((99.0, 1.0),),
        ask_levels=((101.0, 1.0),),
        source="fractional",
        observed_at_epoch_sec=60.0010000001,
    )
    upsert_orderbook_depth_snapshot(connection, snapshot)

    assert snapshot.available_at_ms() == 60_002
    assert (
        load_orderbook_depth_snapshot_after_or_equal(
            connection,
            pair="KRW-BTC",
            target_ts=60_001,
            max_wait_ms=0,
        )
        is None
    )
    loaded = load_orderbook_depth_snapshot_after_or_equal(
        connection,
        pair="KRW-BTC",
        target_ts=60_001,
        max_wait_ms=1,
    )
    assert loaded is not None and loaded.available_at_ms() == 60_002


def test_sql_depth_selector_does_not_mix_later_observation_group() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE orderbook_depth_levels (
            ts INTEGER NOT NULL,
            pair TEXT NOT NULL,
            side TEXT NOT NULL,
            level_index INTEGER NOT NULL,
            price REAL NOT NULL,
            size REAL NOT NULL,
            cumulative_size REAL NOT NULL,
            cumulative_notional REAL NOT NULL,
            source TEXT NOT NULL,
            observed_at_epoch_sec REAL,
            PRIMARY KEY (ts, pair, side, level_index, source)
        )
        """
    )
    connection.executemany(
        "INSERT INTO orderbook_depth_levels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (60_000, "KRW-BTC", "bid", 0, 99.0, 1.0, 1.0, 99.0, "fixture", 60.0),
            (60_000, "KRW-BTC", "ask", 0, 101.0, 1.0, 1.0, 101.0, "fixture", 60.0),
            (60_000, "KRW-BTC", "bid", 1, 98.0, 5.0, 6.0, 589.0, "fixture", 61.0),
            (60_000, "KRW-BTC", "ask", 1, 102.0, 5.0, 6.0, 611.0, "fixture", 61.0),
        ),
    )

    loaded = load_orderbook_depth_snapshot_after_or_equal(
        connection,
        pair="KRW-BTC",
        target_ts=60_000,
        max_wait_ms=2_000,
    )

    assert loaded is not None
    assert loaded.observed_at_epoch_sec == 60.0
    assert [(level.price, level.size) for level in loaded.bids] == [(99.0, 1.0)]
    assert [(level.price, level.size) for level in loaded.asks] == [(101.0, 1.0)]


def test_depth_loader_orders_mixed_observation_groups_and_keeps_distinct_refs(
    tmp_path,
) -> None:
    database = tmp_path / "mixed-depth-observations.sqlite"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE orderbook_depth_levels (
            ts INTEGER, pair TEXT, side TEXT, level_index INTEGER,
            price REAL, size REAL, cumulative_size REAL,
            cumulative_notional REAL, source TEXT,
            observed_at_epoch_sec REAL
        )
        """
    )
    connection.executemany(
        "INSERT INTO orderbook_depth_levels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (60_000, "KRW-BTC", "bid", 0, 99.0, 1.0, 1.0, 99.0, "fixture", None),
            (60_000, "KRW-BTC", "ask", 0, 101.0, 1.0, 1.0, 101.0, "fixture", None),
            (60_000, "KRW-BTC", "bid", 1, 98.0, 1.0, 1.0, 98.0, "fixture", 61.0),
            (60_000, "KRW-BTC", "ask", 1, 102.0, 1.0, 1.0, 102.0, "fixture", 61.0),
        ),
    )
    connection.commit()
    connection.close()

    snapshots = _load_orderbook_depth_event_snapshots(
        db_path=database,
        market="KRW-BTC",
        interval="1m",
        candles=_dataset().candles,
        source="fixture",
        execution_depth_lookahead_ms=2_000,
    )

    assert [item.observed_at_epoch_sec for item in snapshots] == [None, 61.0]
    assert len({item.depth_ref() for item in snapshots}) == 2


def test_streaming_top_of_book_gate_matches_missing_observation_policy(
    tmp_path,
) -> None:
    database = tmp_path / "knowledge-time.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE candles (ts INTEGER, pair TEXT, interval TEXT)")
    connection.execute(
        """
        CREATE TABLE orderbook_top_snapshots (
            ts INTEGER, pair TEXT, bid_price REAL, ask_price REAL,
            spread_bps REAL, source TEXT, observed_at_epoch_sec REAL
        )
        """
    )
    connection.execute("INSERT INTO candles VALUES (60000, 'KRW-BTC', '1m')")
    connection.execute(
        "INSERT INTO orderbook_top_snapshots VALUES "
        "(60000, 'KRW-BTC', 99, 101, 200, 'fixture', NULL)"
    )
    connection.commit()
    connection.close()
    spec = TopOfBookDatasetSpec(required=True, missing_policy="fail")
    manifest = SimpleNamespace(
        market="KRW-BTC",
        interval="1m",
        dataset=SimpleNamespace(top_of_book=spec),
    )

    payload = _top_of_book_split_sql(
        db_path=database,
        manifest=manifest,
        start_ts=60_000,
        end_ts=60_000,
        expected_signal_count=1,
    )

    assert payload["top_of_book_gate_status"] == "FAIL"
    assert payload["top_of_book_observation_time_missing_count"] == 1
    assert "top_of_book_observation_time_missing" in payload["top_of_book_gate_reasons"]


def test_streaming_top_of_book_gate_rejects_pre_event_observation_time(
    tmp_path,
) -> None:
    database = tmp_path / "invalid-knowledge-time.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE candles (ts INTEGER, pair TEXT, interval TEXT)")
    connection.execute(
        """
        CREATE TABLE orderbook_top_snapshots (
            ts INTEGER, pair TEXT, bid_price REAL, ask_price REAL,
            spread_bps REAL, source TEXT, observed_at_epoch_sec REAL
        )
        """
    )
    connection.execute("INSERT INTO candles VALUES (60000, 'KRW-BTC', '1m')")
    connection.execute(
        "INSERT INTO orderbook_top_snapshots VALUES "
        "(60000, 'KRW-BTC', 99, 101, 200, 'fixture', 59.0)"
    )
    connection.commit()
    connection.close()
    manifest = SimpleNamespace(
        market="KRW-BTC",
        interval="1m",
        dataset=SimpleNamespace(
            top_of_book=TopOfBookDatasetSpec(required=True, missing_policy="fail")
        ),
    )

    payload = _top_of_book_split_sql(
        db_path=database,
        manifest=manifest,
        start_ts=60_000,
        end_ts=60_000,
        expected_signal_count=1,
    )

    assert payload["top_of_book_gate_status"] == "FAIL"
    assert payload["top_of_book_observation_time_invalid_count"] == 1
    assert "top_of_book_observation_time_invalid" in payload["top_of_book_gate_reasons"]


def test_depth_observation_coverage_has_sql_materialized_snapshot_parity() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE orderbook_depth_levels (
            ts INTEGER, pair TEXT, side TEXT, level_index INTEGER,
            price REAL, size REAL, cumulative_size REAL,
            cumulative_notional REAL, source TEXT,
            observed_at_epoch_sec REAL
        )
        """
    )
    depth = build_orderbook_depth_snapshot(
        ts=60_000,
        pair="KRW-BTC",
        bid_levels=((99.0, 1.0),),
        ask_levels=((101.0, 1.0),),
        source="fixture",
        observed_at_epoch_sec=60.0,
    )
    upsert_orderbook_depth_snapshot(connection, depth)
    sql = summarize_orderbook_depth_evidence(connection, pair="KRW-BTC")
    materialized = _orderbook_depth_summary_from_snapshot(
        snapshot=replace(_dataset(), orderbook_depth_snapshots=(depth,))
    )

    assert (
        sql["l2_depth_observation_time_present_count"]
        == materialized["l2_depth_observation_time_present_count"]
        == 1
    )
    assert (
        sql["l2_depth_observation_time_missing_count"]
        == materialized["l2_depth_observation_time_missing_count"]
        == 0
    )


def test_depth_quality_summary_marks_pre_event_observation_invalid() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE orderbook_depth_levels (
            ts INTEGER, pair TEXT, side TEXT, level_index INTEGER,
            price REAL, size REAL, cumulative_size REAL,
            cumulative_notional REAL, source TEXT,
            observed_at_epoch_sec REAL
        )
        """
    )
    connection.executemany(
        "INSERT INTO orderbook_depth_levels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (60_000, "KRW-BTC", "bid", 0, 99.0, 1.0, 1.0, 99.0, "fixture", 59.0),
            (60_000, "KRW-BTC", "ask", 0, 101.0, 1.0, 1.0, 101.0, "fixture", 59.0),
        ),
    )

    summary = summarize_orderbook_depth_evidence(connection, pair="KRW-BTC")

    assert summary["l2_depth_observation_time_invalid_count"] == 1
    assert summary["l2_depth_knowledge_time_basis"] == "invalid_observation_time"
