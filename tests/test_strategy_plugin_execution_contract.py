from __future__ import annotations

import pytest

from market_research.research_composition import (
    list_builtin_strategies as list_research_strategies,
)
from market_research.research.strategy_contract import ResearchStrategyPlugin


def test_strategy_plugin_does_not_require_strategy_specific_runner():
    assert all(not hasattr(plugin, "runner") for plugin in list_research_strategies())


def test_all_builtin_plugins_use_common_execution_authority():
    assert {plugin.execution_authority for plugin in list_research_strategies()} == {
        "common_simulation_engine"
    }


def test_plugin_cannot_register_custom_backtest_runner():
    plugin = list_research_strategies()[0]
    with pytest.raises(ValueError, match="custom_execution_authority"):
        ResearchStrategyPlugin(
            name="test",
            version="1",
            spec=plugin.spec,
            required_data=(),
            optional_data=(),
            event_builder=plugin.event_builder,
            decision_contract_version="1",
            diagnostics_namespace="test",
            execution_authority="custom_runner",
        )


def test_plugin_contract_hash_binds_event_builder_and_execution_authority():
    payload = list_research_strategies()[0].contract_payload()
    assert (
        payload["execution_authority"] == "common_simulation_engine"
        and payload["event_builder_qualname"]
    )
