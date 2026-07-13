"""Compatibility factory delegating to the production composition root."""
def build_noop_baseline_plugin():
    from market_research.research_composition import builtin_strategy_registry
    return builtin_strategy_registry().resolve("noop_baseline")

__all__ = ["build_noop_baseline_plugin"]
