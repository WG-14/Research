from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from bithumb_bot.core.sma_policy import MarketWindow, PositionSnapshot

from .base import PositionContext


@dataclass(frozen=True)
class ExitRuleDecision:
    should_exit: bool
    reason: str
    context: dict[str, object]


@dataclass(frozen=True)
class ExitPolicyConfig:
    rule_names: tuple[str, ...]
    stop_loss_ratio: float
    max_holding_sec: float
    min_take_profit_ratio: float
    small_loss_tolerance_ratio: float
    live_fee_rate_estimate: float

    def policy_input_payload(self) -> dict[str, object]:
        return {
            "rule_names": list(self.rule_names),
            "stop_loss_ratio": float(self.stop_loss_ratio),
            "max_holding_sec": float(self.max_holding_sec),
            "min_take_profit_ratio": float(self.min_take_profit_ratio),
            "small_loss_tolerance_ratio": float(self.small_loss_tolerance_ratio),
            "live_fee_rate_estimate": float(self.live_fee_rate_estimate),
        }


@dataclass(frozen=True)
class ExitDecision:
    triggered: bool
    final_signal: str
    rule: str | None
    reason: str
    evaluations: tuple[dict[str, object], ...]


class ExitRule(Protocol):
    name: str

    def evaluate(
        self,
        *,
        position: PositionContext,
        candle_ts: int,
        market_price: float,
        signal_context: dict[str, object],
    ) -> ExitRuleDecision: ...


@dataclass(frozen=True)
class OppositeCrossExitRule:
    min_take_profit_ratio: float = 0.0
    live_fee_rate_estimate: float = 0.0
    small_loss_tolerance_ratio: float = 0.0
    name: str = "opposite_cross"

    def evaluate(
        self,
        *,
        position: PositionContext,
        candle_ts: int,
        market_price: float,
        signal_context: dict[str, object],
    ) -> ExitRuleDecision:
        base_signal = str(signal_context.get("base_signal", "HOLD"))
        opposite_cross_triggered = bool(position.in_position and base_signal == "SELL")
        min_profit_floor = max(
            max(0.0, float(self.min_take_profit_ratio)),
            2.0 * max(0.0, float(self.live_fee_rate_estimate)),
        )
        unrealized_pnl_ratio = float(position.unrealized_pnl_ratio)
        resolved_small_loss_tolerance = max(0.0, float(self.small_loss_tolerance_ratio))
        is_small_loss = (-resolved_small_loss_tolerance) <= unrealized_pnl_ratio < 0.0
        is_small_gain = 0.0 <= unrealized_pnl_ratio < min_profit_floor
        filtered_by_pnl_floor = bool(opposite_cross_triggered and (is_small_loss or is_small_gain))
        filter_zone = "small_loss" if is_small_loss else "small_gain" if is_small_gain else "outside"

        should_exit = bool(opposite_cross_triggered and not filtered_by_pnl_floor)
        if should_exit:
            reason = "exit by opposite cross"
        elif filtered_by_pnl_floor:
            reason = f"opposite cross deferred: pnl in {filter_zone} noise band"
        else:
            reason = "opposite cross not triggered"
        return ExitRuleDecision(
            should_exit=should_exit,
            reason=reason,
            context={
                "rule": self.name,
                "base_signal": base_signal,
                "opposite_cross_triggered": opposite_cross_triggered,
                "filter_applied": filtered_by_pnl_floor,
                "deferred_by_min_take_profit_floor": filtered_by_pnl_floor,
                "unrealized_pnl_ratio": unrealized_pnl_ratio,
                "min_profit_floor": min_profit_floor,
                "required_take_profit_ratio": min_profit_floor,
                "configured_min_take_profit_ratio": max(0.0, float(self.min_take_profit_ratio)),
                "roundtrip_fee_ratio": 2.0 * max(0.0, float(self.live_fee_rate_estimate)),
                "small_loss_tolerance_ratio": resolved_small_loss_tolerance,
                "small_loss_tolerance_configured_ratio": max(
                    0.0, float(self.small_loss_tolerance_ratio)
                ),
                "small_loss_zone": is_small_loss,
                "small_gain_zone": is_small_gain,
                "filter_zone": filter_zone,
                "profit_floor_basis": {
                    "configured_min_take_profit_ratio": max(0.0, float(self.min_take_profit_ratio)),
                    "roundtrip_fee_ratio": 2.0 * max(0.0, float(self.live_fee_rate_estimate)),
                    "effective_min_profit_floor_ratio": min_profit_floor,
                },
                "candle_ts": int(candle_ts),
                "market_price": float(market_price),
            },
        )


