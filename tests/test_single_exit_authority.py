from pathlib import Path


def test_common_engine_does_not_import_strategy_exit_evaluator():
    source = Path("src/market_research/research/simulation_engine.py").read_text()
    assert "evaluate_sma_exit_policy" not in source
    assert "materialize_sma_exit_policy" not in source


def test_sma_event_does_not_prebuild_final_exit_intent():
    source = Path("src/market_research/research/strategies/sma_with_filter_events.py").read_text()
    assert "exit_intent=" not in source
