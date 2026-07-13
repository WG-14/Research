from __future__ import annotations

from typing import Any
from dataclasses import replace

from ..backtest_types import BacktestRunContext
from ..strategy_contract import (
    ResearchDataRequirement,
    ResearchStrategyDataRequirements,
    ResearchStrategyPlugin,
)
from ..strategy_spec import SMA_WITH_FILTER_SPEC, materialize_strategy_parameters
from .sma_with_filter_events import build_sma_with_filter_research_events
from ..exit_rules import materialize_sma_exit_policy, evaluate_sma_exit_policy
from ..exit_decision import ExitDecision
from ..position_model import ResearchPosition


def _materialize(*, plugin: ResearchStrategyPlugin, parameter_values: dict[str, Any], fee_rate: float, slippage_bps: float, context: BacktestRunContext | None = None) -> dict[str, Any]:
    del plugin, context
    values = materialize_strategy_parameters("sma_with_filter", parameter_values, fee_rate=fee_rate, slippage_bps=slippage_bps)
    for key, value in {
        "SMA_FILTER_GAP_MIN_RATIO": 0.0,
        "SMA_FILTER_VOL_MIN_RANGE_RATIO": 0.0,
        "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO": 0.0,
        "SMA_COST_EDGE_ENABLED": False,
        "SMA_MARKET_REGIME_ENABLED": False,
    }.items():
        if key not in parameter_values:
            values[key] = value
    return values


def _requirements(strategy_spec: object | None = None) -> ResearchStrategyDataRequirements:
    params = dict(getattr(strategy_spec, "parameters", {}) or {})
    lookback = max(int(params.get("SMA_LONG", 30)), int(params.get("SMA_FILTER_VOL_WINDOW", 10)), int(params.get("SMA_FILTER_OVEREXT_LOOKBACK", 3)) + 1) + 2
    return ResearchStrategyDataRequirements(
        required_data=SMA_WITH_FILTER_SPEC.required_data,
        optional_data=SMA_WITH_FILTER_SPEC.optional_data,
        capabilities=(
            ResearchDataRequirement("candles", min_coverage_pct=100.0, source="sqlite_candles", lookback_rows=lookback),
            ResearchDataRequirement("top_of_book", required=False),
        ),
    )


def _exit_policy_materializer(strategy_name: str, parameter_values: dict[str, Any]) -> dict[str, object]:
    return materialize_sma_exit_policy(strategy_name, parameter_values)


def _exit_decision(*, policy: dict[str, object], portfolio: Any, event: Any, market_price: float) -> ExitDecision:
    position = ResearchPosition(cash=portfolio.cash, asset_qty=portfolio.filled_position_qty,
        entry_price=portfolio.average_cost, entry_ts=portfolio.effective_entry_ts,
        sellable_qty=portfolio.filled_position_qty)
    result = evaluate_sma_exit_policy(policy=policy, position=position, candle_ts=int(event.decision_ts),
        market_price=float(market_price), exit_signal=str(event.exit_signal or "HOLD"),
        feature_state=event.feature_snapshot)
    return ExitDecision(result.triggered, result.rule, result.reason, {"evaluations": result.evaluations})


class _SmaRuntime:
    def __init__(self, *, compiled_contract: Any, execution_timing_policy: Any, portfolio_policy: Any,
                 fee_rate: float, slippage_bps: float) -> None:
        self.parameters = dict(compiled_contract.materialized_parameters)
        self.timing, self.portfolio_policy = execution_timing_policy, portfolio_policy
        self.fee_rate, self.slippage_bps = fee_rate, slippage_bps
        self.window = max(int(self.parameters["SMA_LONG"]),
            int(self.parameters.get("SMA_FILTER_VOL_WINDOW") or 1),
            int(self.parameters.get("SMA_FILTER_OVEREXT_LOOKBACK") or 1)) + 2

    def initialize(self, context: Any) -> dict[str, object]:
        return {}

    def on_market_event(self, market: Any, portfolio: Any, state: Any) -> tuple[Any, ...]:
        snapshot = market.causal_snapshot()
        offset = max(0, len(snapshot.candles) - self.window)
        if offset:
            snapshot = replace(snapshot, candles=snapshot.candles[offset:],
                top_of_book_quotes=snapshot.top_of_book_quotes[offset:])
        events = build_sma_with_filter_research_events(dataset=snapshot, parameter_values=self.parameters,
            fee_rate=self.fee_rate, slippage_bps=self.slippage_bps,
            execution_timing_policy=self.timing, portfolio_policy=self.portfolio_policy)
        return tuple(event for event in events if event.candle_ts == market.current_candle.ts)


def _runtime_factory(**values: Any) -> _SmaRuntime:
    values.pop("context", None)
    return _SmaRuntime(**values)


def build_sma_with_filter_plugin() -> ResearchStrategyPlugin:
    plugin = ResearchStrategyPlugin(
        name=SMA_WITH_FILTER_SPEC.strategy_name,
        version=SMA_WITH_FILTER_SPEC.strategy_version,
        spec=SMA_WITH_FILTER_SPEC,
        required_data=SMA_WITH_FILTER_SPEC.required_data,
        optional_data=SMA_WITH_FILTER_SPEC.optional_data,
        event_builder=build_sma_with_filter_research_events,
        parameter_materializer=_materialize,
        decision_contract_version=SMA_WITH_FILTER_SPEC.decision_contract_version,
        diagnostics_namespace="sma_with_filter",
        data_requirements_builder=_requirements,
        exit_policy_materializer=_exit_policy_materializer,
        exit_decision_builder=_exit_decision,
        exit_mode="strategy_owned",
        runtime_factory=_runtime_factory,
    )
    return plugin
