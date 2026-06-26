from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from .decision_equivalence import sha256_prefixed


H74_CYCLE_STATE_HOLDING = "HOLDING"
H74_CYCLE_STATE_CLOSED = "CLOSED"


def build_h74_cycle_id(*, strategy_instance_id: str, entry_client_order_id: str, authority_hash: str) -> str:
    digest = sha256_prefixed(
        {
            "strategy_instance_id": strategy_instance_id,
            "entry_client_order_id": entry_client_order_id,
            "authority_hash": authority_hash,
        }
    )
    return "h74-" + digest.split(":", 1)[1][:24]


@dataclass(frozen=True)
class H74CycleInventory:
    cycle_id: str
    authority_hash: str
    strategy_instance_id: str
    acquired_qty: float
    sold_qty: float
    locked_exit_qty: float

    @property
    def remaining_cycle_qty(self) -> float:
        return max(0.0, float(self.acquired_qty) - float(self.sold_qty) - float(self.locked_exit_qty))

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "cycle_id": self.cycle_id,
            "authority_hash": self.authority_hash,
            "strategy_instance_id": self.strategy_instance_id,
            "acquired_qty": float(self.acquired_qty),
            "sold_qty": float(self.sold_qty),
            "locked_exit_qty": float(self.locked_exit_qty),
            "remaining_cycle_qty": self.remaining_cycle_qty,
        }
        payload["cycle_inventory_hash"] = sha256_prefixed(payload)
        return payload


