"""Built-in threshold research plugin implementation."""

from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import (
    StrategyFeatureDefinition,
    StrategyParameterSchema,
    StrategyRuleDeclaration,
    StrategyRuleSpec,
    StrategySpec,
)
from market_research.strategy_sdk.runtime import make_event_builder_runtime_factory

THRESHOLD_RESEARCH_ONLY_SPEC = StrategySpec(
    strategy_name="threshold_research_only",
    strategy_version="threshold_research_only.research_contract.v1",
    accepted_parameter_names=("THRESHOLD_CLOSE_ABOVE",),
    required_parameter_names=("THRESHOLD_CLOSE_ABOVE",),
    behavior_affecting_parameter_names=("THRESHOLD_CLOSE_ABOVE",),
    metadata_only_parameter_names=(),
    research_only_parameter_names=(),
    default_parameters={},
    decision_contract_version="research_threshold_research_only_decision_contract.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={
        "schema_version": 1,
        "rules": (),
        "description": "Research-only threshold strategy with no explicit exit.",
    },
    parameter_schema=(
        StrategyParameterSchema(
            "THRESHOLD_CLOSE_ABOVE",
            "float",
            required=True,
            min_value=0.0,
            unit="quote_currency_per_asset",
            description="Completed-candle close threshold required for entry.",
            optimization_allowed=True,
            since_version="threshold_research_only.research_contract.v1",
        ),
    ),
    rule_spec=StrategyRuleSpec(
        1,
        entry=StrategyRuleDeclaration(
            "close_above_threshold",
            "Buy when close is strictly above threshold.",
            "close > THRESHOLD_CLOSE_ABOVE",
            ("THRESHOLD_CLOSE_ABOVE",),
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
                "existing_or_pending_position",
                "Block duplicate entry.",
                "position or buy pending",
            ),
        ),
    ),
    feature_definitions=(
        StrategyFeatureDefinition(
            "close",
            "Current completed candle close used by the research threshold.",
            ("candles",),
            "current_candle.close",
            (),
        ),
    ),
)

# The event module imports the completed spec above; this late import breaks that cycle.
from .threshold_research_only_events import (  # noqa: E402
    build_threshold_research_only_events,
)


_runtime_factory = make_event_builder_runtime_factory(
    build_threshold_research_only_events,
    current_candle_only=True,
    pass_candle_index_offset=True,
    suppress_while_positioned=True,
)


def build_threshold_research_only_plugin() -> ResearchStrategyPlugin:
    from market_research.research.strategy_manifest import (
        builtin_strategy_manifest_hash,
    )

    return ResearchStrategyPlugin(
        name=THRESHOLD_RESEARCH_ONLY_SPEC.strategy_name,
        version=THRESHOLD_RESEARCH_ONLY_SPEC.strategy_version,
        spec=THRESHOLD_RESEARCH_ONLY_SPEC,
        required_data=THRESHOLD_RESEARCH_ONLY_SPEC.required_data,
        optional_data=THRESHOLD_RESEARCH_ONLY_SPEC.optional_data,
        event_builder=build_threshold_research_only_events,
        decision_contract_version=THRESHOLD_RESEARCH_ONLY_SPEC.decision_contract_version,
        diagnostics_namespace="threshold_research_only",
        runtime_factory=_runtime_factory,
        reconstruction_module=__name__,
        reconstruction_qualname="build_threshold_research_only_plugin",
        package_manifest_hash=builtin_strategy_manifest_hash(__name__),
    )


STRATEGY_PLUGIN_FACTORY = build_threshold_research_only_plugin

__all__ = ["build_threshold_research_only_plugin", "STRATEGY_PLUGIN_FACTORY"]
