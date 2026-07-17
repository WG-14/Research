from dataclasses import replace
from types import FunctionType

from market_research.research_composition import builtin_strategy_registry
from market_research.builtin_strategies import sma_with_filter
from market_research.strategy_sdk.runtime import EventBuilderStrategyRuntime


def test_runtime_class_method_change_changes_plugin_contract_hash(monkeypatch):
    plugin = builtin_strategy_registry().resolve("sma_with_filter")
    before = plugin.contract_hash()
    monkeypatch.setattr(
        EventBuilderStrategyRuntime,
        "on_market_event",
        lambda self, market, portfolio, state: (),
    )
    assert plugin.contract_hash() != before


def test_transitive_exit_helper_change_changes_source_binding(monkeypatch):
    plugin = builtin_strategy_registry().resolve("sma_with_filter")
    before = plugin.contract_hash()
    monkeypatch.setattr(
        sma_with_filter, "evaluate_sma_exit_policy", lambda **values: None
    )
    assert plugin.contract_hash() != before


def _external_hook(source: str):
    namespace = {}
    exec(compile(source, "<external-strategy-hook>", "exec"), namespace)
    hook = FunctionType(namespace["build_events"].__code__, {})
    hook.__module__ = "external_research_plugin.strategy"
    hook.__qualname__ = "build_events"
    return hook


def test_external_plugin_hook_behavior_is_source_bound():
    base = builtin_strategy_registry().resolve("noop_baseline")
    first = replace(
        base,
        event_builder=_external_hook("def build_events(**values):\n    return ()\n"),
        runtime_factory=None,
    )
    changed = replace(
        base,
        event_builder=_external_hook(
            "def build_events(**values):\n    return tuple(values.get('events', ()))\n"
        ),
        runtime_factory=None,
    )

    first_hook = first.contract_payload()["behavior_hooks"][
        "event_builder_compatibility"
    ]
    changed_hook = changed.contract_payload()["behavior_hooks"][
        "event_builder_compatibility"
    ]

    assert first_hook["module"] == changed_hook["module"]
    assert first_hook["qualname"] == changed_hook["qualname"]
    assert first_hook["transitive_behavior_components"]
    assert first.contract_hash() != changed.contract_hash()
