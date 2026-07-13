"""Built-in SMA-with-filter research plugin implementation."""
from dataclasses import replace
from typing import Any, Mapping

from market_research.research.backtest_types import BacktestRunContext
from market_research.research.exit_decision import ExitDecision
from .sma_exit_rules import evaluate_sma_exit_policy, materialize_sma_exit_policy
from market_research.research.position_model import ResearchPosition
from market_research.research.strategy_contract import (ResearchDataRequirement,
    ResearchStrategyDataRequirements, ResearchStrategyPlugin)
from market_research.research.strategy_spec import StrategyParameterSchema, StrategySpec

_SMA_ACCEPTED = (
    "SMA_SHORT", "SMA_LONG", "SMA_FILTER_GAP_MIN_RATIO", "SMA_FILTER_VOL_WINDOW",
    "SMA_FILTER_VOL_MIN_RANGE_RATIO", "SMA_FILTER_VOLUME_WINDOW", "SMA_FILTER_LIQUIDITY_WINDOW",
    "SMA_MARKET_REGIME_ENABLED", "SMA_FILTER_OVEREXT_LOOKBACK", "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO",
    "SMA_COST_EDGE_ENABLED", "SMA_COST_EDGE_MIN_RATIO", "ENTRY_EDGE_BUFFER_RATIO",
    "STRATEGY_MIN_EXPECTED_EDGE_RATIO", "STRATEGY_ENTRY_SLIPPAGE_BPS", "LIVE_FEE_RATE_ESTIMATE",
    "STRATEGY_EXIT_RULES", "STRATEGY_EXIT_STOP_LOSS_RATIO", "STRATEGY_EXIT_MAX_HOLDING_MIN",
    "STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO", "STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO")
_SMA_RESEARCH_ONLY = ("SMA_FILTER_VOLUME_WINDOW", "SMA_FILTER_LIQUIDITY_WINDOW")
_SMA_DEFAULTS = {
    "SMA_FILTER_GAP_MIN_RATIO": .0012, "SMA_FILTER_VOL_WINDOW": 10,
    "SMA_FILTER_VOL_MIN_RANGE_RATIO": .003, "SMA_FILTER_VOLUME_WINDOW": 10,
    "SMA_FILTER_LIQUIDITY_WINDOW": 10, "SMA_FILTER_OVEREXT_LOOKBACK": 3,
    "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO": .02, "SMA_MARKET_REGIME_ENABLED": True,
    "SMA_COST_EDGE_ENABLED": True, "SMA_COST_EDGE_MIN_RATIO": 0.0, "ENTRY_EDGE_BUFFER_RATIO": .0005,
    "STRATEGY_MIN_EXPECTED_EDGE_RATIO": 0.0, "STRATEGY_ENTRY_SLIPPAGE_BPS": 0.0,
    "LIVE_FEE_RATE_ESTIMATE": .0004, "STRATEGY_EXIT_RULES": "stop_loss,opposite_cross,max_holding_time",
    "STRATEGY_EXIT_STOP_LOSS_RATIO": 0.0, "STRATEGY_EXIT_MAX_HOLDING_MIN": 0,
    "STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO": 0.0, "STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO": 0.0}
_BOOL_PARAMETERS = {"SMA_MARKET_REGIME_ENABLED", "SMA_COST_EDGE_ENABLED"}
_INT_PARAMETERS = {"SMA_SHORT", "SMA_LONG", "SMA_FILTER_VOL_WINDOW", "SMA_FILTER_VOLUME_WINDOW",
                   "SMA_FILTER_LIQUIDITY_WINDOW", "SMA_FILTER_OVEREXT_LOOKBACK", "STRATEGY_EXIT_MAX_HOLDING_MIN"}
