"""Built-in buy-and-hold plugin implementation."""
from dataclasses import replace
from typing import Any

from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import StrategySpec

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


class _BuyAndHoldRuntime:
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
        events = build_buy_and_hold_baseline_events(dataset=current,
            parameter_values=self.parameters, fee_rate=self.fee_rate, slippage_bps=self.slippage_bps,
            execution_timing_policy=self.timing, portfolio_policy=self.portfolio_policy,
            candle_index_offset=market.current_index)
        return events


def _runtime_factory(**values: Any) -> _BuyAndHoldRuntime:
    values.pop("context", None)
    return _BuyAndHoldRuntime(**values)


def build_buy_and_hold_baseline_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(name=BUY_AND_HOLD_BASELINE_SPEC.strategy_name,
        version=BUY_AND_HOLD_BASELINE_SPEC.strategy_version, spec=BUY_AND_HOLD_BASELINE_SPEC,
        required_data=BUY_AND_HOLD_BASELINE_SPEC.required_data,
        optional_data=BUY_AND_HOLD_BASELINE_SPEC.optional_data,
        event_builder=build_buy_and_hold_baseline_events,
        decision_contract_version=BUY_AND_HOLD_BASELINE_SPEC.decision_contract_version,
        diagnostics_namespace="buy_and_hold_baseline", runtime_factory=_runtime_factory,
        reconstruction_module=__name__, reconstruction_qualname="build_buy_and_hold_baseline_plugin")

__all__ = ["build_buy_and_hold_baseline_plugin"]
