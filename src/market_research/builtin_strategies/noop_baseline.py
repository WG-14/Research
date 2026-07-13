"""Built-in noop plugin implementation."""
from typing import Any

from market_research.research.backtest_types import BacktestRunContext
from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import StrategySpec, materialize_parameters_from_spec

NOOP_BASELINE_SPEC = StrategySpec(
    strategy_name="noop_baseline", strategy_version="noop_baseline.research_contract.v1",
    accepted_parameter_names=("NOOP_DECISION_START_INDEX", "NOOP_DECISION_REASON"), required_parameter_names=(),
    behavior_affecting_parameter_names=("NOOP_DECISION_START_INDEX", "NOOP_DECISION_REASON"),
    metadata_only_parameter_names=(), research_only_parameter_names=(),
    default_parameters={"NOOP_DECISION_START_INDEX": 0, "NOOP_DECISION_REASON": "noop_baseline_hold"},
    decision_contract_version="research_noop_baseline_decision_contract.v1", required_data=("candles",),
    optional_data=(), exit_policy_schema={"schema_version": 1, "rules": (),
        "description": "No-op baseline never emits executable entry or exit intent."})
from .noop_baseline_events import build_noop_baseline_events


def _materialize(*, plugin: ResearchStrategyPlugin, parameter_values: dict[str, Any], fee_rate: float,
                 slippage_bps: float, context: BacktestRunContext | None = None) -> dict[str, Any]:
    del plugin, context
    return materialize_parameters_from_spec(NOOP_BASELINE_SPEC, parameter_values,
                                            fee_rate=fee_rate, slippage_bps=slippage_bps)


def build_noop_baseline_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(name=NOOP_BASELINE_SPEC.strategy_name,
        version=NOOP_BASELINE_SPEC.strategy_version, spec=NOOP_BASELINE_SPEC,
        required_data=NOOP_BASELINE_SPEC.required_data, optional_data=NOOP_BASELINE_SPEC.optional_data,
        event_builder=build_noop_baseline_events, parameter_materializer=_materialize,
        decision_contract_version=NOOP_BASELINE_SPEC.decision_contract_version,
        diagnostics_namespace="noop_baseline",
        reconstruction_module=__name__, reconstruction_qualname="build_noop_baseline_plugin")

__all__ = ["build_noop_baseline_plugin"]
