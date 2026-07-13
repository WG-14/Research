"""Compatibility adapter for built-in SMA exit behavior."""
from market_research.builtin_strategies.sma_exit_rules import (ResearchExitDecision,
    evaluate_sma_exit_policy, materialize_sma_exit_policy)

__all__ = ["ResearchExitDecision", "evaluate_sma_exit_policy", "materialize_sma_exit_policy"]
