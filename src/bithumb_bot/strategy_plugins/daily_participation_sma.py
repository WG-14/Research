from __future__ import annotations

from dataclasses import replace
from typing import Any

from bithumb_bot.core.sma_policy import (
    EntryExecutionIntent,
    ExecutionConstraintSnapshot,
    MarketWindow,
    PositionSnapshot,
    SmaPolicyConfig,
    _stable_hash,
)
from bithumb_bot.research.backtest_types import BacktestRun, BacktestRunContext
from bithumb_bot.research.dataset_snapshot import DatasetSnapshot
from bithumb_bot.research.execution_model import ExecutionModel
from bithumb_bot.research.experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy
from bithumb_bot.research.strategy_registry import ResearchStrategyPlugin
from bithumb_bot.research.strategy_spec import (
    SMA_WITH_FILTER_SPEC,
    StrategyParameterSchema,
    StrategySpec,
    materialize_strategy_parameters,
)
from bithumb_bot.strategy.daily_participation_policy import (
    DailyParticipationPolicyConfig,
    DailyParticipationStateSnapshot,
    evaluate_daily_participation_policy,
)
from bithumb_bot.strategy.exit_rules import ExitPolicyConfig
from bithumb_bot.strategy.sma_decision_assembler import evaluate_sma_final_decision
from bithumb_bot.strategy_authoring import research_plugin_from_event_builder
from bithumb_bot.strategy_plugins.sma_with_filter_events import SmaWithFilterDecisionAdapter


DAILY_PARTICIPATION_PARAMETERS: tuple[str, ...] = (
    "DAILY_PARTICIPATION_ENABLED",
    "DAILY_PARTICIPATION_TIMEZONE",
    "DAILY_PARTICIPATION_COUNT_BASIS",
    "DAILY_PARTICIPATION_WINDOW_START_HOUR_KST",
    "DAILY_PARTICIPATION_WINDOW_END_HOUR_KST",
    "DAILY_PARTICIPATION_BUY_FRACTION",
    "DAILY_PARTICIPATION_MAX_ORDER_KRW",
)


DAILY_PARTICIPATION_SMA_SPEC = StrategySpec(
    strategy_name="daily_participation_sma",
    strategy_version="daily_participation_sma.research_runtime_contract.v1",
    accepted_parameter_names=tuple(SMA_WITH_FILTER_SPEC.accepted_parameter_names) + DAILY_PARTICIPATION_PARAMETERS,
    required_parameter_names=tuple(SMA_WITH_FILTER_SPEC.required_parameter_names),
    behavior_affecting_parameter_names=tuple(SMA_WITH_FILTER_SPEC.behavior_affecting_parameter_names)
    + DAILY_PARTICIPATION_PARAMETERS,
    metadata_only_parameter_names=(),
    research_only_parameter_names=SMA_WITH_FILTER_SPEC.research_only_parameter_names,
    default_parameters={
        **SMA_WITH_FILTER_SPEC.default_parameters,
        "DAILY_PARTICIPATION_ENABLED": False,
        "DAILY_PARTICIPATION_TIMEZONE": "Asia/Seoul",
        "DAILY_PARTICIPATION_COUNT_BASIS": "filled",
        "DAILY_PARTICIPATION_WINDOW_START_HOUR_KST": 0,
        "DAILY_PARTICIPATION_WINDOW_END_HOUR_KST": 24,
        "DAILY_PARTICIPATION_BUY_FRACTION": 0.05,
        "DAILY_PARTICIPATION_MAX_ORDER_KRW": 10000.0,
    },
    parameter_schema=SMA_WITH_FILTER_SPEC.parameter_schema
    + (
        StrategyParameterSchema("DAILY_PARTICIPATION_ENABLED", "bool", unit="enabled_flag"),
        StrategyParameterSchema("DAILY_PARTICIPATION_TIMEZONE", "str", enum=("Asia/Seoul", "KST"), unit="timezone"),
        StrategyParameterSchema(
            "DAILY_PARTICIPATION_COUNT_BASIS",
            "str",
            enum=("intent", "submit_expected", "submitted", "filled", "closed_trade"),
            unit="count_basis",
        ),
        StrategyParameterSchema("DAILY_PARTICIPATION_WINDOW_START_HOUR_KST", "int", min_value=0, max_value=23, unit="hour"),
        StrategyParameterSchema("DAILY_PARTICIPATION_WINDOW_END_HOUR_KST", "int", min_value=1, max_value=24, unit="hour"),
        StrategyParameterSchema("DAILY_PARTICIPATION_BUY_FRACTION", "float", min_value=0.0, max_value=1.0, unit="cash_fraction"),
        StrategyParameterSchema("DAILY_PARTICIPATION_MAX_ORDER_KRW", "float", min_value=0.0, unit="krw"),
    ),
    decision_contract_version="daily_participation_sma_decision_contract.v1",
    required_data=SMA_WITH_FILTER_SPEC.required_data,
    optional_data=SMA_WITH_FILTER_SPEC.optional_data,
    exit_policy_schema=SMA_WITH_FILTER_SPEC.exit_policy_schema,
)


