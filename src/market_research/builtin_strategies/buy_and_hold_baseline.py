"""Built-in buy-and-hold plugin implementation."""
from typing import Any

from market_research.research.backtest_types import BacktestRunContext
from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import StrategySpec, materialize_parameters_from_spec

BUY_AND_HOLD_BASELINE_SPEC = StrategySpec(
    strategy_name="buy_and_hold_baseline", strategy_version="buy_and_hold_baseline.research_contract.v1",
    accepted_parameter_names=("BUY_HOLD_BUY_INDEX", "BUY_HOLD_DECISION_REASON"),
    required_parameter_names=("BUY_HOLD_BUY_INDEX",),
    behavior_affecting_parameter_names=("BUY_HOLD_BUY_INDEX", "BUY_HOLD_DECISION_REASON"),
    metadata_only_parameter_names=(), research_only_parameter_names=(),
    default_parameters={"BUY_HOLD_DECISION_REASON": "buy_and_hold_architecture_canary"},
    decision_contract_version="research_buy_and_hold_baseline_decision_contract.v1", required_data=("candles",),
    optional_data=(), exit_policy_schema={"schema_version": 1, "rules": (),
        "description": "Executable canary emits one BUY intent, then HOLD decisions."})
from .buy_and_hold_baseline_events import build_buy_and_hold_baseline_events


def _materialize(*, plugin: ResearchStrategyPlugin, parameter_values: dict[str, Any], fee_rate: float,
                 slippage_bps: float, context: BacktestRunContext | None = None) -> dict[str, Any]:
    del plugin, context
    return materialize_parameters_from_spec(BUY_AND_HOLD_BASELINE_SPEC, parameter_values,
                                            fee_rate=fee_rate, slippage_bps=slippage_bps)


def build_buy_and_hold_baseline_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(name=BUY_AND_HOLD_BASELINE_SPEC.strategy_name,
        version=BUY_AND_HOLD_BASELINE_SPEC.strategy_version, spec=BUY_AND_HOLD_BASELINE_SPEC,
        required_data=BUY_AND_HOLD_BASELINE_SPEC.required_data,
        optional_data=BUY_AND_HOLD_BASELINE_SPEC.optional_data,
        event_builder=build_buy_and_hold_baseline_events, parameter_materializer=_materialize,
        decision_contract_version=BUY_AND_HOLD_BASELINE_SPEC.decision_contract_version,
        diagnostics_namespace="buy_and_hold_baseline",
        reconstruction_module=__name__, reconstruction_qualname="build_buy_and_hold_baseline_plugin")

__all__ = ["build_buy_and_hold_baseline_plugin"]
