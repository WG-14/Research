from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .lifecycle_artifacts import RuntimeCycleArtifact
from .. import runtime_state
from ..observability import format_log_kv

RUN_LOG = logging.getLogger("bithumb_bot.run")


@dataclass(frozen=True)
class CheckpointDecision:
    status: str
    allowed: bool
    cycle_id: str
    reason: str
    candle_ts: int | None


@dataclass(frozen=True)
class RuntimeCheckpoint:
    symbol: str
    interval: str

    def evaluate_closed_candle(
        self,
        *,
        closed_row: Any,
        incomplete_ts: int | None,
        last_processed_candle_ts_ms: int | None,
        close_guard_ms: int,
    ) -> CheckpointDecision:
        if incomplete_ts is not None:
            RUN_LOG.info(
                format_log_kv(
                    "[SKIP] incomplete/open candle",
                    symbol=self.symbol,
                    interval=self.interval,
                    candle_ts=incomplete_ts,
                    last_processed_candle_ts=last_processed_candle_ts_ms,
                    reason=f"latest candle has not cleared close guard ({close_guard_ms}ms)",
                )
            )
        if closed_row is None:
            return CheckpointDecision(
                status="no_closed_candle",
                allowed=False,
                cycle_id="skip:no_closed_candle",
                reason="no fully closed candle available yet",
                candle_ts=incomplete_ts,
            )
        closed_ts = int(closed_row["ts"]) if hasattr(closed_row, "keys") else int(closed_row[0])
        if last_processed_candle_ts_ms is not None:
            if closed_ts == last_processed_candle_ts_ms:
                return CheckpointDecision(
                    status="duplicate",
                    allowed=False,
                    cycle_id="skip:duplicate_candle",
                    reason="closed candle already processed before restart/previous tick",
                    candle_ts=closed_ts,
                )
            if closed_ts < last_processed_candle_ts_ms:
                return CheckpointDecision(
                    status="stale_processed",
                    allowed=False,
                    cycle_id="skip:stale_processed_candle",
                    reason="closed candle is older than persisted last processed candle",
                    candle_ts=closed_ts,
                )
        return CheckpointDecision(
            status="ready",
            allowed=True,
            cycle_id="checkpoint:candidate",
            reason="closed candle ready",
            candle_ts=closed_ts,
        )

    def apply(self, *, candle_ts_ms: int, now_epoch_sec: float | None = None) -> None:
        apply_processed_candle_checkpoint(candle_ts_ms=candle_ts_ms, now_epoch_sec=now_epoch_sec)


def apply_processed_candle_checkpoint(*, candle_ts_ms: int, now_epoch_sec: float | None = None) -> None:
    runtime_state.mark_processed_candle(candle_ts_ms=candle_ts_ms, now_epoch_sec=now_epoch_sec)


__all__ = [
    "CheckpointDecision",
    "RuntimeCheckpoint",
    "RuntimeCycleArtifact",
    "apply_processed_candle_checkpoint",
]
