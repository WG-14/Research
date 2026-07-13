from pathlib import Path
from dataclasses import replace
import pytest
from market_research.research_composition import resolve_builtin_strategy as resolve_research_strategy


def test_common_engine_does_not_import_strategy_exit_evaluator():
    source = Path("src/market_research/research/simulation_engine.py").read_text()
    assert "evaluate_sma_exit_policy" not in source
    assert "materialize_sma_exit_policy" not in source


def test_sma_event_does_not_prebuild_final_exit_intent():
    source = Path("src/market_research/research/strategies/sma_with_filter_events.py").read_text()
    assert "exit_intent=" not in source


def test_common_policy_rejects_strategy_exit_builder():
    plugin = resolve_research_strategy("sma_with_filter")
    with pytest.raises(ValueError, match="multiple_exit_authorities"):
        replace(plugin, exit_mode="common_typed_policy")
