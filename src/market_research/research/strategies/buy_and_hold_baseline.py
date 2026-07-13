"""Compatibility factory delegating to the production composition root."""
def build_buy_and_hold_baseline_plugin():
    from market_research.research_composition import builtin_strategy_registry
    return builtin_strategy_registry().resolve("buy_and_hold_baseline")

__all__ = ["build_buy_and_hold_baseline_plugin"]
