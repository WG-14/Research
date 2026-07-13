from market_research.research.builtin_registry import builtin_strategy_registry
from market_research.builtin_strategies import sma_with_filter


def test_runtime_class_method_change_changes_plugin_contract_hash(monkeypatch):
    plugin = builtin_strategy_registry().resolve("sma_with_filter")
    before = plugin.contract_hash()
    monkeypatch.setattr(sma_with_filter._SmaRuntime, "on_market_event", lambda self, market, portfolio, state: ())
    assert plugin.contract_hash() != before


def test_transitive_exit_helper_change_changes_source_binding(monkeypatch):
    plugin = builtin_strategy_registry().resolve("sma_with_filter")
    before = plugin.contract_hash()
    monkeypatch.setattr(sma_with_filter, "evaluate_sma_exit_policy", lambda **values: None)
    assert plugin.contract_hash() != before
