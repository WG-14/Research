from __future__ import annotations

import sqlite3

from bithumb_bot.db_core import ensure_schema
from bithumb_bot.oms import create_order
from bithumb_bot.runtime.daily_participation_claims import (
    DailyParticipationClaimKey,
    claim_rows,
    pending_daily_participation_claim_count,
    reconstruct_daily_participation_claims_from_orders,
)
from bithumb_bot.runtime.daily_participation_count_provider import build_runtime_daily_count_snapshot_from_sqlite
from bithumb_bot.strategy.daily_participation_policy import (
    DailyParticipationPolicyConfig,
    evaluate_daily_participation_policy,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _config(policy_hash_variant: str = "", *, retry_terminal_failed_claims: bool = False) -> DailyParticipationPolicyConfig:
    return DailyParticipationPolicyConfig(
        enabled=True,
        timezone="Asia/Seoul",
        count_basis="filled",
        window_start_hour=0,
        window_end_hour=24,
        buy_fraction=0.05,
        max_order_krw=10000.0 + len(policy_hash_variant),
        retry_terminal_failed_claims=retry_terminal_failed_claims,
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


def test_pending_claim_blocks_second_fallback_intent() -> None:
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
    result = evaluate_daily_participation_policy(config=_config(), state=state)
    assert result.allowed is False
    assert result.reason_code == "daily_participation_pending_claim_exists"


def test_claim_key_is_strategy_instance_scoped() -> None:
    conn = _conn()
    key = DailyParticipationClaimKey("daily:a", "KRW-BTC", "2023-12-31", "sha256:policy-a")
    _create_claim_order(conn, instance="daily:b", policy_hash="sha256:policy-a")
    _create_claim_order(conn, instance="daily:a", policy_hash="sha256:policy-b")

    assert pending_daily_participation_claim_count(conn, key=key) == 0


def test_filled_claim_marks_day_fulfilled() -> None:
    conn = _conn()
    _create_claim_order(conn, policy_hash="sha256:policy")
    conn.execute("UPDATE orders SET status='FILLED' WHERE client_order_id=?", ("daily:a:order:policy",))

    reconstruct_daily_participation_claims_from_orders(conn, now_ms=1_704_031_260_000)

    rows = claim_rows(conn)
    assert rows[0]["status"] == "fulfilled"
    assert pending_daily_participation_claim_count(
        conn,
        key=DailyParticipationClaimKey("daily:a", "KRW-BTC", "2023-12-31", "sha256:policy"),
    ) == 0


def test_terminal_failed_claim_allows_retry_only_when_policy_allows() -> None:
    conn = _conn()
    blocked_config = _config()
    allowed_config = _config(retry_terminal_failed_claims=True)
    policy_hash = blocked_config.policy_hash()
    allowed_policy_hash = allowed_config.policy_hash()
    _create_claim_order(conn, policy_hash=policy_hash)
    _create_claim_order(conn, policy_hash=allowed_policy_hash)
    conn.execute("UPDATE orders SET status='FAILED' WHERE daily_participation_policy_hash IN (?, ?)", (policy_hash, allowed_policy_hash))

    blocked_snapshot = build_runtime_daily_count_snapshot_from_sqlite(
        conn=conn,
        config=blocked_config,
        decision_ts=1_704_031_200_000,
        pair="KRW-BTC",
        strategy_instance_id="daily:a",
    )
    allowed_snapshot = build_runtime_daily_count_snapshot_from_sqlite(
        conn=conn,
        config=allowed_config,
        decision_ts=1_704_031_200_000,
        pair="KRW-BTC",
        strategy_instance_id="daily:a",
    )

    assert blocked_snapshot.pending_claim_count == 1
    assert allowed_snapshot.pending_claim_count == 0


def test_partial_fill_blocks_duplicate_buy() -> None:
    conn = _conn()
    policy_hash = _config().policy_hash()
    _create_claim_order(conn, policy_hash=policy_hash)
    conn.execute("UPDATE orders SET status='PARTIAL', qty_filled=0.25 WHERE strategy_instance_id='daily:a'")

    snapshot = build_runtime_daily_count_snapshot_from_sqlite(
        conn=conn,
        config=_config(),
        decision_ts=1_704_031_200_000,
        pair="KRW-BTC",
        strategy_instance_id="daily:a",
    )

    assert claim_rows(conn)[0]["status"] == "partially_filled"
    assert snapshot.pending_claim_count == 1


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