def daily_participation_config_from_parameters(values: dict[str, Any]) -> DailyParticipationPolicyConfig:
    return DailyParticipationPolicyConfig(
        enabled=bool(values["DAILY_PARTICIPATION_ENABLED"]),
        timezone=str(values["DAILY_PARTICIPATION_TIMEZONE"]),
        count_basis=str(values["DAILY_PARTICIPATION_COUNT_BASIS"]),  # type: ignore[arg-type]
        window_start_hour=int(values["DAILY_PARTICIPATION_WINDOW_START_HOUR_KST"]),
        window_end_hour=int(values["DAILY_PARTICIPATION_WINDOW_END_HOUR_KST"]),
        buy_fraction=float(values["DAILY_PARTICIPATION_BUY_FRACTION"]),
        max_order_krw=float(values["DAILY_PARTICIPATION_MAX_ORDER_KRW"]),
    )


def materialize_daily_participation_sma_parameters(
    *,
    plugin: ResearchStrategyPlugin,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    context: BacktestRunContext | None = None,
) -> dict[str, Any]:
    del plugin, context
    values = materialize_strategy_parameters(
        "daily_participation_sma",
        parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )
    daily_participation_config_from_parameters(values)
    return values


def evaluate_daily_participation_sma_decision(
    *,
    market: MarketWindow,
    position: PositionSnapshot,
    config: SmaPolicyConfig,
    execution_context: ExecutionConstraintSnapshot,
    exit_policy_config: ExitPolicyConfig,
    participation_config: DailyParticipationPolicyConfig,
    participation_state: DailyParticipationStateSnapshot,
    signal_context_extra: dict[str, object] | None = None,
    rule_sources: dict[str, str] | None = None,
):
    base = evaluate_sma_final_decision(
        market=market,
        position=position,
        config=config,
        execution_context=execution_context,
        exit_policy_config=exit_policy_config,
        signal_context_extra=signal_context_extra,
        rule_sources=rule_sources,
    )
    base_entry_signal = "BUY" if base.final_signal == "BUY" else "HOLD"
    participation = evaluate_daily_participation_policy(config=participation_config, state=participation_state)
    final_signal = base.final_signal
    final_reason = base.final_reason
    entry_signal_source = "sma_cross" if base.final_signal == "BUY" else "hold"
    entry_sizing_source = "base_sma" if base.final_signal == "BUY" else "none"
    execution_intent = base.execution_intent
    if base.final_signal != "BUY" and participation.allowed:
        final_signal = "BUY"
        final_reason = participation.reason_code
        entry_signal_source = "daily_participation_fallback"
        entry_sizing_source = "daily_participation_policy"
        execution_intent = EntryExecutionIntent(
            side="BUY",
            intent="enter_open_exposure",
            pair=market.pair,
            requires_execution_sizing=True,
            budget_fraction_of_cash=float(participation_config.buy_fraction),
            max_budget_krw=float(participation_config.max_order_krw),
        )
    execution_payload = execution_intent.as_dict() if execution_intent is not None else None
    trace = dict(base.trace)
    trace.update(
        {
            "strategy_family": "daily_participation_sma",
            "base_strategy": "sma_with_filter",
            "entry_signal_source": entry_signal_source,
            "entry_sizing_source": entry_sizing_source,
            "base_entry_signal": base_entry_signal,
            "participation_entry_signal": "BUY" if participation.allowed else "HOLD",
            "daily_participation_decision": participation.as_dict(),
            "timezone": participation_config.timezone,
            "count_basis": participation.count_basis,
            "kst_day": participation.kst_day,
            "daily_count_snapshot_hash": participation.daily_count_snapshot_hash,
            "participation_policy_hash": participation.participation_policy_hash,
            "participation_decision_hash": participation.participation_decision_hash,
            "not_a_fill_guarantee": True,
            "execution_intent": execution_payload,
        }
    )
    policy_input_hash = _stable_hash(
        {
            "base_policy_input_hash": base.policy_input_hash,
            "entry_signal_source": entry_signal_source,
            "entry_sizing_source": entry_sizing_source,
            "daily_count_snapshot_hash": participation.daily_count_snapshot_hash,
            "participation_policy_hash": participation.participation_policy_hash,
            "participation_input_hash": participation.participation_input_hash,
            "execution_sizing": {
                "base_buy_fraction": float(config.buy_fraction),
                "base_max_order_krw": float(config.max_order_krw),
                "participation_buy_fraction": float(participation_config.buy_fraction),
                "participation_max_order_krw": float(participation_config.max_order_krw),
            },
        }
    )
    policy_decision_hash = _stable_hash(
        {
            "strategy_name": "daily_participation_sma",
            "final_signal": final_signal,
            "final_reason": final_reason,
            "entry_signal_source": entry_signal_source,
            "entry_sizing_source": entry_sizing_source,
            "execution_intent": execution_payload,
            "participation_decision_hash": participation.participation_decision_hash,
        }
    )
    return replace(
        base,
        strategy_name="daily_participation_sma",
        final_signal=final_signal,
        final_reason=final_reason,
        execution_intent=execution_intent,
        trace=trace,
        policy_hash=_stable_hash(trace),
        policy_input_hash=policy_input_hash,
        policy_decision_hash=policy_decision_hash,
    )


