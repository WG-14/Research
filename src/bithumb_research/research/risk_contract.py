"""Deterministic research-only risk policy and evidence."""

from __future__ import annotations

from dataclasses import dataclass

from .hashing import sha256_prefixed
from .position_model import ResearchPosition


@dataclass(frozen=True, slots=True)
class ResearchRiskPolicy:
    schema_version: int = 1
    max_daily_loss_krw: float = 0.0
    max_position_loss_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_daily_order_count: int = 0
    max_trade_count_per_day: int = 0
    cooldown_after_loss_min: int = 0
    kill_switch: bool = False
    max_open_positions: int = 1
    unresolved_order_policy: str = "block"
    policy_status: str = "enabled"
    missing_policy: str = "fail_closed_for_validation"
    source: str = "research_manifest"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "max_daily_loss_krw": float(self.max_daily_loss_krw),
            "max_position_loss_pct": float(self.max_position_loss_pct),
            "max_drawdown_pct": float(self.max_drawdown_pct),
            "max_daily_order_count": int(self.max_daily_order_count),
            "max_trade_count_per_day": int(self.max_trade_count_per_day),
            "cooldown_after_loss_min": int(self.cooldown_after_loss_min),
            "kill_switch": bool(self.kill_switch),
            "max_open_positions": int(self.max_open_positions),
            "unresolved_order_policy": self.unresolved_order_policy,
            "policy_status": self.policy_status,
            "missing_policy": self.missing_policy,
            "source": self.source,
        }

    def policy_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def effective_limits(self) -> dict[str, object]:
        return {
            "max_daily_loss_krw": float(self.max_daily_loss_krw),
            "max_position_loss_pct": float(self.max_position_loss_pct),
            "max_daily_order_count": int(self.max_daily_order_count),
            "max_trade_count_per_day": int(self.max_trade_count_per_day),
            "max_drawdown_pct": float(self.max_drawdown_pct),
            "cooldown_after_loss_min": int(self.cooldown_after_loss_min),
            "max_open_positions": int(self.max_open_positions),
            "risk_policy_status": self.policy_status,
        }


@dataclass(frozen=True, slots=True)
class ResearchRiskDecision:
    allowed: bool
    reason_code: str
    evidence: dict[str, object]

    @property
    def evidence_hash(self) -> str:
        return sha256_prefixed(self.evidence)


def evaluate_research_risk(
    *,
    policy: ResearchRiskPolicy,
    requested_signal: str,
    position: ResearchPosition,
    market_price: float,
    baseline_equity: float,
    current_equity: float,
    peak_equity: float,
) -> ResearchRiskDecision:
    signal = str(requested_signal or "HOLD").upper()
    reason = "none"
    if signal == "BUY" and position.in_position:
        allowed, reason = False, "buy_blocked_existing_position"
    elif signal == "SELL" and not position.in_position:
        allowed, reason = False, "sell_blocked_no_sellable_qty"
    elif policy.policy_status == "disabled_explicit":
        allowed = signal in {"BUY", "SELL"}
    elif policy.max_daily_loss_krw > 0.0 and baseline_equity - current_equity >= policy.max_daily_loss_krw:
        allowed, reason = False, "daily_loss_limit"
    elif policy.max_position_loss_pct > 0.0 and position.unrealized_pnl_ratio(market_price) <= -(policy.max_position_loss_pct / 100.0):
        allowed, reason = False, "position_loss_limit"
    elif policy.max_drawdown_pct > 0.0 and peak_equity > 0.0 and ((peak_equity - current_equity) / peak_equity * 100.0) >= policy.max_drawdown_pct:
        allowed, reason = False, "max_drawdown_limit"
    else:
        allowed = signal in {"BUY", "SELL"}
    evidence = {
        "schema_version": 1,
        "policy": policy.as_dict(),
        "policy_hash": policy.policy_hash(),
        "requested_signal": signal,
        "allowed": allowed,
        "reason_code": reason,
        "baseline_equity": float(baseline_equity),
        "current_equity": float(current_equity),
        "peak_equity": float(peak_equity),
        "position_state_hash": position.position_state_hash(market_price=market_price),
    }
    return ResearchRiskDecision(allowed=allowed, reason_code=reason, evidence=evidence)
