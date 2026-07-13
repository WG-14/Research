"""Strategy-neutral typed exit-policy evaluation."""
from __future__ import annotations

from .exit_decision import ExitDecision
from .portfolio_view import ReadOnlyPortfolioView


class GenericExitPolicyEvaluator:
    def evaluate(self, *, policy: dict[str, object], portfolio: ReadOnlyPortfolioView,
                 market_price: float, event_ts: int) -> ExitDecision:
        if portfolio.filled_position_qty <= 0 or portfolio.average_cost is None:
            return ExitDecision(False, None, "no_open_position", {})
        pnl_ratio = (float(market_price) - portfolio.average_cost) / portfolio.average_cost
        for raw_name in policy.get("rules") or ():
            name = str(raw_name)
            if name == "stop_loss":
                threshold = float(dict(policy.get(name) or {}).get("stop_loss_ratio") or 0)
                triggered = threshold > 0 and pnl_ratio <= -threshold
            elif name == "take_profit":
                threshold = float(dict(policy.get(name) or {}).get("take_profit_ratio") or 0)
                triggered = threshold > 0 and pnl_ratio >= threshold
            elif name in {"max_holding_time", "time_exit"}:
                threshold = int(dict(policy.get("max_holding_time") or {}).get("max_holding_min") or 0) * 60_000
                triggered = bool(threshold and portfolio.effective_entry_ts is not None and
                                 int(event_ts) - portfolio.effective_entry_ts >= threshold)
            else:
                raise ValueError(f"unsupported_generic_exit_rule:{name}")
            evidence = {"pnl_ratio": pnl_ratio, "threshold": threshold}
            if triggered:
                return ExitDecision(True, name, f"exit_by_{name}", evidence)
        return ExitDecision(False, None, "no_exit_rule_triggered", {"pnl_ratio": pnl_ratio})
