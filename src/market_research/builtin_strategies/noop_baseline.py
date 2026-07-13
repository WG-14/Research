"""Built-in noop plugin implementation."""
from dataclasses import replace
from typing import Any

from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import StrategySpec

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


class _NoopRuntime:
    def __init__(self, *, compiled_contract: Any, execution_timing_policy: Any,
                 portfolio_policy: Any, fee_rate: float, slippage_bps: float) -> None:
        self.parameters = dict(compiled_contract.materialized_parameters)
        self.timing, self.portfolio_policy = execution_timing_policy, portfolio_policy
        self.fee_rate, self.slippage_bps = fee_rate, slippage_bps

    def initialize(self, context: Any) -> dict[str, object]:
        return {}

    def on_market_event(self, market: Any, portfolio: Any, state: Any) -> tuple[Any, ...]:
        snapshot = market.causal_snapshot()
        current = replace(snapshot, candles=(market.current_candle,),
                          top_of_book_quotes=snapshot.top_of_book_quotes[-1:])
        events = build_noop_baseline_events(dataset=current,
            parameter_values=self.parameters, fee_rate=self.fee_rate, slippage_bps=self.slippage_bps,
            execution_timing_policy=self.timing, portfolio_policy=self.portfolio_policy,
            candle_index_offset=market.current_index)
        return events


def _runtime_factory(**values: Any) -> _NoopRuntime:
    values.pop("context", None)
    return _NoopRuntime(**values)


def build_noop_baseline_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(name=NOOP_BASELINE_SPEC.strategy_name,
        version=NOOP_BASELINE_SPEC.strategy_version, spec=NOOP_BASELINE_SPEC,
        required_data=NOOP_BASELINE_SPEC.required_data, optional_data=NOOP_BASELINE_SPEC.optional_data,
        event_builder=build_noop_baseline_events,
        decision_contract_version=NOOP_BASELINE_SPEC.decision_contract_version,
        diagnostics_namespace="noop_baseline", runtime_factory=_runtime_factory,
        reconstruction_module=__name__, reconstruction_qualname="build_noop_baseline_plugin")

__all__ = ["build_noop_baseline_plugin"]
