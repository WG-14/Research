"""Built-in buy-and-hold plugin implementation."""

from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import (
    StrategyFeatureDefinition,
    StrategyParameterSchema,
    StrategyRuleDeclaration,
    StrategyRuleSpec,
    StrategySpec,
)
from market_research.strategy_sdk.runtime import make_event_builder_runtime_factory

BUY_AND_HOLD_BASELINE_SPEC = StrategySpec(
    strategy_name="buy_and_hold_baseline",
    strategy_version="buy_and_hold_baseline.research_contract.v1",
    accepted_parameter_names=("BUY_HOLD_BUY_INDEX", "BUY_HOLD_DECISION_REASON"),
    required_parameter_names=("BUY_HOLD_BUY_INDEX",),
    behavior_affecting_parameter_names=(
        "BUY_HOLD_BUY_INDEX",
        "BUY_HOLD_DECISION_REASON",
    ),
    metadata_only_parameter_names=(),
    research_only_parameter_names=(),
    default_parameters={"BUY_HOLD_DECISION_REASON": "buy_and_hold_architecture_canary"},
    decision_contract_version="research_buy_and_hold_baseline_decision_contract.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={
        "schema_version": 1,
        "rules": (),
        "description": "Executable canary emits one BUY intent, then HOLD decisions.",
    },
    parameter_schema=(
        StrategyParameterSchema(
            "BUY_HOLD_BUY_INDEX",
            "int",
            required=True,
            min_value=0,
            unit="candle_index",
            description="Zero-based completed-candle index for the single entry.",
            optimization_allowed=False,
            since_version="buy_and_hold_baseline.research_contract.v1",
        ),
        StrategyParameterSchema(
            "BUY_HOLD_DECISION_REASON",
            "str",
            unit="label",
            description="Audit reason attached to the deterministic entry decision.",
            default_value="buy_and_hold_architecture_canary",
            optimization_allowed=False,
            since_version="buy_and_hold_baseline.research_contract.v1",
        ),
    ),
    rule_spec=StrategyRuleSpec(
        1,
        entry=StrategyRuleDeclaration(
            "buy_at_index",
            "Buy once at the configured candle index.",
            "candle_index == BUY_HOLD_BUY_INDEX",
            ("BUY_HOLD_BUY_INDEX",),
        ),
        take_profit=StrategyRuleDeclaration(
            "take_profit", "No take-profit exit.", "never"
        ),
        edge_invalidation=StrategyRuleDeclaration(
            "edge_invalidation", "No edge-invalidation exit.", "never"
        ),
        time_exit=StrategyRuleDeclaration("time_exit", "No time exit.", "never"),
        stop_loss=StrategyRuleDeclaration("stop_loss", "No stop-loss exit.", "never"),
        position_sizing=StrategyRuleDeclaration(
            "portfolio_fractional_cash",
            "Use experiment portfolio buy fraction.",
            "on entry",
        ),
        entry_prohibitions=(
            StrategyRuleDeclaration(
                "existing_position",
                "Do not pyramid after entry.",
                "position or buy pending",
            ),
        ),
    ),
    feature_definitions=(
        StrategyFeatureDefinition(
            "candle_index",
            "Zero-based candle index used to select the single baseline entry.",
            ("candles",),
            "candle_index",
            ("BUY_HOLD_BUY_INDEX",),
        ),
    ),
)

# The event module imports the completed spec above; this late import breaks that cycle.
from .buy_and_hold_baseline_events import (  # noqa: E402
    build_buy_and_hold_baseline_events,
)


_runtime_factory = make_event_builder_runtime_factory(
    build_buy_and_hold_baseline_events,
    current_candle_only=True,
    pass_candle_index_offset=True,
)


def build_buy_and_hold_baseline_plugin() -> ResearchStrategyPlugin:
    from market_research.research.strategy_manifest import (
        builtin_strategy_manifest_hash,
    )

    return ResearchStrategyPlugin(
        name=BUY_AND_HOLD_BASELINE_SPEC.strategy_name,
        version=BUY_AND_HOLD_BASELINE_SPEC.strategy_version,
        spec=BUY_AND_HOLD_BASELINE_SPEC,
        required_data=BUY_AND_HOLD_BASELINE_SPEC.required_data,
        optional_data=BUY_AND_HOLD_BASELINE_SPEC.optional_data,
        event_builder=build_buy_and_hold_baseline_events,
        decision_contract_version=BUY_AND_HOLD_BASELINE_SPEC.decision_contract_version,
        diagnostics_namespace="buy_and_hold_baseline",
        runtime_factory=_runtime_factory,
        reconstruction_module=__name__,
        reconstruction_qualname="build_buy_and_hold_baseline_plugin",
        package_manifest_hash=builtin_strategy_manifest_hash(__name__),
    )


STRATEGY_PLUGIN_FACTORY = build_buy_and_hold_baseline_plugin

__all__ = ["build_buy_and_hold_baseline_plugin", "STRATEGY_PLUGIN_FACTORY"]
