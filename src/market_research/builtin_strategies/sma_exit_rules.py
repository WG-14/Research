"""SMA research exit policy; deliberately independent of runtime strategy modules."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, SupportsFloat, SupportsIndex, cast

from market_research.research.hashing import sha256_prefixed
from market_research.research.position_model import ResearchPosition


@dataclass(frozen=True, slots=True)
class ResearchExitDecision:
    triggered: bool
    rule: str | None
    reason: str
    evaluations: tuple[dict[str, object], ...]


def materialize_sma_exit_policy(
    strategy_name: str, parameter_values: dict[str, Any]
) -> dict[str, object]:
    names = tuple(
        item.strip().lower()
        for item in str(
            parameter_values.get("STRATEGY_EXIT_RULES")
            or "stop_loss,opposite_cross,max_holding_time"
        ).split(",")
        if item.strip()
    )
    allowed = {
        "stop_loss",
        "take_profit",
        "edge_invalidation",
        "opposite_cross",
        "max_holding_time",
    }
    unknown = sorted(set(names) - allowed)
    if unknown:
        raise ValueError(f"unknown exit rule={unknown[0]!r}")
    policy = {
        "schema_version": 1,
        "strategy_name": strategy_name,
        "rules": list(names),
        "stop_loss": {
            "stop_loss_ratio": float(
                parameter_values.get("STRATEGY_EXIT_STOP_LOSS_RATIO") or 0.0
            )
        },
        "take_profit": {
            "take_profit_ratio": float(
                parameter_values.get("STRATEGY_EXIT_TAKE_PROFIT_RATIO")
                or parameter_values.get("STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO")
                or 0.0
            )
        },
        "edge_invalidation": {
            "min_edge_ratio": float(
                parameter_values.get("STRATEGY_EXIT_MIN_EDGE_RATIO") or 0.0
            )
        },
        "max_holding_time": {
            "max_holding_min": int(
                parameter_values.get("STRATEGY_EXIT_MAX_HOLDING_MIN") or 0
            )
        },
        "opposite_cross": {
            "min_take_profit_ratio": float(
                parameter_values.get("STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO") or 0.0
            ),
            "small_loss_tolerance_ratio": float(
                parameter_values.get("STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO") or 0.0
            ),
            "live_fee_rate_estimate": float(
                parameter_values.get("LIVE_FEE_RATE_ESTIMATE") or 0.0
            ),
        },
        "parameter_source": "research_materialized_parameters",
    }
    return {
        "exit_policy": policy,
        "exit_policy_config": dict(policy),
        "exit_policy_hash": sha256_prefixed(policy),
        "exit_policy_config_hash": sha256_prefixed(policy),
        "exit_policy_contract_hash": sha256_prefixed(
            {"schema_version": 1, "strategy_name": strategy_name, "rules": list(names)}
        ),
        "exit_policy_source": (
            "market_research.builtin_strategies.sma_exit_rules."
            "materialize_sma_exit_policy"
        ),
        "exit_policy_materialization_mode": "research_only",
    }


def evaluate_sma_exit_policy(
    *,
    policy: dict[str, object],
    position: ResearchPosition,
    candle_ts: int,
    market_price: float,
    exit_signal: str,
    feature_state: dict[str, object] | None = None,
) -> ResearchExitDecision:
    if not position.in_position:
        return ResearchExitDecision(False, None, "no open position for exit policy", ())
    evaluations: list[dict[str, object]] = []
    pnl_ratio = position.unrealized_pnl_ratio(market_price)
    raw_rules = policy.get("rules")
    rules: Iterable[object] = (
        raw_rules
        if isinstance(raw_rules, Iterable)
        and not isinstance(raw_rules, (str, bytes, Mapping))
        else ()
    )
    for name in rules:
        rule = str(name)
        if rule == "stop_loss":
            threshold = _float_or_zero(
                _mapping(policy.get("stop_loss")).get("stop_loss_ratio")
            )
            triggered = threshold > 0.0 and pnl_ratio <= -threshold
            reason = "exit by stop loss" if triggered else "stop loss not triggered"
            context = {"threshold_ratio": threshold, "unrealized_pnl_ratio": pnl_ratio}
        elif rule == "take_profit":
            threshold = _float_or_zero(
                _mapping(policy.get("take_profit")).get("take_profit_ratio")
            )
            triggered = threshold > 0.0 and pnl_ratio >= threshold
            reason = "exit by take profit" if triggered else "take profit not triggered"
            context = {"threshold_ratio": threshold, "unrealized_pnl_ratio": pnl_ratio}
        elif rule == "edge_invalidation":
            threshold = _float_or_zero(
                _mapping(policy.get("edge_invalidation")).get("min_edge_ratio")
            )
            gap = _float_or_zero((feature_state or {}).get("gap_ratio"))
            triggered = threshold > 0.0 and gap < threshold
            reason = "exit by edge invalidation" if triggered else "edge remains valid"
            context = {"minimum_edge_ratio": threshold, "current_edge_ratio": gap}
        elif rule in {"opposite_cross", "crossover"}:
            values = _mapping(policy.get("opposite_cross"))
            configured_floor = max(
                0.0, _float_or_zero(values.get("min_take_profit_ratio"))
            )
            roundtrip_fee = 2.0 * max(
                0.0, _float_or_zero(values.get("live_fee_rate_estimate"))
            )
            floor = max(configured_floor, roundtrip_fee)
            small_loss = max(
                0.0, _float_or_zero(values.get("small_loss_tolerance_ratio"))
            )
            opposite = str(exit_signal or "HOLD").upper() == "SELL"
            noise_band = (-small_loss) <= pnl_ratio < floor
            triggered = opposite and not noise_band
            reason = (
                "exit by opposite cross"
                if triggered
                else (
                    "opposite cross deferred: pnl in noise band"
                    if opposite and noise_band
                    else "opposite cross not triggered"
                )
            )
            context = {
                "unrealized_pnl_ratio": pnl_ratio,
                "min_profit_floor": floor,
                "roundtrip_fee_ratio": roundtrip_fee,
                "small_loss_tolerance_ratio": small_loss,
                "opposite_cross_triggered": opposite,
                "filter_applied": opposite and noise_band,
            }
        elif rule in {"max_holding_time", "time_exit"}:
            threshold = (
                _float_or_zero(
                    _mapping(policy.get("max_holding_time")).get("max_holding_min")
                )
                * 60.0
            )
            holding = position.holding_duration(candle_ts)
            triggered = threshold > 0.0 and holding >= threshold
            reason = (
                "exit by max holding time"
                if triggered
                else "max holding time not triggered"
            )
            context = {"holding_time_sec": holding, "threshold_sec": threshold}
        else:  # materialization rejects this; keep the evaluator fail-closed.
            raise ValueError(f"unknown exit rule={rule!r}")
        evaluation = {
            "rule": rule,
            "triggered": triggered,
            "reason": reason,
            "context": context,
        }
        evaluations.append(evaluation)
        if triggered:
            return ResearchExitDecision(True, rule, reason, tuple(evaluations))
    return ResearchExitDecision(
        False, None, "no exit rule triggered", tuple(evaluations)
    )


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _float_or_zero(value: object) -> float:
    if value is None:
        return 0.0
    numeric = cast(str | bytes | bytearray | SupportsFloat | SupportsIndex, value)
    try:
        return float(numeric)
    except (TypeError, ValueError):
        return 0.0