SMA_WITH_FILTER_SPEC = StrategySpec(
    strategy_name="sma_with_filter", strategy_version="sma_with_filter.research_runtime_contract.v2",
    accepted_parameter_names=_SMA_ACCEPTED, required_parameter_names=("SMA_SHORT", "SMA_LONG"),
    behavior_affecting_parameter_names=tuple(x for x in _SMA_ACCEPTED if x not in _SMA_RESEARCH_ONLY),
    metadata_only_parameter_names=(), research_only_parameter_names=_SMA_RESEARCH_ONLY,
    default_parameters=_SMA_DEFAULTS, decision_contract_version="research_sma_decision_contract.v3_entry_exit_risk_exit",
    required_data=("candles",), optional_data=("top_of_book",),
    exit_policy_schema={"schema_version": 1, "rules": ("stop_loss", "opposite_cross", "max_holding_time")},
    parameter_schema=tuple(StrategyParameterSchema(name,
        "bool" if name in _BOOL_PARAMETERS else "int" if name in _INT_PARAMETERS else "str" if name == "STRATEGY_EXIT_RULES" else "float",
        required=name in {"SMA_SHORT", "SMA_LONG"}, min_value=(None if name in _BOOL_PARAMETERS or name == "STRATEGY_EXIT_RULES" else 1 if name in {"SMA_SHORT", "SMA_LONG", "SMA_FILTER_VOL_WINDOW", "SMA_FILTER_VOLUME_WINDOW", "SMA_FILTER_LIQUIDITY_WINDOW", "SMA_FILTER_OVEREXT_LOOKBACK"} else 0),
        runtime_bound=name not in _SMA_RESEARCH_ONLY, behavior_affecting=name not in _SMA_RESEARCH_ONLY)
        for name in _SMA_ACCEPTED))

from .sma_with_filter_events import build_sma_with_filter_research_events


def _materialize(*, plugin: ResearchStrategyPlugin, parameter_values: dict[str, Any], fee_rate: float,
                 slippage_bps: float, materialized_parameters: dict[str, Any],
                 context: BacktestRunContext | None = None) -> dict[str, Any]:
    del plugin, context, fee_rate, slippage_bps
    values = dict(materialized_parameters)
    for key, value in {"SMA_FILTER_GAP_MIN_RATIO": 0.0, "SMA_FILTER_VOL_MIN_RANGE_RATIO": 0.0,
                       "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO": 0.0, "SMA_COST_EDGE_ENABLED": False,
                       "SMA_MARKET_REGIME_ENABLED": False}.items():
        if key not in parameter_values:
            values[key] = value
    return values


def _requirements(parameters: object | None = None) -> ResearchStrategyDataRequirements:
    params = dict(parameters if isinstance(parameters, Mapping)
                  else getattr(parameters, "parameters", {}) or {})
    lookback = max(int(params.get("SMA_LONG", 30)), int(params.get("SMA_FILTER_VOL_WINDOW", 10)),
                   int(params.get("SMA_FILTER_OVEREXT_LOOKBACK", 3)) + 1) + 2
    return ResearchStrategyDataRequirements(required_data=SMA_WITH_FILTER_SPEC.required_data,
        optional_data=SMA_WITH_FILTER_SPEC.optional_data,
        capabilities=(ResearchDataRequirement("candles", min_coverage_pct=100.0,
            source="sqlite_candles", lookback_rows=lookback),
            ResearchDataRequirement("top_of_book", required=False)))


def _exit_policy_materializer(strategy_name: str, parameter_values: dict[str, Any]) -> dict[str, object]:
    return materialize_sma_exit_policy(strategy_name, parameter_values)


def _exit_decision(*, policy: dict[str, object], portfolio: Any, event: Any,
                   market_price: float) -> ExitDecision:
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
        events = build_sma_with_filter_research_events(dataset=snapshot,
            parameter_values=self.parameters, fee_rate=self.fee_rate, slippage_bps=self.slippage_bps,
            execution_timing_policy=self.timing, portfolio_policy=self.portfolio_policy)
        return tuple(event for event in events if event.candle_ts == market.current_candle.ts)


def _runtime_factory(**values: Any) -> _SmaRuntime:
    values.pop("context", None)
    return _SmaRuntime(**values)


def build_sma_with_filter_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(name=SMA_WITH_FILTER_SPEC.strategy_name,
        version=SMA_WITH_FILTER_SPEC.strategy_version, spec=SMA_WITH_FILTER_SPEC,
        required_data=SMA_WITH_FILTER_SPEC.required_data, optional_data=SMA_WITH_FILTER_SPEC.optional_data,
        event_builder=build_sma_with_filter_research_events, parameter_materializer=_materialize,
        decision_contract_version=SMA_WITH_FILTER_SPEC.decision_contract_version,
        diagnostics_namespace="sma_with_filter", data_requirements_builder=_requirements,
        exit_policy_materializer=_exit_policy_materializer, exit_decision_builder=_exit_decision,
        exit_mode="strategy_owned", runtime_factory=_runtime_factory,
        reconstruction_module=__name__, reconstruction_qualname="build_sma_with_filter_plugin")

__all__ = ["build_sma_with_filter_plugin"]
