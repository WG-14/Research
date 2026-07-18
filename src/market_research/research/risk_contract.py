"""Deterministic research-only risk policy and evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
import math

from .hashing import sha256_prefixed
from .position_model import ResearchPosition


MILLISECONDS_PER_MINUTE = 60_000
MILLISECONDS_PER_UTC_DAY = 86_400_000


def utc_day_index(event_ts: int) -> int:
    """Return the deterministic UTC day bucket for an epoch-millisecond event."""

    return int(event_ts) // MILLISECONDS_PER_UTC_DAY


@dataclass(frozen=True, slots=True)
class ResearchRiskPolicy:
    schema_version: int = 1
    max_daily_loss_krw: float = 0.0
    max_position_loss_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_daily_order_count: int = 0
    max_trade_count_per_day: int = 0
    cooldown_after_loss_min: int = 0
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
            "zero_limit_semantics": "disabled_unbounded",
            "daily_bucket_timezone": "UTC",
            "daily_bucket_timestamp_unit": "epoch_millisecond",
            "max_daily_order_count_basis": (
                "execution_request_created_by_order_intent_utc_day"
            ),
            "max_trade_count_per_day_basis": (
                "portfolio_applied_fill_by_effective_utc_day"
            ),
            "cooldown_after_loss_scope": "buy_entry_intents_only",
            "cooldown_after_loss_boundary": (
                "allow_when_decision_ts_gte_last_realized_loss_ts_plus_cooldown"
            ),
            "max_open_positions_engine_semantics": (
                "single_asset_single_position_exactly_one"
            ),
        }


def compile_research_risk_policy(policy: ResearchRiskPolicy) -> dict[str, object]:
    """Validate engine support and materialize the policy actually executed.

    ``ResearchRiskPolicy`` is also a public construction surface, so manifest
    parsing alone cannot be the execution-readiness authority.  The common
    engine calls this function before consuming any market event.
    """

    if policy.schema_version != 1:
        raise ValueError("unsupported_research_risk_policy_schema_version")
    if policy.policy_status not in {"enabled", "disabled_explicit"}:
        raise ValueError("unsupported_research_risk_policy_status")
    if policy.unresolved_order_policy != "block":
        raise ValueError("unsupported_unresolved_order_policy")
    if policy.missing_policy != "fail_closed_for_validation":
        raise ValueError("unsupported_missing_risk_policy")
    for name in (
        "max_daily_loss_krw",
        "max_position_loss_pct",
        "max_drawdown_pct",
    ):
        value = float(getattr(policy, name))
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"invalid_research_risk_limit:{name}")
    for name in (
        "max_daily_order_count",
        "max_trade_count_per_day",
        "cooldown_after_loss_min",
    ):
        value = getattr(policy, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid_research_risk_limit:{name}")
    # The common engine is deliberately single-asset and non-pyramiding.  A
    # value greater than one would advertise portfolio semantics it cannot
    # execute, so fail closed instead of silently treating it as one.
    if policy.max_open_positions != 1:
        raise ValueError("unsupported_max_open_positions_requires_exactly_one")
    return {
        "schema_version": 1,
        "policy_hash": policy.policy_hash(),
        "declared_policy": policy.as_dict(),
        "effective_limits": policy.effective_limits(),
        "execution_authority": "common_simulation_engine",
        "readiness_status": "PASS",
    }


@dataclass(frozen=True, slots=True)
class ResearchRiskContext:
    """Causal state visible to one risk decision."""

    decision_ts: int
    utc_day: int
    daily_order_count: int
    daily_trade_count: int
    last_realized_loss_ts: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "decision_ts": int(self.decision_ts),
            "utc_day": int(self.utc_day),
            "daily_order_count": int(self.daily_order_count),
            "daily_trade_count": int(self.daily_trade_count),
            "last_realized_loss_ts": (
                int(self.last_realized_loss_ts)
                if self.last_realized_loss_ts is not None
                else None
            ),
            "daily_order_count_basis": (
                "execution_request_created_by_order_intent_utc_day"
            ),
            "daily_trade_count_basis": ("portfolio_applied_fill_by_effective_utc_day"),
        }


@dataclass(slots=True)
class ResearchRiskRuntimeState:
    """Run-local counters updated only by authoritative execution events."""

    order_counts_by_utc_day: dict[int, int] = field(default_factory=dict)
    trade_counts_by_utc_day: dict[int, int] = field(default_factory=dict)
    last_realized_loss_ts: int | None = None

    def context_at(self, decision_ts: int) -> ResearchRiskContext:
        day = utc_day_index(decision_ts)
        return ResearchRiskContext(
            decision_ts=int(decision_ts),
            utc_day=day,
            daily_order_count=int(self.order_counts_by_utc_day.get(day, 0)),
            daily_trade_count=int(self.trade_counts_by_utc_day.get(day, 0)),
            last_realized_loss_ts=self.last_realized_loss_ts,
        )

    def record_execution_request(self, *, order_intent_ts: int) -> None:
        day = utc_day_index(order_intent_ts)
        self.order_counts_by_utc_day[day] = (
            int(self.order_counts_by_utc_day.get(day, 0)) + 1
        )

    def record_portfolio_applied_fill(
        self, *, effective_ts: int, realized_pnl: float | None
    ) -> None:
        day = utc_day_index(effective_ts)
        self.trade_counts_by_utc_day[day] = (
            int(self.trade_counts_by_utc_day.get(day, 0)) + 1
        )
        if realized_pnl is not None and float(realized_pnl) < 0.0:
            self.last_realized_loss_ts = int(effective_ts)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "order_counts_by_utc_day": [
                {"utc_day": int(day), "count": int(count)}
                for day, count in sorted(self.order_counts_by_utc_day.items())
            ],
            "trade_counts_by_utc_day": [
                {"utc_day": int(day), "count": int(count)}
                for day, count in sorted(self.trade_counts_by_utc_day.items())
            ],
            "last_realized_loss_ts": (
                int(self.last_realized_loss_ts)
                if self.last_realized_loss_ts is not None
                else None
            ),
            "order_count_basis": ("execution_request_created_by_order_intent_utc_day"),
            "trade_count_basis": ("portfolio_applied_fill_by_effective_utc_day"),
            "loss_timestamp_basis": "negative_realized_pnl_ledger_entry_effective_ts",
        }

    def state_hash(self) -> str:
        return sha256_prefixed(self.as_dict())


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
    risk_context: ResearchRiskContext | None = None,
) -> ResearchRiskDecision:
    signal = str(requested_signal or "HOLD").upper()
    reason = "none"
    if signal == "BUY" and position.in_position:
        allowed, reason = False, "buy_blocked_existing_position"
    elif signal == "SELL" and not position.in_position:
        allowed, reason = False, "sell_blocked_no_sellable_qty"
    elif policy.policy_status == "disabled_explicit":
        allowed = signal in {"BUY", "SELL"}
    elif (
        policy.max_daily_order_count > 0
        or policy.max_trade_count_per_day > 0
        or policy.cooldown_after_loss_min > 0
    ) and risk_context is None:
        allowed, reason = False, "stateful_risk_context_missing"
    elif (
        policy.max_daily_order_count > 0
        and risk_context is not None
        and risk_context.daily_order_count >= policy.max_daily_order_count
    ):
        allowed, reason = False, "max_daily_order_count_reached"
    elif (
        policy.max_trade_count_per_day > 0
        and risk_context is not None
        and risk_context.daily_trade_count >= policy.max_trade_count_per_day
    ):
        allowed, reason = False, "max_trade_count_per_day_reached"
    elif (
        signal == "BUY"
        and policy.cooldown_after_loss_min > 0
        and risk_context is not None
        and risk_context.last_realized_loss_ts is not None
        and risk_context.decision_ts
        < risk_context.last_realized_loss_ts
        + policy.cooldown_after_loss_min * MILLISECONDS_PER_MINUTE
    ):
        allowed, reason = False, "loss_cooldown_active"
    elif (
        policy.max_daily_loss_krw > 0.0
        and baseline_equity - current_equity >= policy.max_daily_loss_krw
    ):
        allowed, reason = False, "daily_loss_limit"
    elif policy.max_position_loss_pct > 0.0 and position.unrealized_pnl_ratio(
        market_price
    ) <= -(policy.max_position_loss_pct / 100.0):
        allowed, reason = False, "position_loss_limit"
    elif (
        policy.max_drawdown_pct > 0.0
        and peak_equity > 0.0
        and ((peak_equity - current_equity) / peak_equity * 100.0)
        >= policy.max_drawdown_pct
    ):
        allowed, reason = False, "max_drawdown_limit"
    else:
        allowed = signal in {"BUY", "SELL"}
    evidence = {
        "schema_version": 1,
        "policy": policy.as_dict(),
        "policy_hash": policy.policy_hash(),
        "effective_policy": policy.effective_limits(),
        "requested_signal": signal,
        "allowed": allowed,
        "reason_code": reason,
        "baseline_equity": float(baseline_equity),
        "current_equity": float(current_equity),
        "peak_equity": float(peak_equity),
        "position_state_hash": position.position_state_hash(market_price=market_price),
        "risk_context": risk_context.as_dict() if risk_context is not None else None,
        "cooldown_until_ts": (
            int(risk_context.last_realized_loss_ts)
            + int(policy.cooldown_after_loss_min) * MILLISECONDS_PER_MINUTE
            if risk_context is not None
            and risk_context.last_realized_loss_ts is not None
            and policy.cooldown_after_loss_min > 0
            else None
        ),
    }
    return ResearchRiskDecision(allowed=allowed, reason_code=reason, evidence=evidence)
