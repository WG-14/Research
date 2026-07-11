from __future__ import annotations

from typing import Any

from ..backtest_runner import run_plugin_backtest
from ..backtest_types import BacktestRun, BacktestRunContext
from ..dataset_snapshot import DatasetSnapshot
from ..execution_model import ExecutionModel
from ..experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy
from ..strategy_contract import (
    ResearchDataRequirement,
    ResearchStrategyDataRequirements,
    ResearchStrategyPlugin,
)
from ..strategy_spec import SMA_WITH_FILTER_SPEC, materialize_strategy_parameters
from bithumb_bot.strategy_plugins.sma_with_filter_events import build_sma_with_filter_research_events


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
    # The event stream carries the historical research exit intent.  Keep this
    # materialization local so a research run does not load policy/profile code.
    return {
        "exit_policy": {"schema_version": 1, "strategy_name": strategy_name, "rules": []},
        "exit_policy_config": {"schema_version": 1, "strategy_name": strategy_name, "rules": []},
        "exit_policy_source": "research_strategy_catalog",
        "exit_policy_materialization_mode": "research_exploratory",
    }


def _run(dataset: DatasetSnapshot, parameter_values: dict[str, Any], fee_rate: float, slippage_bps: float, parameter_stability_score: float | None = None, execution_model: ExecutionModel | None = None, execution_timing_policy: ExecutionTimingPolicy | None = None, portfolio_policy: PortfolioPolicy | None = None, context: BacktestRunContext | None = None) -> BacktestRun:
    if "SMA_SHORT" not in parameter_values or "SMA_LONG" not in parameter_values:
        raise ValueError("sma_with_filter_required_parameters_missing")
    return run_plugin_backtest(plugin=build_sma_with_filter_plugin(), dataset=dataset, parameter_values=parameter_values, fee_rate=fee_rate, slippage_bps=slippage_bps, parameter_stability_score=parameter_stability_score, execution_model=execution_model, execution_timing_policy=execution_timing_policy, portfolio_policy=portfolio_policy, context=context)


def build_sma_with_filter_plugin() -> ResearchStrategyPlugin:
    plugin = ResearchStrategyPlugin(
        name=SMA_WITH_FILTER_SPEC.strategy_name,
        version=SMA_WITH_FILTER_SPEC.strategy_version,
        spec=SMA_WITH_FILTER_SPEC,
        required_data=SMA_WITH_FILTER_SPEC.required_data,
        optional_data=SMA_WITH_FILTER_SPEC.optional_data,
        runner=_run,
        event_builder=build_sma_with_filter_research_events,
        parameter_materializer=_materialize,
        decision_contract_version=SMA_WITH_FILTER_SPEC.decision_contract_version,
        diagnostics_namespace="sma_with_filter",
        data_requirements_builder=_requirements,
        exit_policy_materializer=_exit_policy_materializer,
    )
    return plugin
