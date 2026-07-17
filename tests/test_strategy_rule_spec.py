from __future__ import annotations

import pytest

from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import StrategySpec
from market_research.research_composition import builtin_strategy_registry


def test_every_builtin_exposes_complete_strategy_rule_spec():
    for plugin in builtin_strategy_registry().plugins.values():
        payload = plugin.spec.rule_spec.as_dict()
        assert payload["entry"]["description"]
        for name in (
            "take_profit",
            "edge_invalidation",
            "time_exit",
            "stop_loss",
            "position_sizing",
        ):
            assert payload[name]["enabled_when"]
        assert plugin.spec.as_dict()["rule_spec"] == payload


def test_sma_rule_spec_exposes_entry_blocks_sizing_and_exit_priority():
    rules = (
        builtin_strategy_registry().resolve("sma_with_filter").spec.rule_spec.as_dict()
    )
    assert rules["entry"]["rule_id"] == "golden_cross"
    assert {item["rule_id"] for item in rules["entry_prohibitions"]} == {
        "gap_filter",
        "volatility_filter",
        "overextension_filter",
        "cost_edge_filter",
    }
    assert rules["position_sizing"]["rule_id"] == "portfolio_fractional_cash"
    assert rules["exit_priority"] == ["stop_loss", "opposite_cross", "max_holding_time"]


def test_plugin_rejects_missing_rule_spec():
    spec = StrategySpec(
        "missing_rules",
        "v1",
        (),
        (),
        (),
        (),
        (),
        {},
        "v1",
        ("candles",),
        (),
        {"schema_version": 1, "rules": ()},
    )
    with pytest.raises(ValueError, match="research_strategy_rule_spec_missing"):
        ResearchStrategyPlugin(
            name="missing_rules",
            version="v1",
            spec=spec,
            required_data=("candles",),
            optional_data=(),
            event_builder=lambda **_: (),
            decision_contract_version="v1",
            diagnostics_namespace="missing",
        )
