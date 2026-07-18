"""Built-in noop plugin implementation."""

from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_spec import (
    StrategyFeatureDefinition,
    StrategyParameterSchema,
    StrategyRuleDeclaration,
    StrategyRuleSpec,
    StrategySpec,
)
from market_research.strategy_sdk.runtime import make_event_builder_runtime_factory

NOOP_BASELINE_SPEC = StrategySpec(
    strategy_name="noop_baseline",
    strategy_version="noop_baseline.research_contract.v1",
    accepted_parameter_names=("NOOP_DECISION_START_INDEX", "NOOP_DECISION_REASON"),
    required_parameter_names=(),
    behavior_affecting_parameter_names=(
        "NOOP_DECISION_START_INDEX",
        "NOOP_DECISION_REASON",
    ),
    metadata_only_parameter_names=(),
    research_only_parameter_names=(),
    default_parameters={
        "NOOP_DECISION_START_INDEX": 0,
        "NOOP_DECISION_REASON": "noop_baseline_hold",
    },
    decision_contract_version="research_noop_baseline_decision_contract.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={
        "schema_version": 1,
        "rules": (),
        "description": "No-op baseline never emits executable entry or exit intent.",
    },
    parameter_schema=(
        StrategyParameterSchema(
            "NOOP_DECISION_START_INDEX",
            "int",
            min_value=0,
            unit="candle_index",
            description="First completed-candle index at which HOLD is emitted.",
            default_value=0,
            optimization_allowed=False,
            since_version="noop_baseline.research_contract.v1",
        ),
        StrategyParameterSchema(
            "NOOP_DECISION_REASON",
            "str",
            unit="label",
            description="Audit reason attached to deterministic HOLD decisions.",
            default_value="noop_baseline_hold",
            optimization_allowed=False,
            since_version="noop_baseline.research_contract.v1",
        ),
    ),
    rule_spec=StrategyRuleSpec(
        1,
        entry=StrategyRuleDeclaration("noop_hold", "Always emit HOLD.", "always"),
        take_profit=StrategyRuleDeclaration(
            "take_profit", "No take-profit exit.", "never"
        ),
        edge_invalidation=StrategyRuleDeclaration(
            "edge_invalidation", "No edge-invalidation exit.", "never"
        ),
        time_exit=StrategyRuleDeclaration("time_exit", "No time exit.", "never"),
        stop_loss=StrategyRuleDeclaration("stop_loss", "No stop-loss exit.", "never"),
        position_sizing=StrategyRuleDeclaration(
            "no_position", "Never allocate capital.", "always"
        ),
        entry_prohibitions=(
            StrategyRuleDeclaration("all_entries", "Block every entry.", "always"),
        ),
    ),
    feature_definitions=(
        StrategyFeatureDefinition(
            "decision_index",
            "Zero-based candle index used only to emit deterministic HOLD decisions.",
            ("candles",),
            "candle_index",
            ("NOOP_DECISION_START_INDEX",),
        ),
    ),
)

# The event module imports the completed spec above; this late import breaks that cycle.
from .noop_baseline_events import build_noop_baseline_events  # noqa: E402


_runtime_factory = make_event_builder_runtime_factory(
    build_noop_baseline_events,
    current_candle_only=True,
    pass_candle_index_offset=True,
)


def build_noop_baseline_plugin() -> ResearchStrategyPlugin:
    from market_research.research.strategy_manifest import (
        builtin_strategy_manifest_hash,
    )

    return ResearchStrategyPlugin(
        name=NOOP_BASELINE_SPEC.strategy_name,
        version=NOOP_BASELINE_SPEC.strategy_version,
        spec=NOOP_BASELINE_SPEC,
        required_data=NOOP_BASELINE_SPEC.required_data,
        optional_data=NOOP_BASELINE_SPEC.optional_data,
        event_builder=build_noop_baseline_events,
        decision_contract_version=NOOP_BASELINE_SPEC.decision_contract_version,
        diagnostics_namespace="noop_baseline",
        runtime_factory=_runtime_factory,
        reconstruction_module=__name__,
        reconstruction_qualname="build_noop_baseline_plugin",
        package_manifest_hash=builtin_strategy_manifest_hash(__name__),
    )


STRATEGY_PLUGIN_FACTORY = build_noop_baseline_plugin

__all__ = ["build_noop_baseline_plugin", "STRATEGY_PLUGIN_FACTORY"]
