"""Compatibility entry point delegating to the common simulation engine."""

from __future__ import annotations

from typing import Any


def run_decision_event_backtest(
    *, strategy_name: str, strategy_registry: Any, **kwargs: Any
) -> Any:
    from .strategy_catalog import resolve_research_strategy

    plugin = resolve_research_strategy(strategy_name, registry=strategy_registry)
    from .simulation_engine import run_common_simulation_backtest

    return run_common_simulation_backtest(
        plugin=plugin,
        registry=strategy_registry,
        dataset=kwargs["dataset"],
        parameter_values=dict(kwargs.get("parameter_values") or {}),
        fee_rate=float(kwargs.get("fee_rate") or 0.0),
        slippage_bps=float(kwargs.get("slippage_bps") or 0.0),
        parameter_stability_score=kwargs.get("parameter_stability_score"),
        execution_model=kwargs.get("execution_model"),
        execution_timing_policy=kwargs.get("execution_timing_policy"),
        portfolio_policy=kwargs.get("portfolio_policy"),
        risk_policy=kwargs.get("risk_policy"),
        context=kwargs.get("context"),
    )
