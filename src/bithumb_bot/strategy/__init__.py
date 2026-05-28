"""Compatibility strategy facade.

Promotion-grade runtime strategy lifecycle behavior is exposed through
``ResearchStrategyPlugin`` manifests and plugin-bootstrapped runtime decision
adapters. ``StrategyPolicy`` helpers here are smoke/compatibility construction
surfaces and must not be used as live promotion-grade decision boundaries.
DB-bound ``SmaCrossStrategy`` and ``LegacySmaWithFilterDbAdapter`` exports
remain compatibility-only.
"""

from .base import LegacyDbStrategy, PositionContext, StrategyDecision, StrategyPolicy
from .registry import (
    create_legacy_db_strategy,
    create_smoke_strategy_policy,
    list_legacy_db_strategies,
    list_smoke_strategy_policies,
    register_legacy_db_strategy,
    register_smoke_strategy_policy,
)
from .sma import build_sma_with_filter_decision_from_normalized_db, decide_sma_with_filter_snapshot_from_db
from .sma_legacy_adapter import (
    LegacySmaWithFilterDbAdapter,
    SmaCrossStrategy,
    create_legacy_sma_with_filter_db_adapter,
    create_sma_strategy,
)
from .sma_policy_strategy import SmaWithFilterStrategy, create_sma_with_filter_strategy

register_legacy_db_strategy("sma_cross", create_sma_strategy)
register_smoke_strategy_policy("sma_with_filter", create_sma_with_filter_strategy)

__all__ = [
    "LegacyDbStrategy",
    "StrategyPolicy",
    "StrategyDecision",
    "PositionContext",
    "SmaCrossStrategy",
    "SmaWithFilterStrategy",
    "LegacySmaWithFilterDbAdapter",
    "create_legacy_sma_with_filter_db_adapter",
    "create_sma_strategy",
    "create_sma_with_filter_strategy",
    "build_sma_with_filter_decision_from_normalized_db",
    "decide_sma_with_filter_snapshot_from_db",
    "register_smoke_strategy_policy",
    "create_smoke_strategy_policy",
    "list_smoke_strategy_policies",
    "register_legacy_db_strategy",
    "create_legacy_db_strategy",
    "list_legacy_db_strategies",
]
