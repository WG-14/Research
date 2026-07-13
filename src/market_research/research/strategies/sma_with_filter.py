"""Compatibility factory delegating to the production composition root."""
def build_sma_with_filter_plugin():
    from market_research.research_composition import builtin_strategy_registry
    return builtin_strategy_registry().resolve("sma_with_filter")

__all__ = ["build_sma_with_filter_plugin"]
