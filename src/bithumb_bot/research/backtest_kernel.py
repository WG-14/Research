"""Research-only generic event runner boundary.

The supported strategies own their concrete backtest kernels.  This helper is
kept for research callers that supply an explicit event stream, without any
runtime planner, broker, or submit-plan dependency.
"""

from __future__ import annotations

from typing import Any


def run_decision_event_backtest(*, strategy_name: str, **kwargs: Any) -> Any:
    from .strategy_catalog import resolve_research_strategy

    plugin = resolve_research_strategy(strategy_name)
    return plugin.runner(
        kwargs["dataset"],
        dict(kwargs.get("parameter_values") or {}),
        float(kwargs.get("fee_rate") or 0.0),
        float(kwargs.get("slippage_bps") or 0.0),
        kwargs.get("parameter_stability_score"),
        kwargs.get("execution_model"),
        kwargs.get("execution_timing_policy"),
        kwargs.get("portfolio_policy"),
        kwargs.get("context"),
    )
