"""Public research-strategy authoring contracts; contains no built-in imports."""

from market_research.research.causal_market_view import (
    CausalMarketView,
    FutureMarketAccessError,
)
from market_research.research.exit_decision import ExitDecision
from market_research.research.portfolio_view import ReadOnlyPortfolioView
from market_research.research.strategy_compiler import (
    StrategyCompilationError,
    StrategyCompiler,
)
from market_research.research.strategy_contract import (
    CompiledStrategyContract,
    ResearchStrategyPlugin,
    StrategyCapabilityContract,
)
from market_research.research.strategy_registry import (
    StrategyRegistry,
    StrategyRegistryError,
)
from .runtime import EventBuilderStrategyRuntime, make_event_builder_runtime_factory

__all__ = [
    "CausalMarketView",
    "CompiledStrategyContract",
    "ExitDecision",
    "FutureMarketAccessError",
    "ReadOnlyPortfolioView",
    "ResearchStrategyPlugin",
    "StrategyCapabilityContract",
    "StrategyCompilationError",
    "StrategyCompiler",
    "StrategyRegistry",
    "StrategyRegistryError",
    "EventBuilderStrategyRuntime",
    "make_event_builder_runtime_factory",
]
