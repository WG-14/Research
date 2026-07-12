from __future__ import annotations

from typing import Any

from ..backtest_types import BacktestRunContext
from ..strategy_contract import (
    ResearchDataRequirement,
    ResearchStrategyDataRequirements,
    ResearchStrategyPlugin,
)
from ..strategy_spec import SMA_WITH_FILTER_SPEC, materialize_strategy_parameters
from .sma_with_filter_events import build_sma_with_filter_research_events
from ..exit_rules import materialize_sma_exit_policy


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
    )
    return plugin