def ensure_h74_cycle_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS h74_cycle_state (
            cycle_id TEXT PRIMARY KEY,
            authority_hash TEXT NOT NULL,
            strategy_instance_id TEXT NOT NULL,
            pair TEXT NOT NULL DEFAULT 'KRW-BTC',
            state TEXT NOT NULL DEFAULT 'HOLDING',
            entry_client_order_id TEXT,
            exit_client_order_id TEXT,
            entry_filled_ts INTEGER,
            scheduled_exit_ts INTEGER,
            acquired_qty REAL NOT NULL DEFAULT 0,
            sold_qty REAL NOT NULL DEFAULT 0,
            locked_exit_qty REAL NOT NULL DEFAULT 0,
            contract_hash TEXT,
            unauthorized_intermediate_order_count INTEGER NOT NULL DEFAULT 0,
            updated_ts INTEGER NOT NULL DEFAULT (strftime('%s','now') * 1000)
        )
        """
    )
    columns = {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in conn.execute("PRAGMA table_info(h74_cycle_state)").fetchall()
    }
    if "contract_hash" not in columns:
        conn.execute("ALTER TABLE h74_cycle_state ADD COLUMN contract_hash TEXT")


def upsert_h74_cycle_fill(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    authority_hash: str,
    strategy_instance_id: str,
    pair: str,
    side: str,
    qty: float,
    client_order_id: str,
    fill_ts: int,
    contract_hash: str | None = None,
    max_holding_minutes: int = 74,
) -> None:
    ensure_h74_cycle_schema(conn)
    normalized_side = str(side or "").upper()
    acquired_delta = float(qty) if normalized_side == "BUY" else 0.0
    sold_delta = float(qty) if normalized_side == "SELL" else 0.0
    entry_id = client_order_id if normalized_side == "BUY" else None
    exit_id = client_order_id if normalized_side == "SELL" else None
    scheduled_exit_ts = int(fill_ts) + int(max_holding_minutes) * 60_000 if normalized_side == "BUY" else None
    conn.execute(
        """
        INSERT INTO h74_cycle_state(
            cycle_id, authority_hash, strategy_instance_id, pair, state,
            entry_client_order_id, exit_client_order_id, entry_filled_ts,
            scheduled_exit_ts, acquired_qty, sold_qty, contract_hash, updated_ts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cycle_id) DO UPDATE SET
            authority_hash=excluded.authority_hash,
            strategy_instance_id=excluded.strategy_instance_id,
            pair=excluded.pair,
            contract_hash=COALESCE(excluded.contract_hash, h74_cycle_state.contract_hash),
            entry_client_order_id=COALESCE(h74_cycle_state.entry_client_order_id, excluded.entry_client_order_id),
            exit_client_order_id=COALESCE(excluded.exit_client_order_id, h74_cycle_state.exit_client_order_id),
            entry_filled_ts=COALESCE(h74_cycle_state.entry_filled_ts, excluded.entry_filled_ts),
            scheduled_exit_ts=COALESCE(h74_cycle_state.scheduled_exit_ts, excluded.scheduled_exit_ts),
            acquired_qty=h74_cycle_state.acquired_qty + excluded.acquired_qty,
            sold_qty=h74_cycle_state.sold_qty + excluded.sold_qty,
            state=CASE
                WHEN h74_cycle_state.acquired_qty + excluded.acquired_qty - h74_cycle_state.sold_qty - excluded.sold_qty - h74_cycle_state.locked_exit_qty <= 1e-12
                     AND h74_cycle_state.acquired_qty + excluded.acquired_qty > 0
                THEN 'CLOSED'
                ELSE h74_cycle_state.state
            END,
            updated_ts=excluded.updated_ts
        """,
        (
            cycle_id,
            authority_hash,
            strategy_instance_id,
            pair,
            H74_CYCLE_STATE_HOLDING,
            entry_id,
            exit_id,
            int(fill_ts) if normalized_side == "BUY" else None,
            scheduled_exit_ts,
            acquired_delta,
            sold_delta,
            str(contract_hash or "").strip() or None,
            int(fill_ts),
        ),
    )


def load_h74_cycle_inventory(conn: sqlite3.Connection, *, cycle_id: str) -> H74CycleInventory | None:
    ensure_h74_cycle_schema(conn)
    row = conn.execute(
        """
        SELECT cycle_id, authority_hash, strategy_instance_id, acquired_qty, sold_qty, locked_exit_qty
        FROM h74_cycle_state
        WHERE cycle_id=?
        """,
        (cycle_id,),
    ).fetchone()
    if row is None:
        return None
    return H74CycleInventory(
        cycle_id=str(row["cycle_id"] if hasattr(row, "keys") else row[0]),
        authority_hash=str(row["authority_hash"] if hasattr(row, "keys") else row[1]),
        strategy_instance_id=str(row["strategy_instance_id"] if hasattr(row, "keys") else row[2]),
        acquired_qty=float(row["acquired_qty"] if hasattr(row, "keys") else row[3]),
        sold_qty=float(row["sold_qty"] if hasattr(row, "keys") else row[4]),
        locked_exit_qty=float(row["locked_exit_qty"] if hasattr(row, "keys") else row[5]),
    )


def load_open_h74_cycle_inventories(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    authority_hash: str,
    pair: str,
) -> tuple[H74CycleInventory, ...]:
    ensure_h74_cycle_schema(conn)
    rows = conn.execute(
        """
        SELECT cycle_id, authority_hash, strategy_instance_id, acquired_qty, sold_qty, locked_exit_qty
        FROM h74_cycle_state
        WHERE strategy_instance_id=?
          AND authority_hash=?
          AND pair=?
          AND state=?
        ORDER BY updated_ts ASC, cycle_id ASC
        """,
        (
            str(strategy_instance_id),
            str(authority_hash),
            str(pair),
            H74_CYCLE_STATE_HOLDING,
        ),
    ).fetchall()
    return tuple(
        H74CycleInventory(
            cycle_id=str(row["cycle_id"] if hasattr(row, "keys") else row[0]),
            authority_hash=str(row["authority_hash"] if hasattr(row, "keys") else row[1]),
            strategy_instance_id=str(row["strategy_instance_id"] if hasattr(row, "keys") else row[2]),
            acquired_qty=float(row["acquired_qty"] if hasattr(row, "keys") else row[3]),
            sold_qty=float(row["sold_qty"] if hasattr(row, "keys") else row[4]),
            locked_exit_qty=float(row["locked_exit_qty"] if hasattr(row, "keys") else row[5]),
        )
        for row in rows
    )


def load_open_h74_cycle_inventory(
    conn: sqlite3.Connection,
    *,
    strategy_instance_id: str,
    authority_hash: str,
    pair: str,
) -> H74CycleInventory | None:
    inventories = load_open_h74_cycle_inventories(
        conn,
        strategy_instance_id=strategy_instance_id,
        authority_hash=authority_hash,
        pair=pair,
    )
    if len(inventories) > 1:
        raise ValueError("multiple_open_h74_cycles")
    return inventories[0] if inventories else None


def lock_h74_cycle_exit_qty(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    exit_client_order_id: str,
    qty: float,
    updated_ts: int,
) -> None:
    ensure_h74_cycle_schema(conn)
    lock_qty = max(0.0, float(qty))
    conn.execute(
        """
        UPDATE h74_cycle_state
        SET locked_exit_qty=?,
            exit_client_order_id=?,
            updated_ts=?
        WHERE cycle_id=?
        """,
        (lock_qty, str(exit_client_order_id), int(updated_ts), str(cycle_id)),
    )


def h74_cycle_inventory_from_payload(payload: Mapping[str, Any]) -> H74CycleInventory:
    return H74CycleInventory(
        cycle_id=str(payload.get("cycle_id") or payload.get("h74_cycle_id") or ""),
        authority_hash=str(payload.get("authority_hash") or payload.get("h74_authority_hash") or ""),
        strategy_instance_id=str(payload.get("strategy_instance_id") or ""),
        acquired_qty=float(payload.get("acquired_qty") or payload.get("h74_acquired_qty") or 0.0),
        sold_qty=float(payload.get("sold_qty") or payload.get("h74_sold_qty") or 0.0),
        locked_exit_qty=float(payload.get("locked_exit_qty") or payload.get("h74_locked_exit_qty") or 0.0),
    )


def h74_cycle_health_invariant_reasons(conn: sqlite3.Connection) -> tuple[str, ...]:
    tables = {
        str(row["name"] if hasattr(row, "keys") else row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if "h74_cycle_state" not in tables:
        return ("h74_cycle_schema_missing",)
    reasons: list[str] = []
    buy_rows = conn.execute(
        """
        SELECT client_order_id, cycle_id, strategy_instance_id, authority_hash
        FROM orders
        WHERE side='BUY'
          AND strategy_name='daily_participation_sma'
          AND status IN ('FILLED','PARTIALLY_FILLED')
        """
    ).fetchall()
    for row in buy_rows:
        client_order_id = str(row["client_order_id"] if hasattr(row, "keys") else row[0])
        cycle_id = str((row["cycle_id"] if hasattr(row, "keys") else row[1]) or "").strip()
        strategy_instance_id = str((row["strategy_instance_id"] if hasattr(row, "keys") else row[2]) or "").strip()
        authority_hash = str((row["authority_hash"] if hasattr(row, "keys") else row[3]) or "").strip()
        if not cycle_id or not strategy_instance_id or not authority_hash:
            reasons.append("h74_cycle_ownership_incomplete")
            continue
        cycle = conn.execute(
            "SELECT acquired_qty, sold_qty, locked_exit_qty FROM h74_cycle_state WHERE cycle_id=?",
            (cycle_id,),
        ).fetchone()
        if cycle is None:
            reasons.append("h74_cycle_ownership_incomplete")
            continue
        fill_qty = conn.execute(
            "SELECT COALESCE(SUM(qty),0) AS qty FROM fills WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        summed = float(fill_qty["qty"] if hasattr(fill_qty, "keys") else fill_qty[0])
        acquired = float(cycle["acquired_qty"] if hasattr(cycle, "keys") else cycle[0])
        sold = float(cycle["sold_qty"] if hasattr(cycle, "keys") else cycle[1])
        locked = float(cycle["locked_exit_qty"] if hasattr(cycle, "keys") else cycle[2])
        if acquired + 1e-12 < summed:
            reasons.append("h74_cycle_qty_mismatch")
        if acquired - sold - locked < -1e-12:
            reasons.append("h74_cycle_negative_remaining_qty")
    return tuple(dict.fromkeys(reasons))


__all__ = [
    "H74_CYCLE_STATE_HOLDING",
    "H74_CYCLE_STATE_CLOSED",
    "H74CycleInventory",
    "build_h74_cycle_id",
    "ensure_h74_cycle_schema",
    "h74_cycle_inventory_from_payload",
    "h74_cycle_health_invariant_reasons",
    "load_h74_cycle_inventory",
    "load_open_h74_cycle_inventories",
    "load_open_h74_cycle_inventory",
    "lock_h74_cycle_exit_qty",
    "upsert_h74_cycle_fill",
]
