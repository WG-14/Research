"""Immutable strategy-facing projection of authoritative ledger state."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReadOnlyPortfolioView:
    cash: float
    filled_position_qty: float
    cost_basis: float
    average_cost: float | None
    effective_entry_ts: int | None
    pending_execution_count: int
    last_execution_status: str | None
    realized_pnl: float
    unrealized_pnl: float
