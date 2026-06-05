from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .config import settings
from .oms import collect_risky_order_state
from .risk import (
    _count_orders_today,
    _latest_position_entry_price,
    daily_loss_reason_code_from_reason,
    evaluate_daily_loss_state,
)
from .risk_contract import RiskDecision, RiskPolicy, RiskSnapshot, SubmitPlan
from .risk_policy_engine import RiskPolicyEngine


def settings_risk_policy() -> RiskPolicy:
    return RiskPolicy(
        schema_version=1,
        max_daily_loss_krw=float(settings.MAX_DAILY_LOSS_KRW),
        max_position_loss_pct=float(settings.MAX_POSITION_LOSS_PCT),
        max_daily_order_count=int(settings.MAX_DAILY_ORDER_COUNT),
        kill_switch=bool(settings.KILL_SWITCH),
        max_open_positions=int(settings.MAX_OPEN_POSITIONS),
        unresolved_order_policy="block",
        policy_status="enabled",
        source="runtime_settings",
    )


@dataclass(frozen=True)
class RuntimeRiskEngineAdapter:
    conn: sqlite3.Connection
    policy: RiskPolicy | None = None

    def evaluate_buy_intent(
        self,
        *,
        ts_ms: int,
        cash: float,
        qty: float,
        price: float,
        broker: object | None = None,
        mark_price_source: str = "market_price",
        evaluation_origin: str = "buy_guardrails",
    ) -> RiskDecision:
        policy = self.policy or settings_risk_policy()
        snapshot = self._snapshot(
            ts_ms=ts_ms,
            now_ms=ts_ms,
            cash=cash,
            qty=qty,
            price=price,
            broker=broker,
            mark_price_source=mark_price_source,
            evaluation_origin=evaluation_origin,
            include_unresolved_order_gate=False,
            duplicate_entry=float(qty) > 1e-12,
        )
        decision = RiskPolicyEngine(policy).evaluate_pre_decision(snapshot)
        _record_typed_decision_identity(
            self.conn,
            decision=decision,
            evaluation_ts_ms=int(ts_ms),
            evaluation_origin=evaluation_origin,
        )
        return decision

    def evaluate_pre_submit(
        self,
        *,
        plan: SubmitPlan,
        ts_ms: int,
        now_ms: int,
        cash: float,
        qty: float,
        price: float,
        broker: object | None = None,
        mark_price_source: str = "market_price",
        evaluation_origin: str = "submission_halt",
    ) -> RiskDecision:
        policy = self.policy or settings_risk_policy()
        snapshot = self._snapshot(
            ts_ms=ts_ms,
            now_ms=now_ms,
            cash=cash,
            qty=qty,
            price=price,
            broker=broker,
            mark_price_source=mark_price_source,
            evaluation_origin=evaluation_origin,
            include_unresolved_order_gate=True,
            duplicate_entry=False,
        )
        decision = RiskPolicyEngine(policy).evaluate_pre_submit(plan, snapshot)
        _record_typed_decision_identity(
            self.conn,
            decision=decision,
            evaluation_ts_ms=int(ts_ms),
            evaluation_origin=evaluation_origin,
        )
        return decision

    def _snapshot(
        self,
        *,
        ts_ms: int,
        now_ms: int,
        cash: float,
        qty: float,
        price: float,
        broker: object | None,
        mark_price_source: str,
        evaluation_origin: str,
        include_unresolved_order_gate: bool,
        duplicate_entry: bool,
    ) -> RiskSnapshot:
        del cash
        daily = evaluate_daily_loss_state(
            self.conn,
            ts_ms=int(ts_ms),
            price=float(price),
            broker=broker,
            mark_price_source=mark_price_source,
            evaluation_origin=evaluation_origin,
        )
        mismatch = daily.reason_code == "RISK_STATE_MISMATCH" or daily_loss_reason_code_from_reason(
            daily.reason
        ) == "RISK_STATE_MISMATCH"
        unresolved_blocked = False
        unresolved_reason_code = "OK"
        unresolved_reason = "ok"
        unresolved_state: dict[str, object] = {}
        if include_unresolved_order_gate:
            unresolved_state = dict(
                collect_risky_order_state(
                    self.conn,
                    now_ms=int(now_ms),
                    max_open_order_age_sec=int(settings.MAX_OPEN_ORDER_AGE_SEC),
                )
            )
            unresolved_blocked, unresolved_reason_code, unresolved_reason = _classify_unresolved_state(
                unresolved_state,
                max_open_order_age_sec=int(settings.MAX_OPEN_ORDER_AGE_SEC),
            )
        return RiskSnapshot(
            evaluation_ts_ms=int(ts_ms),
            mark_price=float(price),
            current_equity=daily.current_equity,
            baseline_equity=daily.start_equity,
            loss_today=daily.loss_today,
            current_cash_krw=daily.current_cash_krw,
            current_asset_qty=float(qty),
            position_entry_price=_latest_position_entry_price(self.conn),
            broker_local_mismatch=bool(mismatch),
            recovery_risk_mismatch_reason=daily.reason if mismatch else None,
            duplicate_entry=bool(duplicate_entry),
            daily_order_count=_count_orders_today(self.conn, int(ts_ms)),
            unresolved_order_blocked=bool(unresolved_blocked),
            unresolved_order_reason_code=str(unresolved_reason_code),
            unresolved_order_reason=str(unresolved_reason),
            state_source="runtime_db_broker",
            evidence={
                "daily_loss_evaluation": {
                    "reason_code": daily.reason_code,
                    "decision": daily.decision,
                    "day_kst": daily.day_kst,
                    "mark_price_source": daily.mark_price_source,
                },
                "unresolved_order_gate": {
                    "blocked": bool(unresolved_blocked),
                    "reason_code": str(unresolved_reason_code),
                    "reason": str(unresolved_reason),
                    "state": unresolved_state,
                    "evaluated_once": bool(include_unresolved_order_gate),
                },
            },
        )


