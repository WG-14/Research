from __future__ import annotations

from .research.strategy_registry import list_research_strategy_plugins


_REGISTERED = False


def ensure_runtime_decision_adapters_registered() -> None:
    """Load plugin discovery so adapter resolution can derive from manifests."""
    global _REGISTERED
    if _REGISTERED:
        return
    list_research_strategy_plugins()
    _REGISTERED = True


def reset_runtime_decision_adapter_bootstrap_for_tests() -> None:
    global _REGISTERED
    _REGISTERED = False
