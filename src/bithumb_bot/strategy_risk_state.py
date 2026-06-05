from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .canonical_decision import canonical_payload_hash
from .oms import collect_risky_order_state
from .risk import (
    _count_orders_today,
    _latest_position_entry_price,
    daily_loss_reason_code_from_reason,
    evaluate_daily_loss_state,
)
from .risk_contract import RiskPolicy, RiskSnapshot
from .runtime_risk_engine import _classify_unresolved_state


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _count_trades_today(conn: sqlite3.Connection, ts_ms: int, *, pair: str) -> int | None:
    if not _table_exists(conn, "trades"):
        return None
    day_start_ms = int(ts_ms) - (int(ts_ms) % 86_400_000)
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM trades
        WHERE ts >= ? AND ts <= ? AND COALESCE(pair, '') = COALESCE(?, COALESCE(pair, ''))
        """,
        (day_start_ms, int(ts_ms), str(pair)),
    ).fetchone()
    if row is None:
        return 0
    return int(row["count"] if hasattr(row, "keys") else row[0])


def _portfolio_asset_qty(conn: sqlite3.Connection) -> float | None:
    if not _table_exists(conn, "portfolio"):
        return None
    row = conn.execute(
        "SELECT asset_available, asset_locked, asset_qty FROM portfolio ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    getter = row.__getitem__
    available = getter("asset_available") if hasattr(row, "keys") else row[0]
    locked = getter("asset_locked") if hasattr(row, "keys") else row[1]
    qty = getter("asset_qty") if hasattr(row, "keys") else row[2]
    if available is not None or locked is not None:
        return float(available or 0.0) + float(locked or 0.0)
    if qty is not None:
        return float(qty or 0.0)
    return None


def _open_exposure_qty(conn: sqlite3.Connection, *, pair: str) -> float | None:
    if not _table_exists(conn, "open_position_lots"):
        return None
    row = conn.execute(
        """
        SELECT COALESCE(SUM(qty_open), 0.0) AS qty
        FROM open_position_lots
        WHERE pair=? AND position_state='open_exposure'
        """,
        (str(pair),),
    ).fetchone()
    if row is None:
        return 0.0
    return float(row["qty"] if hasattr(row, "keys") else row[0])


def _missing_required_state(policy: RiskPolicy, snapshot: RiskSnapshot) -> tuple[str, ...]:
    missing: list[str] = []
    if float(policy.max_daily_loss_krw) > 0.0 and snapshot.loss_today is None:
        missing.append("loss_today")
    if float(policy.max_position_loss_pct) > 0.0 and (
        snapshot.current_asset_qty is None or snapshot.position_entry_price is None
    ):
        missing.append("position_loss_state")
    if int(policy.max_daily_order_count) > 0 and snapshot.daily_order_count is None:
        missing.append("daily_order_count")
    if int(policy.max_trade_count_per_day) > 0 and snapshot.daily_trade_count is None:
        missing.append("daily_trade_count")
    if float(policy.max_drawdown_pct) > 0.0 and snapshot.current_drawdown_pct is None:
        missing.append("current_drawdown_pct")
    if int(policy.cooldown_after_loss_min) > 0 and snapshot.minutes_since_last_loss is None:
        missing.append("minutes_since_last_loss")
    return tuple(dict.fromkeys(missing))


@dataclass(frozen=True)
class StrategyRiskStateProvider:
    conn: sqlite3.Connection
    max_open_order_age_sec: int = 300

    def snapshot(
        self,
        *,
        strategy_instance_id: str,
        strategy_name: str,
        pair: str,
        interval: str,
        as_of_ts_ms: int,
        mark_price: float,
        policy: RiskPolicy | None = None,
        broker: object | None = None,
        mark_price_source: str = "market_price",
        enforced: bool = False,
    ) -> RiskSnapshot:
        daily = evaluate_daily_loss_state(
            self.conn,
            ts_ms=int(as_of_ts_ms),
            price=float(mark_price),
            broker=broker,
            mark_price_source=mark_price_source,
            evaluation_origin="strategy_risk_state_provider",
        )
        mismatch = daily.reason_code == "RISK_STATE_MISMATCH" or daily_loss_reason_code_from_reason(
            daily.reason
        ) == "RISK_STATE_MISMATCH"
        unresolved_state: dict[str, Any] = dict(
            collect_risky_order_state(
                self.conn,
                now_ms=int(as_of_ts_ms),
                max_open_order_age_sec=int(self.max_open_order_age_sec),
            )
        )
        unresolved_blocked, unresolved_reason_code, unresolved_reason = _classify_unresolved_state(
            unresolved_state,
            max_open_order_age_sec=int(self.max_open_order_age_sec),
        )
        asset_qty = _open_exposure_qty(self.conn, pair=pair)
        if asset_qty is None:
            asset_qty = _portfolio_asset_qty(self.conn)
        evidence = {
            "strategy_instance_id": str(strategy_instance_id),
            "strategy_name": str(strategy_name),
            "pair": str(pair),
            "interval": str(interval),
            "as_of_ts_ms": int(as_of_ts_ms),
            "state_tables": {
                "portfolio": _table_exists(self.conn, "portfolio"),
                "orders": _table_exists(self.conn, "orders"),
                "trades": _table_exists(self.conn, "trades"),
                "open_position_lots": _table_exists(self.conn, "open_position_lots"),
            },
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
                "evaluated_once": True,
            },
        }
        snapshot = RiskSnapshot(
            evaluation_ts_ms=int(as_of_ts_ms),
            mark_price=float(mark_price),
            current_equity=daily.current_equity,
            baseline_equity=daily.start_equity,
            loss_today=daily.loss_today,
            current_cash_krw=daily.current_cash_krw,
            current_asset_qty=asset_qty,
            position_entry_price=_latest_position_entry_price(self.conn),
            broker_local_mismatch=bool(mismatch),
            recovery_risk_mismatch_reason=daily.reason if mismatch else None,
            duplicate_entry=bool(asset_qty is not None and float(asset_qty) > 1e-12),
            daily_order_count=_count_orders_today(self.conn, int(as_of_ts_ms)),
            daily_trade_count=_count_trades_today(self.conn, int(as_of_ts_ms), pair=pair),
            unresolved_order_blocked=bool(unresolved_blocked),
            unresolved_order_reason_code=str(unresolved_reason_code),
            unresolved_order_reason=str(unresolved_reason),
            state_source="runtime_db_ledger",
            evidence=evidence,
        )
        missing = _missing_required_state(policy, snapshot) if policy is not None else ()
        if missing:
            evidence = {
                **evidence,
                "missing_required_risk_state": list(missing),
                "missing_required_risk_state_behavior": (
                    "fail_closed" if enforced else "telemetry"
                ),
            }
            snapshot = RiskSnapshot(
                **{**snapshot.as_dict(), "evidence": evidence}  # type: ignore[arg-type]
            )
        evidence = {
            **dict(snapshot.evidence),
            "risk_state_evidence_hash": canonical_payload_hash(snapshot.evidence),
        }
        return RiskSnapshot(**{**snapshot.as_dict(), "evidence": evidence})  # type: ignore[arg-type]


def missing_required_risk_state(policy: RiskPolicy, snapshot: RiskSnapshot) -> tuple[str, ...]:
    return _missing_required_state(policy, snapshot)
