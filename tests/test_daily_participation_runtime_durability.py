from __future__ import annotations

from pathlib import Path

from bithumb_bot.db_core import ensure_db
from bithumb_bot.oms import add_fill, create_order
from bithumb_bot.runtime.daily_participation_claims import (
    DailyParticipationClaimKey,
    pending_daily_participation_claim_count,
    reconstruct_daily_participation_claims_from_orders,
    upsert_daily_participation_claim,
)
from bithumb_bot.runtime.daily_participation_count_provider import build_runtime_daily_count_snapshot_from_sqlite
from bithumb_bot.strategy.base import PositionContext
from bithumb_bot.strategy.daily_participation_policy import DailyParticipationPolicyConfig
from bithumb_bot.strategy.exit_rules import MaxHoldingTimeExitRule


PAIR = "KRW-BTC"
INSTANCE = "daily_participation_sma:KRW-BTC:1m"
POLICY_HASH = "sha256:policy"
KST_DAY = "2026-06-19"
DAY_TS = 1_781_792_400_000


def _policy() -> DailyParticipationPolicyConfig:
    return DailyParticipationPolicyConfig(
        enabled=True,
        timezone="Asia/Seoul",
        count_basis="filled",
        window_start_hour=9,
        window_end_hour=11,
        buy_fraction=0.05,
        max_order_krw=50_000,
        fallback_mode="unconditional_participation",
    )


def _db(tmp_path: Path):
    return ensure_db(str(tmp_path / "runtime.sqlite"))


def _buy_order(conn, client_order_id: str, *, status: str = "FILLED", strategy_instance_id: str = INSTANCE) -> None:
    create_order(
        conn=conn,
        client_order_id=client_order_id,
        symbol=PAIR,
        side="BUY",
        qty_req=0.001,
        price=50_000_000,
        strategy_name="daily_participation_sma",
        strategy_instance_id=strategy_instance_id,
        status=status,
        daily_participation_policy_hash=POLICY_HASH,
        daily_participation_kst_day=KST_DAY,
        ts_ms=DAY_TS,
    )


def test_daily_count_filled_basis_survives_restart(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _buy_order(conn, "b1")
    add_fill(conn=conn, client_order_id="b1", fill_id="f1", fill_ts=DAY_TS, price=50_000_000, qty=0.001)
    conn.commit()
    conn.close()

    restarted = ensure_db(str(tmp_path / "runtime.sqlite"))
    snapshot = build_runtime_daily_count_snapshot_from_sqlite(
        conn=restarted,
        config=_policy(),
        decision_ts=DAY_TS + 60_000,
        pair=PAIR,
        strategy_instance_id=INSTANCE,
    )

    assert snapshot.count_for_kst_day == 1


def test_pending_claim_blocks_second_buy_same_kst_day(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    key = DailyParticipationClaimKey(INSTANCE, PAIR, KST_DAY, _policy().policy_hash())
    upsert_daily_participation_claim(conn, key=key, status="claim_pending", ts_ms=DAY_TS)
    conn.commit()
    conn.close()

    restarted = ensure_db(str(tmp_path / "runtime.sqlite"))
    assert pending_daily_participation_claim_count(restarted, key=key) == 1


def test_terminal_failed_claim_blocks_retry_unless_policy_allows(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    key = DailyParticipationClaimKey(INSTANCE, PAIR, KST_DAY, _policy().policy_hash())
    upsert_daily_participation_claim(conn, key=key, status="terminal_failed", ts_ms=DAY_TS)

    assert pending_daily_participation_claim_count(conn, key=key) == 1
    assert pending_daily_participation_claim_count(conn, key=key, retry_terminal_failed_claims=True) == 0


def test_partial_fill_does_not_count_as_fulfilled_by_default(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _buy_order(conn, "partial1", status="PARTIAL")
    add_fill(conn=conn, client_order_id="partial1", fill_id="pf1", fill_ts=DAY_TS, price=50_000_000, qty=0.0005)

    snapshot = build_runtime_daily_count_snapshot_from_sqlite(
        conn=conn,
        config=_policy(),
        decision_ts=DAY_TS + 60_000,
        pair=PAIR,
        strategy_instance_id=INSTANCE,
    )

    assert snapshot.count_for_kst_day == 0


def test_max_holding_exit_triggers_after_74_minutes() -> None:
    decision = MaxHoldingTimeExitRule(max_holding_sec=74 * 60).evaluate(
        position=PositionContext(in_position=True, entry_price=100.0, holding_time_sec=74 * 60, unrealized_pnl_ratio=0.0),
        candle_ts=DAY_TS,
        market_price=100.0,
        signal_context={},
    )

    assert decision.should_exit is True


def test_max_holding_exit_not_triggered_before_74_minutes() -> None:
    decision = MaxHoldingTimeExitRule(max_holding_sec=74 * 60).evaluate(
        position=PositionContext(in_position=True, entry_price=100.0, holding_time_sec=(74 * 60) - 1, unrealized_pnl_ratio=0.0),
        candle_ts=DAY_TS,
        market_price=100.0,
        signal_context={},
    )

    assert decision.should_exit is False
