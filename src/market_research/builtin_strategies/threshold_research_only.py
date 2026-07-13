"""Built-in threshold research plugin implementation."""
from typing import Any

from market_research.research.backtest_types import BacktestRunContext
from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import StrategySpec

THRESHOLD_RESEARCH_ONLY_SPEC = StrategySpec(
    strategy_name="threshold_research_only", strategy_version="threshold_research_only.research_contract.v1",
    accepted_parameter_names=("THRESHOLD_CLOSE_ABOVE",), required_parameter_names=("THRESHOLD_CLOSE_ABOVE",),
    behavior_affecting_parameter_names=("THRESHOLD_CLOSE_ABOVE",), metadata_only_parameter_names=(),
    research_only_parameter_names=(), default_parameters={},
    decision_contract_version="research_threshold_research_only_decision_contract.v1", required_data=("candles",),
    optional_data=(), exit_policy_schema={"schema_version": 1, "rules": (),
        "description": "Research-only threshold strategy with no explicit exit."})
from .threshold_research_only_events import build_threshold_research_only_events


class _ThresholdRuntime:
    def __init__(self, *, compiled_contract: Any, execution_timing_policy: Any, portfolio_policy: Any,
                 fee_rate: float, slippage_bps: float) -> None:
        self.parameters = dict(compiled_contract.materialized_parameters)
        self.timing, self.portfolio_policy = execution_timing_policy, portfolio_policy
        self.fee_rate, self.slippage_bps = fee_rate, slippage_bps

    def initialize(self, context: Any) -> dict[str, object]:
        return {}

    def on_market_event(self, market: Any, portfolio: Any, state: Any) -> tuple[Any, ...]:
        events = build_threshold_research_only_events(dataset=market.causal_snapshot(),
            parameter_values=self.parameters, fee_rate=self.fee_rate, slippage_bps=self.slippage_bps,
            execution_timing_policy=self.timing, portfolio_policy=self.portfolio_policy)
        current = tuple(event for event in events if event.candle_ts == market.current_candle.ts)
        return () if portfolio.filled_position_qty > 0 or portfolio.pending_execution_count > 0 else current


def _runtime_factory(**values: Any) -> _ThresholdRuntime:
    values.pop("context", None)
    return _ThresholdRuntime(**values)


def _materialize(*, plugin: ResearchStrategyPlugin, parameter_values: dict[str, Any], fee_rate: float,
                 slippage_bps: float, materialized_parameters: dict[str, Any],
                 context: BacktestRunContext | None = None) -> dict[str, Any]:
    del plugin, parameter_values, context, fee_rate, slippage_bps
    return dict(materialized_parameters)


def build_threshold_research_only_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(name=THRESHOLD_RESEARCH_ONLY_SPEC.strategy_name,
        version=THRESHOLD_RESEARCH_ONLY_SPEC.strategy_version, spec=THRESHOLD_RESEARCH_ONLY_SPEC,
        required_data=THRESHOLD_RESEARCH_ONLY_SPEC.required_data,
        optional_data=THRESHOLD_RESEARCH_ONLY_SPEC.optional_data,
        event_builder=build_threshold_research_only_events, parameter_materializer=_materialize,
        decision_contract_version=THRESHOLD_RESEARCH_ONLY_SPEC.decision_contract_version,
        diagnostics_namespace="threshold_research_only", runtime_factory=_runtime_factory,
        reconstruction_module=__name__, reconstruction_qualname="build_threshold_research_only_plugin")

__all__ = ["build_threshold_research_only_plugin"]
