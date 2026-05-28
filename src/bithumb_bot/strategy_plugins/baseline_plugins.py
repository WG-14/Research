from __future__ import annotations

from typing import Any

from bithumb_bot.research.backtest_types import BacktestRun, BacktestRunContext
from bithumb_bot.research.dataset_snapshot import DatasetSnapshot
from bithumb_bot.research.execution_model import ExecutionModel
from bithumb_bot.research.experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy
from bithumb_bot.research.strategy_registry import ResearchStrategyPlugin, StrategyRuntimeCapabilities
from bithumb_bot.research.strategy_spec import BUY_AND_HOLD_BASELINE_SPEC, NOOP_BASELINE_SPEC
from bithumb_bot.strategy_plugins.baseline_events import (
    build_buy_and_hold_baseline_events,
    build_noop_baseline_events,
)


def run_noop_baseline_backtest(
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
) -> BacktestRun:
    from bithumb_bot.research.backtest_runner import run_plugin_backtest

    return run_plugin_backtest(
        plugin=NOOP_BASELINE_PLUGIN,
        dataset=dataset,
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        parameter_stability_score=parameter_stability_score,
        execution_model=execution_model,
        execution_timing_policy=execution_timing_policy,
        portfolio_policy=portfolio_policy,
        context=context,
    )


def run_buy_and_hold_baseline_backtest(
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
) -> BacktestRun:
    from bithumb_bot.research.backtest_runner import run_plugin_backtest

    return run_plugin_backtest(
        plugin=BUY_AND_HOLD_BASELINE_PLUGIN,
        dataset=dataset,
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        parameter_stability_score=parameter_stability_score,
        execution_model=execution_model,
        execution_timing_policy=execution_timing_policy,
        portfolio_policy=portfolio_policy,
        context=context,
    )


NOOP_BASELINE_PLUGIN = ResearchStrategyPlugin(
    name=NOOP_BASELINE_SPEC.strategy_name,
    version=NOOP_BASELINE_SPEC.strategy_version,
    spec=NOOP_BASELINE_SPEC,
    required_data=NOOP_BASELINE_SPEC.required_data,
    optional_data=NOOP_BASELINE_SPEC.optional_data,
    runner=run_noop_baseline_backtest,
    research_event_builder=build_noop_baseline_events,
    runtime_replay_builder=None,
    runtime_parameter_adapter=None,
    decision_contract_version=NOOP_BASELINE_SPEC.decision_contract_version,
    diagnostics_namespace="noop_baseline",
    runtime_capabilities=StrategyRuntimeCapabilities(
        promotion_runtime_decisions_supported=False,
        runtime_replay_supported=False,
        research_only=True,
        baseline_only=True,
        live_dry_run_allowed=False,
        live_real_order_allowed=False,
        approved_profile_required=False,
        fail_closed_reason="research_baseline_runtime_unsupported",
    ),
)


BUY_AND_HOLD_BASELINE_PLUGIN = ResearchStrategyPlugin(
    name=BUY_AND_HOLD_BASELINE_SPEC.strategy_name,
    version=BUY_AND_HOLD_BASELINE_SPEC.strategy_version,
    spec=BUY_AND_HOLD_BASELINE_SPEC,
    required_data=BUY_AND_HOLD_BASELINE_SPEC.required_data,
    optional_data=BUY_AND_HOLD_BASELINE_SPEC.optional_data,
    runner=run_buy_and_hold_baseline_backtest,
    research_event_builder=build_buy_and_hold_baseline_events,
    runtime_replay_builder=None,
    runtime_parameter_adapter=None,
    decision_contract_version=BUY_AND_HOLD_BASELINE_SPEC.decision_contract_version,
    diagnostics_namespace="buy_and_hold_baseline",
    runtime_capabilities=StrategyRuntimeCapabilities(
        promotion_runtime_decisions_supported=False,
        runtime_replay_supported=False,
        research_only=True,
        baseline_only=True,
        live_dry_run_allowed=False,
        live_real_order_allowed=False,
        approved_profile_required=False,
        fail_closed_reason="research_baseline_runtime_unsupported",
    ),
)
