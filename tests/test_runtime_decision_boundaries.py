from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from bithumb_bot.config import settings
from bithumb_bot.db_core import ensure_schema
from bithumb_bot.execution_service import validate_execution_submit_plan_payload
from bithumb_bot.research.backtest_kernel import run_decision_event_backtest
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.decision_event import ResearchDecisionEvent
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.strategy.sma import (
    build_sma_with_filter_decision_from_normalized_db,
    create_sma_with_filter_strategy,
)


class CountingConnection(sqlite3.Connection):
    commit_count: int

    def commit(self) -> None:
        self.commit_count = getattr(self, "commit_count", 0) + 1
        super().commit()


def _insert_candles(conn: sqlite3.Connection, *, pair: str, interval: str, base_ts: int) -> None:
    for idx in range(40):
        close = 10.0 + 0.2 * idx
        conn.execute(
            """
            INSERT OR REPLACE INTO candles(ts, pair, interval, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (base_ts + idx * 60_000, pair, interval, close, close, close, close, 1.0),
        )


def test_pure_sma_policy_has_no_runtime_imports_or_side_effect_dependencies() -> None:
    source = Path("src/bithumb_bot/core/sma_policy.py").read_text()
    tree = ast.parse(source)
    forbidden_modules = {
        "sqlite3",
        "time",
        "datetime",
        "bithumb_bot.config",
        "bithumb_bot.broker",
        "bithumb_bot.notifier",
        "bithumb_bot.db_core",
        "bithumb_bot.runtime_state",
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not forbidden_modules.intersection(imported)
    assert ".commit(" not in source
    assert ".execute(" not in source


def test_normalized_db_decision_path_does_not_commit() -> None:
    conn = sqlite3.connect(":memory:", factory=CountingConnection)
    conn.row_factory = sqlite3.Row
    try:
        ensure_schema(conn)
        _insert_candles(conn, pair=settings.PAIR, interval=settings.INTERVAL, base_ts=1_700_001_000_000)
        conn.commit()
        conn.commit_count = 0

        strategy = create_sma_with_filter_strategy(
            short_n=2,
            long_n=3,
            pair=settings.PAIR,
            interval=settings.INTERVAL,
        )
        decision = build_sma_with_filter_decision_from_normalized_db(
            conn,
            strategy,
            through_ts_ms=1_700_001_000_000 + 39 * 60_000,
        )
    finally:
        conn.close()

    assert decision is not None
    assert conn.commit_count == 0


def test_execution_submit_plan_contract_detects_missing_or_inconsistent_fields() -> None:
    valid_plan = {
        "side": "BUY",
        "source": "strategy_position",
        "authority": "configured_strategy_order_size",
        "final_action": "ENTER_STRATEGY_POSITION",
        "qty": 0.001,
        "notional_krw": 100_000.0,
        "target_exposure_krw": 100_000.0,
        "current_effective_exposure_krw": 0.0,
        "delta_krw": 100_000.0,
        "submit_expected": True,
        "pre_submit_proof_status": "not_required",
        "block_reason": "none",
        "idempotency_key": None,
    }

    validate_execution_submit_plan_payload(valid_plan, field_name="buy_submit_plan")

    missing = dict(valid_plan)
    missing.pop("final_action")
    with pytest.raises(ValueError, match="buy_submit_plan_schema_missing_fields:final_action"):
        validate_execution_submit_plan_payload(missing, field_name="buy_submit_plan")

    inconsistent = dict(valid_plan)
    inconsistent["pre_submit_proof_status"] = "failed"
    with pytest.raises(ValueError, match="buy_submit_plan_schema_submit_expected_with_failed_proof"):
        validate_execution_submit_plan_payload(inconsistent, field_name="buy_submit_plan")


def test_research_kernel_marks_missing_sma_policy_metadata_non_comparable() -> None:
    base_ts = 1_700_002_000_000
    dataset = DatasetSnapshot(
        snapshot_id="unit",
        source="unit",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=DateRange(start="2024-01-01", end="2024-01-02"),
        candles=tuple(
            Candle(
                ts=base_ts + idx * 60_000,
                open=10.0 + idx,
                high=10.0 + idx,
                low=10.0 + idx,
                close=10.0 + idx,
                volume=1.0,
            )
            for idx in range(3)
        ),
    )
    event = ResearchDecisionEvent(
        candle_ts=base_ts + 60_000,
        decision_ts=base_ts + 61_000,
        strategy_name="sma_with_filter",
        strategy_version="unit",
        raw_signal="BUY",
        final_signal="BUY",
        reason="legacy final signal must not be authoritative",
        feature_snapshot={},
        strategy_diagnostics={},
        entry_signal="BUY",
        extra_payload={},
    )

    run = run_decision_event_backtest(
        dataset=dataset,
        strategy_name="sma_with_filter",
        parameter_values={
            "SMA_SHORT": 1,
            "SMA_LONG": 2,
            "SMA_FILTER_VOL_WINDOW": 1,
            "SMA_FILTER_OVEREXT_LOOKBACK": 1,
            "BUY_FRACTION": 1.0,
            "MAX_ORDER_KRW": 100_000.0,
        },
        fee_rate=0.0,
        slippage_bps=0.0,
        decision_events=(event,),
    )

    assert run.decisions
    decision = run.decisions[0]
    assert decision["final_signal"] == "HOLD"
    assert decision["research_policy_unsupported"] is True
    assert decision["research_policy_comparable"] is False
    assert decision["research_policy_unsupported_reason"] == (
        "sma_with_filter_policy_decision_missing_not_comparable"
    )
