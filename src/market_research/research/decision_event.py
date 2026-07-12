from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ResearchDecisionEvent:
    candle_ts: int
    decision_ts: int
    strategy_name: str
    strategy_version: str
    raw_signal: str
    final_signal: str
    reason: str
    feature_snapshot: dict[str, object]
    strategy_diagnostics: dict[str, object]
    entry_signal: str | None = None
    exit_signal: str | None = None
    blocked_filters: tuple[str, ...] = ()
    order_intent: dict[str, object] | None = None
    exit_intent: dict[str, object] | None = None
    extra_payload: dict[str, Any] = field(default_factory=dict)
