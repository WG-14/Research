from __future__ import annotations

import inspect
from dataclasses import replace
from pathlib import Path

import pytest

from bithumb_bot import operator_commands
from bithumb_bot.approved_profile import ApprovedProfileError, validate_approved_profile
from bithumb_bot.operator_smoke import (
    OPERATOR_SMOKE_STRATEGY_NAME,
    SMOKE_BUY_CONFIRMATION_TOKEN,
    OperatorSmokeError,
    build_smoke_buy_plan,
    validate_smoke_buy_request,
)
from bithumb_bot.operator_smoke_authority import (
    build_operator_smoke_authority_payload,
)
from bithumb_bot.runtime.daily_participation_claims import (
    DailyParticipationClaimKey,
    ensure_daily_participation_claims_schema,
    pending_daily_participation_claim_count,
)


def test_smoke_buy_requires_live_mode() -> None:
    with pytest.raises(OperatorSmokeError, match="smoke_buy_requires_live_mode"):
        validate_smoke_buy_request(
            mode="paper",
            live_real_order_armed=True,
            kill_switch=False,
            krw=50_000,
            confirm=SMOKE_BUY_CONFIRMATION_TOKEN,
        )


def test_smoke_buy_requires_real_order_armed() -> None:
    with pytest.raises(OperatorSmokeError, match="smoke_buy_requires_live_real_order_armed"):
        validate_smoke_buy_request(
            mode="live",
            live_real_order_armed=False,
            kill_switch=False,
            krw=50_000,
            confirm=SMOKE_BUY_CONFIRMATION_TOKEN,
        )


def test_smoke_buy_requires_confirmation_token() -> None:
    with pytest.raises(OperatorSmokeError, match="smoke_buy_requires_confirmation_token"):
        validate_smoke_buy_request(
            mode="live",
            live_real_order_armed=True,
            kill_switch=False,
            krw=50_000,
            confirm="",
        )


def test_smoke_buy_caps_krw_at_50000() -> None:
    with pytest.raises(OperatorSmokeError, match="smoke_buy_krw_above_50000_cap"):
        validate_smoke_buy_request(
            mode="live",
            live_real_order_armed=True,
            kill_switch=False,
            krw=50_001,
            confirm=SMOKE_BUY_CONFIRMATION_TOKEN,
        )


def test_smoke_buy_uses_operator_execution_smoke_identity() -> None:
    plan = build_smoke_buy_plan(market="KRW-BTC", krw=50_000, run_id="run123")

    assert plan.strategy_name == "operator_execution_smoke"
    assert plan.strategy_instance_id == "operator_execution_smoke:run123"
    assert plan.origin == "operator_smoke"


def test_smoke_buy_does_not_satisfy_approved_profile_required() -> None:
    payload = build_operator_smoke_authority_payload(
        expires_at=__import__("datetime").datetime(2099, 1, 1, tzinfo=__import__("datetime").timezone.utc)
    )

    with pytest.raises(ApprovedProfileError):
        validate_approved_profile(payload)


def test_smoke_buy_not_counted_as_daily_participation_event(tmp_path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(tmp_path / "smoke.sqlite")
    conn.row_factory = sqlite3.Row
    ensure_daily_participation_claims_schema(conn)
    conn.execute(
        """
        INSERT INTO daily_participation_claims(
            strategy_instance_id, pair, kst_day, participation_policy_hash,
            status, retry_allowed, created_ts, updated_ts
        )
        VALUES (?, ?, ?, ?, ?, 0, 1, 1)
        """,
        (f"{OPERATOR_SMOKE_STRATEGY_NAME}:run123", "KRW-BTC", "2026-06-19", "sha256:policy", "submitted"),
    )
    conn.commit()

    count = pending_daily_participation_claim_count(
        conn,
        key=DailyParticipationClaimKey(
            strategy_instance_id="daily_participation_sma:KRW-BTC:1m",
            pair="KRW-BTC",
            kst_day="2026-06-19",
            participation_policy_hash="sha256:policy",
        ),
    )

    assert count == 0


def test_smoke_buy_cli_handler_does_not_call_broker_create_order_directly() -> None:
    source = inspect.getsource(operator_commands.cmd_smoke_buy)

    assert "create_order" not in source
