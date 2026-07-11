"""Compatibility bridge for unextracted research strategies.

This is deliberately lazy and is not reachable from the SMA research command.
It keeps the legacy integrated CLI working while each remaining implementation
is moved into this package in a subsequent focused patch.
"""
from __future__ import annotations

from typing import Any

from ..strategy_contract import ResearchStrategyPlugin


def build_legacy_research_plugin(name: str) -> ResearchStrategyPlugin:
    from bithumb_bot.research.strategy_registry import resolve_research_strategy_plugin

    legacy: Any = resolve_research_strategy_plugin(name)
    return ResearchStrategyPlugin(
        name=legacy.name,
        version=legacy.version,
        spec=legacy.spec,
        required_data=legacy.required_data,
        optional_data=legacy.optional_data,
        runner=legacy.runner,
        event_builder=legacy.research_event_builder,
        parameter_materializer=legacy.research_parameter_materializer,
        decision_contract_version=legacy.decision_contract_version,
        diagnostics_namespace=legacy.diagnostics_namespace,
        diagnostics_builder=legacy.diagnostics_count_builder,
    )
