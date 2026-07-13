"""Built-in buy-and-hold plugin implementation."""
from typing import Any

from market_research.research.backtest_types import BacktestRunContext
from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import BUY_AND_HOLD_BASELINE_SPEC, materialize_strategy_parameters
from market_research.research.strategies.buy_and_hold_baseline_events import build_buy_and_hold_baseline_events


def _materialize(*, plugin: ResearchStrategyPlugin, parameter_values: dict[str, Any], fee_rate: float,
                 slippage_bps: float, context: BacktestRunContext | None = None) -> dict[str, Any]:
    del plugin, context
    return materialize_strategy_parameters("buy_and_hold_baseline", parameter_values,
                                           fee_rate=fee_rate, slippage_bps=slippage_bps)


def build_buy_and_hold_baseline_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(name=BUY_AND_HOLD_BASELINE_SPEC.strategy_name,
        version=BUY_AND_HOLD_BASELINE_SPEC.strategy_version, spec=BUY_AND_HOLD_BASELINE_SPEC,
        required_data=BUY_AND_HOLD_BASELINE_SPEC.required_data,
        optional_data=BUY_AND_HOLD_BASELINE_SPEC.optional_data,
        event_builder=build_buy_and_hold_baseline_events, parameter_materializer=_materialize,
        decision_contract_version=BUY_AND_HOLD_BASELINE_SPEC.decision_contract_version,
        diagnostics_namespace="buy_and_hold_baseline")

__all__ = ["build_buy_and_hold_baseline_plugin"]
