"""Research-only declaration for the threshold strategy."""

from __future__ import annotations

from typing import Any

from ..backtest_types import BacktestRunContext
from ..strategy_contract import ResearchStrategyPlugin
from ..strategy_spec import THRESHOLD_RESEARCH_ONLY_SPEC, materialize_strategy_parameters
from .threshold_research_only_events import build_threshold_research_only_events
from .threshold_research_only_kernel import run_threshold_research_only_backtest


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
        runner=run_threshold_research_only_backtest,
        event_builder=build_threshold_research_only_events,
        parameter_materializer=_materialize,
        decision_contract_version=(
            THRESHOLD_RESEARCH_ONLY_SPEC.decision_contract_version
        ),
        diagnostics_namespace="threshold_research_only",
    )
