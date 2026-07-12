from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .hashing import sha256_prefixed


@dataclass(frozen=True)
class OrderIntent:
    """Versioned, immutable instruction emitted by a research decision.

    This is deliberately not an order.  It has no fill price or portfolio
    effect; the common simulation engine resolves both after timing and risk
    policy have been applied.
    """

    decision_id: str
    intent_id: str
    side: str
    sizing: str = "portfolio_policy_fractional_cash"
    buy_fraction: float | None = None
    requested_qty: float | None = None
    reason: str = ""
    order_intent_ts: int = 0
    exit_rule: str | None = None
    exit_reason: str | None = None
    schema_version: int = 1

    @classmethod
    def from_decision(cls, *, decision_id: str, side: str, **values: Any) -> "OrderIntent":
        payload = {"schema_version": 1, "decision_id": decision_id, "side": str(side).upper(), **values}
        return cls(
            decision_id=decision_id,
            intent_id=sha256_prefixed(payload),
            side=str(side).upper(),
            sizing=str(values.get("sizing") or "portfolio_policy_fractional_cash"),
            buy_fraction=(float(values["buy_fraction"]) if values.get("buy_fraction") is not None else None),
            requested_qty=(float(values["requested_qty"]) if values.get("requested_qty") is not None else None),
            reason=str(values.get("reason") or ""),
            order_intent_ts=int(values.get("order_intent_ts") or values.get("decision_ts") or 0),
            exit_rule=(str(values["exit_rule"]) if values.get("exit_rule") is not None else None),
            exit_reason=(str(values["exit_reason"]) if values.get("exit_reason") is not None else None),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "decision_id": self.decision_id,
            "intent_id": self.intent_id, "side": self.side, "sizing": self.sizing,
            "buy_fraction": self.buy_fraction, "requested_qty": self.requested_qty,
            "reason": self.reason, "order_intent_ts": self.order_intent_ts,
            "exit_rule": self.exit_rule, "exit_reason": self.exit_reason,
        }

    def __getitem__(self, key: str) -> object:
        """Read-only compatibility projection; the stored stream remains typed."""
        return self.as_dict()[key]


@dataclass(frozen=True)
class ResearchDecisionEvent:
    """Typed strategy decision; ``decision_id`` is stable authoritative lineage."""
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
    order_intent: OrderIntent | None = None
    exit_intent: OrderIntent | None = None
    extra_payload: dict[str, Any] = field(default_factory=dict)
    authoritative_decision_id: str = ""

    def __post_init__(self) -> None:
        calculated = self._calculated_decision_id()
        if self.authoritative_decision_id and self.authoritative_decision_id != calculated:
            raise ValueError("decision_id_content_mismatch")
        object.__setattr__(self, "authoritative_decision_id", calculated)

    def decision_id(self) -> str:
        return self.authoritative_decision_id

    def _calculated_decision_id(self) -> str:
        return sha256_prefixed({
            "strategy_name": self.strategy_name, "strategy_version": self.strategy_version,
            "candle_ts": self.candle_ts, "decision_ts": self.decision_ts,
            "raw_signal": self.raw_signal, "final_signal": self.final_signal,
            "reason": self.reason, "feature_snapshot": self.feature_snapshot,
        })

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.authoritative_decision_id, "candle_ts": self.candle_ts,
            "decision_ts": self.decision_ts, "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version, "raw_signal": self.raw_signal,
            "entry_signal": self.entry_signal, "exit_signal": self.exit_signal,
            "final_signal": self.final_signal, "reason": self.reason,
            "feature_snapshot": self.feature_snapshot,
            "strategy_diagnostics": self.strategy_diagnostics,
        }

    def __getitem__(self, key: str) -> object:
        """Read-only compatibility projection; the stored stream remains typed."""
        return self.as_dict()[key]