def _classify_unresolved_state(
    state: dict[str, Any],
    *,
    max_open_order_age_sec: int,
) -> tuple[bool, str, str]:
    if int(state.get("submit_unknown_count") or 0) > 0:
        return True, "SUBMIT_UNKNOWN_PRESENT", "submit-unknown unresolved order exists"
    if int(state.get("accounting_pending_count") or 0) > 0:
        return True, "ACCOUNTING_PENDING_PRESENT", "accounting-pending order exists"
    if int(state.get("recovery_required_count") or 0) > 0:
        return True, "RECOVERY_REQUIRED_PRESENT", "recovery-required order exists"
    open_count = int(state.get("unresolved_open_order_count") or 0)
    if open_count <= 0:
        return False, "OK", "ok"
    age_sec = float(state.get("oldest_unresolved_open_order_age_sec") or 0.0)
    max_age_sec = max(1, int(max_open_order_age_sec))
    if age_sec > max_age_sec:
        return (
            True,
            "STALE_UNRESOLVED_OPEN_ORDER",
            f"stale unresolved open order exists: age={age_sec:.1f}s > {max_age_sec}s",
        )
    return True, "UNRESOLVED_OPEN_ORDER_PRESENT", "unresolved open order exists"


def _record_typed_decision_identity(
    conn: sqlite3.Connection,
    *,
    decision: RiskDecision,
    evaluation_ts_ms: int,
    evaluation_origin: str,
) -> None:
    _ensure_typed_risk_columns(conn)
    had_tx = conn.in_transaction
    conn.execute(
        """
        UPDATE risk_evaluations
        SET
            risk_input_hash=?,
            risk_policy_hash=?,
            risk_evidence_hash=?,
            risk_decision_hash=?,
            risk_reason_code=?,
            risk_status=?,
            risk_evaluation_point=?,
            risk_state_source=?,
            effective_risk_limits_json=?
        WHERE id = (
            SELECT id
            FROM risk_evaluations
            WHERE evaluation_ts_ms=? AND evaluation_origin=?
            ORDER BY id DESC
            LIMIT 1
        )
        """,
        (
            decision.risk_input_hash,
            decision.risk_policy_hash,
            decision.risk_evidence_hash,
            decision.risk_decision_hash,
            decision.reason_code,
            decision.status,
            decision.evaluation_point,
            decision.state_source,
            json.dumps(decision.effective_limits, ensure_ascii=False, sort_keys=True),
            int(evaluation_ts_ms),
            str(evaluation_origin),
        ),
    )
    if not had_tx:
        conn.commit()


def _ensure_typed_risk_columns(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"]) if hasattr(row, "keys") else str(row[1])
        for row in conn.execute("PRAGMA table_info(risk_evaluations)").fetchall()
    }
    for name, ddl in (
        ("risk_input_hash", "risk_input_hash TEXT"),
        ("risk_policy_hash", "risk_policy_hash TEXT"),
        ("risk_evidence_hash", "risk_evidence_hash TEXT"),
        ("risk_decision_hash", "risk_decision_hash TEXT"),
        ("risk_reason_code", "risk_reason_code TEXT"),
        ("risk_status", "risk_status TEXT"),
        ("risk_evaluation_point", "risk_evaluation_point TEXT"),
        ("risk_state_source", "risk_state_source TEXT"),
        ("effective_risk_limits_json", "effective_risk_limits_json TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE risk_evaluations ADD COLUMN {ddl}")
