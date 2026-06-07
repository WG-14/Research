from __future__ import annotations

# Schema-known values are broad serialization compatibility values, not live
# submit allowlists. Live authorization is decided by submit_authority_policy.py.
EXECUTION_SUBMIT_PLAN_SCHEMA_KNOWN_SOURCES = frozenset(
    {
        "target_delta",
        "strategy_position",
        "residual_inventory",
        "research_backtest",
    }
)
EXECUTION_SUBMIT_PLAN_SCHEMA_KNOWN_AUTHORITIES = frozenset(
    {
        "canonical_target_delta_sizing",
        "configured_strategy_order_size",
        "residual_inventory_policy",
        "residual_inventory_delta",
        "strategy_execution_intent",
        "research_compatibility_execution_intent",
        "target_position_delta",
    }
)

__all__ = [
    "EXECUTION_SUBMIT_PLAN_SCHEMA_KNOWN_AUTHORITIES",
    "EXECUTION_SUBMIT_PLAN_SCHEMA_KNOWN_SOURCES",
]
