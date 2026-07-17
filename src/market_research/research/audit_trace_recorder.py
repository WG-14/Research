from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import backtest_support as support


@dataclass(frozen=True)
class AuditTraceRecorder:
    """Writes audit trace observability without owning execution authority."""

    def record_execution(self, run_context: Any, trade: dict[str, object]) -> None:
        support.trace_execution(run_context, trade)

    def record_decision(
        self, run_context: Any, decision_payload: dict[str, object]
    ) -> None:
        support.trace_decision(run_context, decision_payload)

    def record_equity_mark(
        self,
        run_context: Any,
        *,
        ts: int,
        equity: float,
        cash: float,
        asset_qty: float,
    ) -> None:
        support.trace_equity_mark(
            run_context,
            ts=ts,
            equity=equity,
            cash=cash,
            asset_qty=asset_qty,
        )


__all__ = ["AuditTraceRecorder"]