def build_daily_participation_sma_research_events(
    *,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    execution_timing_policy: ExecutionTimingPolicy,
    portfolio_policy: Any | None = None,
    context: Any | None = None,
) -> tuple[Any, ...]:
    del portfolio_policy, context
    return SmaWithFilterDecisionAdapter(
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        timing_policy=execution_timing_policy,
        strategy_name="daily_participation_sma",
    ).build_events(dataset)


def run_daily_participation_sma_backtest(
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
        plugin=DAILY_PARTICIPATION_SMA_PLUGIN,
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


_RESEARCH_PLUGIN = research_plugin_from_event_builder(
    strategy_name="daily_participation_sma",
    spec=DAILY_PARTICIPATION_SMA_SPEC,
    version=DAILY_PARTICIPATION_SMA_SPEC.strategy_version,
    required_data=DAILY_PARTICIPATION_SMA_SPEC.required_data,
    optional_data=DAILY_PARTICIPATION_SMA_SPEC.optional_data,
    build_research_events=build_daily_participation_sma_research_events,
    diagnostics_namespace="daily_participation_sma",
    research_parameter_materializer=materialize_daily_participation_sma_parameters,
)


DAILY_PARTICIPATION_SMA_PLUGIN = replace(
    _RESEARCH_PLUGIN.to_research_strategy_plugin(),
    runner=run_daily_participation_sma_backtest,
)