@dataclass(frozen=True)
class StopLossExitRule:
    stop_loss_ratio: float
    name: str = "stop_loss"

    def __post_init__(self) -> None:
        value = float(self.stop_loss_ratio)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"stop_loss_ratio must be finite and >= 0, got {value!r}")

    def evaluate(
        self,
        *,
        position: PositionContext,
        candle_ts: int,
        market_price: float,
        signal_context: dict[str, object],
    ) -> ExitRuleDecision:
        threshold = float(self.stop_loss_ratio)
        unrealized_pnl_ratio = float(position.unrealized_pnl_ratio)
        should_exit = bool(
            position.in_position
            and threshold > 0.0
            and unrealized_pnl_ratio <= -threshold
        )
        return ExitRuleDecision(
            should_exit=should_exit,
            reason="exit by stop loss" if should_exit else "stop loss not triggered",
            context={
                "rule": self.name,
                "threshold_ratio": threshold,
                "unrealized_pnl_ratio": unrealized_pnl_ratio,
                "base_signal": str(signal_context.get("base_signal", "HOLD")),
                "raw_signal": str(signal_context.get("raw_signal", signal_context.get("base_signal", "HOLD"))),
                "entry_signal": str(signal_context.get("entry_signal", "HOLD")),
                "exit_signal": str(signal_context.get("exit_signal", signal_context.get("base_signal", "HOLD"))),
                "candle_ts": int(candle_ts),
                "market_price": float(market_price),
            },
        )


@dataclass(frozen=True)
class MaxHoldingTimeExitRule:
    max_holding_sec: float
    name: str = "max_holding_time"

    def evaluate(
        self,
        *,
        position: PositionContext,
        candle_ts: int,
        market_price: float,
        signal_context: dict[str, object],
    ) -> ExitRuleDecision:
        threshold = max(0.0, float(self.max_holding_sec))
        should_exit = bool(
            position.in_position
            and threshold > 0
            and float(position.holding_time_sec) >= threshold
        )
        return ExitRuleDecision(
            should_exit=should_exit,
            reason="exit by max holding time" if should_exit else "max holding time not triggered",
            context={
                "rule": self.name,
                "holding_time_sec": float(position.holding_time_sec),
                "threshold_sec": threshold,
                "candle_ts": int(candle_ts),
                "market_price": float(market_price),
            },
        )


@dataclass(frozen=True)
class TakeProfitExitRule:
    take_profit_ratio: float
    name: str = "take_profit"

    def __post_init__(self) -> None:
        value = float(self.take_profit_ratio)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"take_profit_ratio must be finite and >= 0, got {value!r}")

    def evaluate(
        self,
        *,
        position: PositionContext,
        candle_ts: int,
        market_price: float,
        signal_context: dict[str, object],
    ) -> ExitRuleDecision:
        threshold = float(self.take_profit_ratio)
        unrealized_pnl_ratio = float(position.unrealized_pnl_ratio)
        should_exit = bool(
            position.in_position
            and threshold > 0.0
            and unrealized_pnl_ratio >= threshold
        )
        return ExitRuleDecision(
            should_exit=should_exit,
            reason="exit by take profit" if should_exit else "take profit not triggered",
            context={
                "rule": self.name,
                "threshold_ratio": threshold,
                "unrealized_pnl_ratio": unrealized_pnl_ratio,
                "candle_ts": int(candle_ts),
                "market_price": float(market_price),
            },
        )


