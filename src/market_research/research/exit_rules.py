"""SMA research exit policy; deliberately independent of runtime strategy modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .hashing import sha256_prefixed
from .position_model import ResearchPosition


@dataclass(frozen=True, slots=True)
class ResearchExitDecision:
    triggered: bool
    rule: str | None
    reason: str
    evaluations: tuple[dict[str, object], ...]


def materialize_sma_exit_policy(strategy_name: str, parameter_values: dict[str, Any]) -> dict[str, object]:
    names = tuple(
        item.strip().lower()
        for item in str(parameter_values.get("STRATEGY_EXIT_RULES") or "stop_loss,opposite_cross,max_holding_time").split(",")
        if item.strip()
    )
    allowed = {"stop_loss", "opposite_cross", "max_holding_time"}
    unknown = sorted(set(names) - allowed)
    if unknown:
        raise ValueError(f"unknown exit rule={unknown[0]!r}")
    policy = {
        "schema_version": 1,
        "strategy_name": strategy_name,
        "rules": list(names),
        "stop_loss": {"stop_loss_ratio": float(parameter_values.get("STRATEGY_EXIT_STOP_LOSS_RATIO") or 0.0)},
        "max_holding_time": {"max_holding_min": int(parameter_values.get("STRATEGY_EXIT_MAX_HOLDING_MIN") or 0)},
        "opposite_cross": {
            "min_take_profit_ratio": float(parameter_values.get("STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO") or 0.0),
            "small_loss_tolerance_ratio": float(parameter_values.get("STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO") or 0.0),
            "live_fee_rate_estimate": float(parameter_values.get("LIVE_FEE_RATE_ESTIMATE") or 0.0),
        },
        "parameter_source": "research_materialized_parameters",
    }
    return {
        "exit_policy": policy,
        "exit_policy_config": dict(policy),
        "exit_policy_hash": sha256_prefixed(policy),
        "exit_policy_config_hash": sha256_prefixed(policy),
        "exit_policy_contract_hash": sha256_prefixed({"schema_version": 1, "strategy_name": strategy_name, "rules": list(names)}),
        "exit_policy_source": "research.exit_rules.materialize_sma_exit_policy",
        "exit_policy_materialization_mode": "research_only",
    }


def evaluate_sma_exit_policy(
    *,
    policy: dict[str, object],
    position: ResearchPosition,
    candle_ts: int,
    market_price: float,
    exit_signal: str,
) -> ResearchExitDecision:
    if not position.in_position:
        return ResearchExitDecision(False, None, "no open position for exit policy", ())
    evaluations: list[dict[str, object]] = []
    pnl_ratio = position.unrealized_pnl_ratio(market_price)
    for name in policy.get("rules") or ():
        rule = str(name)
        if rule == "stop_loss":
            threshold = float(dict(policy.get("stop_loss") or {}).get("stop_loss_ratio") or 0.0)
            triggered = threshold > 0.0 and pnl_ratio <= -threshold
            reason = "exit by stop loss" if triggered else "stop loss not triggered"
            context = {"threshold_ratio": threshold, "unrealized_pnl_ratio": pnl_ratio}
        elif rule == "opposite_cross":
            values = dict(policy.get("opposite_cross") or {})
            configured_floor = max(0.0, float(values.get("min_take_profit_ratio") or 0.0))
            roundtrip_fee = 2.0 * max(0.0, float(values.get("live_fee_rate_estimate") or 0.0))
            floor = max(configured_floor, roundtrip_fee)
            small_loss = max(0.0, float(values.get("small_loss_tolerance_ratio") or 0.0))
            opposite = str(exit_signal or "HOLD").upper() == "SELL"
            noise_band = (-small_loss) <= pnl_ratio < floor
            triggered = opposite and not noise_band
            reason = "exit by opposite cross" if triggered else ("opposite cross deferred: pnl in noise band" if opposite and noise_band else "opposite cross not triggered")
            context = {"unrealized_pnl_ratio": pnl_ratio, "min_profit_floor": floor, "roundtrip_fee_ratio": roundtrip_fee, "small_loss_tolerance_ratio": small_loss, "opposite_cross_triggered": opposite, "filter_applied": opposite and noise_band}
        elif rule == "max_holding_time":
            threshold = float(dict(policy.get("max_holding_time") or {}).get("max_holding_min") or 0.0) * 60.0
            holding = position.holding_duration(candle_ts)
            triggered = threshold > 0.0 and holding >= threshold
            reason = "exit by max holding time" if triggered else "max holding time not triggered"
            context = {"holding_time_sec": holding, "threshold_sec": threshold}
        else:  # materialization rejects this; keep the evaluator fail-closed.
            raise ValueError(f"unknown exit rule={rule!r}")
        evaluation = {"rule": rule, "triggered": triggered, "reason": reason, "context": context}
        evaluations.append(evaluation)
        if triggered:
            return ResearchExitDecision(True, rule, reason, tuple(evaluations))
    return ResearchExitDecision(False, None, "no exit rule triggered", tuple(evaluations))
