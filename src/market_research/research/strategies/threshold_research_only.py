"""Compatibility factory delegating to the production composition root."""
def build_threshold_research_only_plugin():
    from market_research.research_composition import builtin_strategy_registry
    return builtin_strategy_registry().resolve("threshold_research_only")

__all__ = ["build_threshold_research_only_plugin"]
