"""Research-only declaration for the threshold strategy."""

from __future__ import annotations

from typing import Any

from ..backtest_types import BacktestRunContext
from ..strategy_contract import ResearchStrategyPlugin
from ..strategy_spec import THRESHOLD_RESEARCH_ONLY_SPEC, materialize_strategy_parameters
from .threshold_research_only_events import build_threshold_research_only_events


class _ThresholdRuntime:
    def __init__(self, *, compiled_contract: Any, execution_timing_policy: Any, portfolio_policy: Any,
                 fee_rate: float, slippage_bps: float) -> None:
        self.parameters = dict(compiled_contract.materialized_parameters)
        self.timing = execution_timing_policy
        self.portfolio_policy = portfolio_policy
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps

    def initialize(self, context: Any) -> dict[str, object]:
        return {}

    def on_market_event(self, market: Any, portfolio: Any, state: Any) -> tuple[Any, ...]:
        events = build_threshold_research_only_events(dataset=market.causal_snapshot(),
            parameter_values=self.parameters, fee_rate=self.fee_rate, slippage_bps=self.slippage_bps,
            execution_timing_policy=self.timing, portfolio_policy=self.portfolio_policy)
        current = tuple(event for event in events if event.candle_ts == market.current_candle.ts)
        if portfolio.filled_position_qty > 0 or portfolio.pending_execution_count > 0:
            return ()
        return current


def _runtime_factory(**values: Any) -> _ThresholdRuntime:
    values.pop("context", None)
    return _ThresholdRuntime(**values)


def _materialize(
    *,
    plugin: ResearchStrategyPlugin,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    context: BacktestRunContext | None = None,
) -> dict[str, Any]:
    del plugin, context
    return materialize_strategy_parameters(
        "threshold_research_only",
        parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )


def build_threshold_research_only_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(
        name=THRESHOLD_RESEARCH_ONLY_SPEC.strategy_name,
        version=THRESHOLD_RESEARCH_ONLY_SPEC.strategy_version,
        spec=THRESHOLD_RESEARCH_ONLY_SPEC,
        required_data=THRESHOLD_RESEARCH_ONLY_SPEC.required_data,
        optional_data=THRESHOLD_RESEARCH_ONLY_SPEC.optional_data,
        event_builder=build_threshold_research_only_events,
        parameter_materializer=_materialize,
        decision_contract_version=(
            THRESHOLD_RESEARCH_ONLY_SPEC.decision_contract_version
        ),
        diagnostics_namespace="threshold_research_only",
        runtime_factory=_runtime_factory,
    )