def create_exit_rules(
    *,
    rule_names: list[str],
    max_holding_sec: float,
    stop_loss_ratio: float = 0.0,
    take_profit_ratio: float = 0.0,
) -> list[ExitRule]:
    rules: list[ExitRule] = []
    priority = {"stop_loss": 0, "take_profit": 1, "max_holding_time": 2}
    normalized_names = [str(raw_name).strip().lower() for raw_name in rule_names if str(raw_name).strip()]
    unknown = [name for name in normalized_names if name not in priority]
    if unknown:
        raise ValueError(f"unknown exit rule={unknown[0]!r}")
    resolved_stop_loss_ratio = float(stop_loss_ratio)
    if not math.isfinite(resolved_stop_loss_ratio) or resolved_stop_loss_ratio < 0.0:
        raise ValueError(f"stop_loss_ratio must be finite and >= 0, got {resolved_stop_loss_ratio!r}")
    if resolved_stop_loss_ratio > 0.0 and "stop_loss" not in normalized_names:
        raise ValueError("stop_loss_ratio is positive but STRATEGY_EXIT_RULES does not include stop_loss")
    resolved_take_profit_ratio = float(take_profit_ratio)
    if not math.isfinite(resolved_take_profit_ratio) or resolved_take_profit_ratio < 0.0:
        raise ValueError(f"take_profit_ratio must be finite and >= 0, got {resolved_take_profit_ratio!r}")
    if resolved_take_profit_ratio > 0.0 and "take_profit" not in normalized_names:
        raise ValueError("take_profit_ratio is positive but STRATEGY_EXIT_RULES does not include take_profit")
    for name in sorted(dict.fromkeys(normalized_names), key=lambda item: priority[item]):
        if name == "stop_loss":
            rules.append(StopLossExitRule(stop_loss_ratio=resolved_stop_loss_ratio))
        elif name == "take_profit":
            rules.append(TakeProfitExitRule(take_profit_ratio=resolved_take_profit_ratio))
        elif name == "max_holding_time":
            rules.append(MaxHoldingTimeExitRule(max_holding_sec=float(max_holding_sec)))
    return rules


def merge_exit_rules(
    common_exit_rules: list[ExitRule],
    strategy_exit_rules: list[ExitRule],
) -> list[ExitRule]:
    """Preserve common risk exits while allowing plugin-owned strategy exits."""
    if not strategy_exit_rules:
        return list(common_exit_rules)

    common_by_name = {rule.name: rule for rule in common_exit_rules}
    strategy_names = {rule.name for rule in strategy_exit_rules}

    if not any(name in common_by_name for name in strategy_names):
        return [*common_exit_rules, *strategy_exit_rules]

    merged: list[ExitRule] = []
    seen: set[str] = set()
    for rule in strategy_exit_rules:
        authoritative_rule = common_by_name.get(rule.name, rule)
        if authoritative_rule.name in seen:
            continue
        merged.append(authoritative_rule)
        seen.add(authoritative_rule.name)
    for rule in common_exit_rules:
        if rule.name in seen:
            continue
        merged.append(rule)
        seen.add(rule.name)
    return merged


