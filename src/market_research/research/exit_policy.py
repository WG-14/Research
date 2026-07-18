"""Strategy-neutral typed exit-policy evaluation."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import SupportsFloat, SupportsIndex, cast

from .exit_decision import ExitDecision
from .portfolio_view import ReadOnlyPortfolioView


class GenericExitPolicyEvaluator:
    def evaluate(
        self,
        *,
        policy: dict[str, object],
        portfolio: ReadOnlyPortfolioView,
        market_price: float,
        event_ts: int,
    ) -> ExitDecision:
        if portfolio.filled_position_qty <= 0 or portfolio.average_cost is None:
            return ExitDecision(False, None, "no_open_position", {})
        pnl_ratio = (
            float(market_price) - portfolio.average_cost
        ) / portfolio.average_cost
        raw_rules = policy.get("rules")
        rules: Iterable[object] = (
            raw_rules
            if isinstance(raw_rules, Iterable)
            and not isinstance(raw_rules, (str, bytes, Mapping))
            else ()
        )
        for raw_name in rules:
            name = str(raw_name)
            raw_config = policy.get(name)
            config = raw_config if isinstance(raw_config, Mapping) else {}
            if name == "stop_loss":
                threshold = _float_or_zero(config.get("stop_loss_ratio"))
                triggered = threshold > 0 and pnl_ratio <= -threshold
            elif name == "take_profit":
                threshold = _float_or_zero(config.get("take_profit_ratio"))
                triggered = threshold > 0 and pnl_ratio >= threshold
            elif name in {"max_holding_time", "time_exit"}:
                max_holding = policy.get("max_holding_time")
                max_holding_config = (
                    max_holding if isinstance(max_holding, Mapping) else {}
                )
                threshold = (
                    int(_float_or_zero(max_holding_config.get("max_holding_min")))
                    * 60_000
                )
                triggered = bool(
                    threshold
                    and portfolio.effective_entry_ts is not None
                    and int(event_ts) - portfolio.effective_entry_ts >= threshold
                )
            else:
                raise ValueError(f"unsupported_generic_exit_rule:{name}")
            evidence = {"pnl_ratio": pnl_ratio, "threshold": threshold}
            if triggered:
                return ExitDecision(True, name, f"exit_by_{name}", evidence)
        return ExitDecision(
            False, None, "no_exit_rule_triggered", {"pnl_ratio": pnl_ratio}
        )


def _float_or_zero(value: object) -> float:
    if value is None:
        return 0.0
    numeric = cast(str | bytes | bytearray | SupportsFloat | SupportsIndex, value)
    try:
        return float(numeric)
    except (TypeError, ValueError):
        return 0.0
