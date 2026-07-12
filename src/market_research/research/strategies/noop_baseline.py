"""Research-only declaration for the noop baseline."""

from __future__ import annotations

from typing import Any

from ..backtest_types import BacktestRunContext
from ..strategy_contract import ResearchStrategyPlugin
from ..strategy_spec import NOOP_BASELINE_SPEC, materialize_strategy_parameters
from .noop_baseline_events import build_noop_baseline_events


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
        "noop_baseline",
        parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )


def build_noop_baseline_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(
        name=NOOP_BASELINE_SPEC.strategy_name,
        version=NOOP_BASELINE_SPEC.strategy_version,
        spec=NOOP_BASELINE_SPEC,
        required_data=NOOP_BASELINE_SPEC.required_data,
        optional_data=NOOP_BASELINE_SPEC.optional_data,
        event_builder=build_noop_baseline_events,
        parameter_materializer=_materialize,
        decision_contract_version=NOOP_BASELINE_SPEC.decision_contract_version,
        diagnostics_namespace="noop_baseline",
    )