def create_sma_exit_rules(
    *,
    rule_names: list[str],
    max_holding_sec: float,
    min_take_profit_ratio: float,
    live_fee_rate_estimate: float,
    small_loss_tolerance_ratio: float,
    stop_loss_ratio: float = 0.0,
) -> list[ExitRule]:
    rules: list[ExitRule] = []
    priority = {"stop_loss": 0, "opposite_cross": 1, "max_holding_time": 2}
    normalized_names = [str(raw_name).strip().lower() for raw_name in rule_names if str(raw_name).strip()]
    unknown = [name for name in normalized_names if name not in priority]
    if unknown:
        raise ValueError(f"unknown exit rule={unknown[0]!r}")
    common_names = [name for name in normalized_names if name in {"stop_loss", "max_holding_time"}]
    common_rules = create_exit_rules(
        rule_names=common_names,
        max_holding_sec=max_holding_sec,
        stop_loss_ratio=stop_loss_ratio,
    )
    common_by_name = {rule.name: rule for rule in common_rules}
    for name in sorted(dict.fromkeys(normalized_names), key=lambda item: priority[item]):
        if name == "opposite_cross":
            rules.append(
                OppositeCrossExitRule(
                    min_take_profit_ratio=float(min_take_profit_ratio),
                    live_fee_rate_estimate=float(live_fee_rate_estimate),
                    small_loss_tolerance_ratio=float(small_loss_tolerance_ratio),
                )
            )
        else:
            rules.append(common_by_name[name])
    return rules


def evaluate_sma_exit_policy(
    *,
    position: PositionSnapshot,
    market: MarketWindow,
    raw_signal: str,
    raw_reason: str,
    entry_signal: str,
    exit_signal: str,
    config: ExitPolicyConfig,
    signal_context_extra: dict[str, object] | None = None,
    rule_sources: dict[str, str] | None = None,
) -> ExitDecision:
    """Evaluate SMA exit rules from immutable strategy snapshots.

    Runtime and research callers share this DB-free wrapper so exit authority
    does not drift between orchestration paths.
    """
    if not position.in_position:
        return ExitDecision(
            triggered=False,
            final_signal=str(entry_signal or "HOLD").upper(),
            rule=None,
            reason="no open position for exit policy",
            evaluations=(),
        )

    rules = create_sma_exit_rules(
        rule_names=list(config.rule_names),
        max_holding_sec=float(config.max_holding_sec),
        min_take_profit_ratio=float(config.min_take_profit_ratio),
        live_fee_rate_estimate=float(config.live_fee_rate_estimate),
        small_loss_tolerance_ratio=float(config.small_loss_tolerance_ratio),
        stop_loss_ratio=float(config.stop_loss_ratio),
    )
    position_context = PositionContext(
        in_position=bool(position.in_position),
        entry_ts=position.entry_ts,
        entry_price=position.entry_price,
        qty_open=float(position.qty_open),
        holding_time_sec=float(position.holding_time_sec),
        unrealized_pnl=float(position.unrealized_pnl),
        unrealized_pnl_ratio=float(position.unrealized_pnl_ratio),
    )
    signal_context: dict[str, object] = {
        "base_signal": str(exit_signal or raw_signal or "HOLD").upper(),
        "base_reason": str(raw_reason or ""),
        "raw_signal": str(raw_signal or "HOLD").upper(),
        "entry_signal": str(entry_signal or "HOLD").upper(),
        "exit_signal": str(exit_signal or raw_signal or "HOLD").upper(),
        "curr_s": float(market.curr_s),
        "curr_l": float(market.curr_l),
    }
    if signal_context_extra:
        signal_context.update(dict(signal_context_extra))

    evaluations: list[dict[str, object]] = []
    for rule in rules:
        result = rule.evaluate(
            position=position_context,
            candle_ts=int(market.candle_ts),
            market_price=float(market.closes[-1]) if market.closes else 0.0,
            signal_context=signal_context,
        )
        evaluation = {
            "rule": rule.name,
            "triggered": bool(result.should_exit),
            "reason": result.reason,
            "context": result.context,
        }
        if rule_sources and rule.name in rule_sources:
            evaluation["rule_source"] = str(rule_sources[rule.name])
        evaluations.append(evaluation)
        if result.should_exit:
            return ExitDecision(
                triggered=True,
                final_signal="SELL",
                rule=rule.name,
                reason=result.reason,
                evaluations=tuple(evaluations),
            )

    return ExitDecision(
        triggered=False,
        final_signal="HOLD",
        rule=None,
        reason="no exit rule triggered",
        evaluations=tuple(evaluations),
    )
