from __future__ import annotations

import pytest

from bithumb_bot.db_core import ensure_db
from bithumb_bot.runtime_scope import derive_risk_scope_id, require_risk_scope_reset_authority, strategy_revision_id
from bithumb_bot.risk_contract import RiskPolicy
from bithumb_bot.risk_policy_engine import RiskPolicyEngine
from bithumb_bot.strategy_risk_state import StrategyRiskStateProvider


def _payload(**overrides: object) -> dict[str, object]:
    payload = {
        "strategy_name": "daily_participation_sma",
        "strategy_instance_id": "old",
        "pair": "KRW-BTC",
        "interval": "1m",
        "runtime_contract_hash": "sha256:" + "1" * 64,
        "approved_profile_hash": "sha256:" + "2" * 64,
        "strategy_parameters_hash": "sha256:" + "3" * 64,
        "risk_policy_hash": "sha256:" + "4" * 64,
        "risk_capital_basis": "fixed_observation_notional",
        "risk_capital_krw": 100_000,
    }
    payload.update(overrides)
    return payload


def test_non_economic_runtime_contract_change_preserves_risk_scope_id() -> None:
    old = _payload(strategy_instance_id="64fb", runtime_contract_hash="sha256:" + "1" * 64)
    new = _payload(strategy_instance_id="cabccc", runtime_contract_hash="sha256:" + "9" * 64)

    assert strategy_revision_id(old) != strategy_revision_id(new)
    assert derive_risk_scope_id(old) == derive_risk_scope_id(new)


def test_risk_scope_reset_requires_explicit_authority() -> None:
    with pytest.raises(ValueError, match="risk_scope_reset_authority_required"):
        require_risk_scope_reset_authority(
            previous=_payload(risk_capital_krw=100_000),
            current=_payload(risk_capital_krw=200_000),
        )


def test_strategy_revision_change_does_not_drop_lifecycle_history() -> None:
    old = _payload(strategy_instance_id="64fb")
    new = _payload(strategy_instance_id="cabccc")

    assert derive_risk_scope_id(old) == derive_risk_scope_id(new)


def _seed_loss_lifecycle(conn, *, old_instance_id: str, risk_scope_id: str) -> None:
    decision_id = conn.execute(
        """
        INSERT INTO strategy_decisions(decision_ts, strategy_name, signal, reason, candle_ts, market_price, context_json)
        VALUES (?, 'daily_participation_sma', 'BUY', 'unit', ?, 100.0, ?)
        """,
        (
            1_800_000_000_000,
            1_800_000_000_000,
            (
                '{"strategy_name":"daily_participation_sma",'
                f'"strategy_instance_id":"{old_instance_id}",'
                '"pair":"KRW-BTC","interval":"1m",'
                f'"risk_scope_id":"{risk_scope_id}"'
                "}"
            ),
        ),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO trade_lifecycles(
            pair, entry_trade_id, exit_trade_id, entry_client_order_id, exit_client_order_id,
            entry_ts, exit_ts, matched_qty, entry_price, exit_price, gross_pnl, fee_total,
            net_pnl, holding_time_sec, strategy_name, strategy_instance_id,
            owner_strategy_instance_id, owner_risk_scope_id, risk_scope_id, entry_decision_id
        ) VALUES ('KRW-BTC', 1, 2, 'entry', 'exit', ?, ?, 1, 100, 90, -10, 0, -10, 60,
            'daily_participation_sma', ?, ?, ?, ?, ?)
        """,
        (
            1_800_000_000_000,
            1_800_000_060_000,
            old_instance_id,
            old_instance_id,
            risk_scope_id,
            risk_scope_id,
            decision_id,
        ),
    )
    conn.commit()


def test_loss_today_uses_risk_scope_id_not_strategy_instance_id(tmp_path) -> None:
    conn = ensure_db(str(tmp_path / "loss-scope.sqlite"))
    _seed_loss_lifecycle(conn, old_instance_id="old-instance", risk_scope_id="scope-a")

    snapshot = StrategyRiskStateProvider(conn).snapshot(
        strategy_instance_id="new-instance",
        strategy_name="daily_participation_sma",
        pair="KRW-BTC",
        interval="1m",
        as_of_ts_ms=1_800_000_120_000,
        mark_price=100.0,
        policy=RiskPolicy(max_daily_loss_krw=1.0, source="unit"),
        enforced=True,
        risk_scope_id="scope-a",
    )

    assert snapshot.loss_today == pytest.approx(10.0)
    assert snapshot.evidence["state_derivation"]["loss_today"]["scope"] == "risk_scope"


def test_cooldown_uses_risk_scope_id_not_strategy_instance_id(tmp_path) -> None:
    conn = ensure_db(str(tmp_path / "cooldown-scope.sqlite"))
    _seed_loss_lifecycle(conn, old_instance_id="old-instance", risk_scope_id="scope-a")
    policy = RiskPolicy(cooldown_after_loss_min=15, source="unit")

    snapshot = StrategyRiskStateProvider(conn).snapshot(
        strategy_instance_id="new-instance",
        strategy_name="daily_participation_sma",
        pair="KRW-BTC",
        interval="1m",
        as_of_ts_ms=1_800_000_120_000,
        mark_price=100.0,
        policy=policy,
        enforced=True,
        risk_scope_id="scope-a",
    )
    decision = RiskPolicyEngine(policy).evaluate_pre_decision(snapshot)

    assert snapshot.minutes_since_last_loss == pytest.approx(1.0)
    assert snapshot.evidence["state_derivation"]["minutes_since_last_loss"]["scope"] == "risk_scope"
    assert decision.reason_code == "COOLDOWN_AFTER_LOSS"
