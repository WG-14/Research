from __future__ import annotations

import sqlite3

from bithumb_bot.db_core import ensure_schema
from bithumb_bot.oms import create_order
from bithumb_bot.runtime.daily_participation_claims import (
    DailyParticipationClaimKey,
    pending_daily_participation_claim_count,
    reconstruct_daily_participation_claims_from_orders,
)
from bithumb_bot.runtime.daily_participation_count_provider import build_runtime_daily_count_snapshot_from_sqlite
from bithumb_bot.strategy.daily_participation_policy import DailyParticipationPolicyConfig


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _config(policy_hash_variant: str = "") -> DailyParticipationPolicyConfig:
    return DailyParticipationPolicyConfig(
        enabled=True,
        timezone="Asia/Seoul",
        count_basis="filled",
        window_start_hour=0,
        window_end_hour=24,
        buy_fraction=0.05,
        max_order_krw=10000.0 + len(policy_hash_variant),
    )


def _create_claim_order(conn: sqlite3.Connection, *, instance: str = "daily:a", policy_hash: str = "sha256:policy") -> None:
    create_order(
        client_order_id=f"{instance}:order:{policy_hash[-6:]}",
        symbol="KRW-BTC",
        side="BUY",
        qty_req=1.0,
        price=100.0,
        strategy_name="daily_participation_sma",
        strategy_instance_id=instance,
        daily_participation_policy_hash=policy_hash,
        daily_count_snapshot_hash="sha256:count",
        participation_decision_hash="sha256:decision",
        daily_participation_kst_day="2023-12-31",
        daily_participation_fallback_mode="unconditional_participation",
        status="NEW",
        ts_ms=1_704_031_200_000,
        conn=conn,
    )


def test_pending_claim_blocks_second_fallback_buy_before_fill() -> None:
    conn = _conn()
    policy_hash = _config().policy_hash()
    _create_claim_order(conn, policy_hash=policy_hash)

    snapshot = build_runtime_daily_count_snapshot_from_sqlite(
        conn=conn,
        config=_config(),
        decision_ts=1_704_031_200_000,
        pair="KRW-BTC",
        strategy_instance_id="daily:a",
    )
    state = snapshot.state_snapshot(decision_ts=1_704_031_200_000, position_open=False, entry_allowed=True)

    assert state.pending_claim_count == 1


def test_pending_claim_key_includes_strategy_instance_pair_day_policy_hash() -> None:
    conn = _conn()
    key = DailyParticipationClaimKey("daily:a", "KRW-BTC", "2023-12-31", "sha256:policy-a")
    _create_claim_order(conn, instance="daily:b", policy_hash="sha256:policy-a")
    _create_claim_order(conn, instance="daily:a", policy_hash="sha256:policy-b")

    assert pending_daily_participation_claim_count(conn, key=key) == 0


def test_restart_reconstructs_pending_claim_from_orders_or_claim_table() -> None:
    conn = _conn()
    _create_claim_order(conn, policy_hash="sha256:policy")
    conn.execute("DELETE FROM daily_participation_claims")

    reconstructed = reconstruct_daily_participation_claims_from_orders(conn, now_ms=1_704_031_260_000)

    assert reconstructed == 1
    assert pending_daily_participation_claim_count(
        conn,
        key=DailyParticipationClaimKey("daily:a", "KRW-BTC", "2023-12-31", "sha256:policy"),
    ) == 1
