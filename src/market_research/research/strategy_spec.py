from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, SupportsFloat, SupportsIndex, cast

if TYPE_CHECKING:
    from .strategy_registry import StrategyRegistry

from .research_classification import requires_candidate_validation
from .feature_definition import (
    FeatureDefinition,
    validate_feature_definition_set,
)
from .hashing import sha256_prefixed
from .immutable_contract import canonical_mutable, deep_freeze


class StrategySpecError(ValueError):
    pass


# Compatibility name for the public strategy-extension API.  Strategy and
# diagnostic runtimes now share the same versioned feature authority.
StrategyFeatureDefinition = FeatureDefinition


@dataclass(frozen=True, slots=True)
class StrategyRuleDeclaration:
    rule_id: str
    description: str
    enabled_when: str
    parameter_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.rule_id.strip()
            or not self.description.strip()
            or not self.enabled_when.strip()
        ):
            raise StrategySpecError(
                "strategy rule id, description, and enabled_when are required"
            )
        if len(set(self.parameter_names)) != len(self.parameter_names):
            raise StrategySpecError(
                f"strategy rule has duplicate parameter binding:{self.rule_id}"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "description": self.description,
            "enabled_when": self.enabled_when,
            "parameter_names": list(self.parameter_names),
        }


@dataclass(frozen=True, slots=True)
class StrategyRuleSpec:
    schema_version: int
    entry: StrategyRuleDeclaration
    take_profit: StrategyRuleDeclaration
    edge_invalidation: StrategyRuleDeclaration
    time_exit: StrategyRuleDeclaration
    stop_loss: StrategyRuleDeclaration
    position_sizing: StrategyRuleDeclaration
    entry_prohibitions: tuple[StrategyRuleDeclaration, ...] = ()
    additional_exits: tuple[StrategyRuleDeclaration, ...] = ()
    exit_priority: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise StrategySpecError("strategy rule spec schema_version must be 1")
        declarations = (
            self.entry,
            self.take_profit,
            self.edge_invalidation,
            self.time_exit,
            self.stop_loss,
            self.position_sizing,
            *self.entry_prohibitions,
            *self.additional_exits,
        )
        ids = [item.rule_id for item in declarations]
        if len(set(ids)) != len(ids):
            raise StrategySpecError("strategy rule ids must be unique")
        exit_ids = {
            self.take_profit.rule_id,
            self.edge_invalidation.rule_id,
            self.time_exit.rule_id,
            self.stop_loss.rule_id,
            *(item.rule_id for item in self.additional_exits),
        }
        if len(set(self.exit_priority)) != len(self.exit_priority):
            raise StrategySpecError(
                "strategy exit priority must not contain duplicates"
            )
        unknown_priority = sorted(set(self.exit_priority) - exit_ids)
        if unknown_priority:
            raise StrategySpecError(
                "strategy exit priority references unknown rule(s): "
                + ",".join(unknown_priority)
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "entry": self.entry.as_dict(),
            "take_profit": self.take_profit.as_dict(),
            "edge_invalidation": self.edge_invalidation.as_dict(),
            "time_exit": self.time_exit.as_dict(),
            "stop_loss": self.stop_loss.as_dict(),
            "position_sizing": self.position_sizing.as_dict(),
            "entry_prohibitions": [item.as_dict() for item in self.entry_prohibitions],
            "additional_exits": [item.as_dict() for item in self.additional_exits],
            "exit_priority": list(self.exit_priority),
        }

    def parameter_names(self) -> tuple[str, ...]:
        declarations = (
            self.entry,
            self.take_profit,
            self.edge_invalidation,
            self.time_exit,
            self.stop_loss,
            self.position_sizing,
            *self.entry_prohibitions,
            *self.additional_exits,
        )
        return tuple(
            sorted({name for item in declarations for name in item.parameter_names})
        )


@dataclass(frozen=True)
class StrategyParameterSchema:
    name: str
    value_type: str
    required: bool = False
    min_value: float | None = None
    max_value: float | None = None
    enum: tuple[object, ...] = ()
    unit: str = ""
    runtime_bound: bool = True
    behavior_affecting: bool = True
    deprecated_keys: tuple[str, ...] = ()
    migration_rule: str = ""
    description: str = ""
    default_value: object | None = None
    optimization_allowed: bool = True
    runtime_mutable: bool = False
    since_version: str = "1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.value_type,
            "required": bool(self.required),
            "min": self.min_value,
            "max": self.max_value,
            "enum": list(self.enum),
            "unit": self.unit,
            "runtime_bound": bool(self.runtime_bound),
            "behavior_affecting": bool(self.behavior_affecting),
            "deprecated_keys": list(self.deprecated_keys),
            "migration_rule": self.migration_rule,
            "description": self.description,
            "default": canonical_mutable(self.default_value),
            "optimization_allowed": bool(self.optimization_allowed),
            "runtime_mutable": bool(self.runtime_mutable),
            "since_version": self.since_version,
        }

    def validate(self, value: object) -> None:
        if self.value_type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise StrategySpecError(f"{self.name} must be int")
            numeric = int(value)
            comparable: float | str | bool = float(numeric)
        elif self.value_type == "float":
            try:
                float_input = cast(
                    str | bytes | bytearray | SupportsFloat | SupportsIndex, value
                )
                numeric_float = float(float_input)
            except (TypeError, ValueError) as exc:
                raise StrategySpecError(f"{self.name} must be float") from exc
            if not math.isfinite(numeric_float):
                raise StrategySpecError(f"{self.name} must be finite")
            comparable = numeric_float
        elif self.value_type == "bool":
            if not isinstance(value, bool):
                raise StrategySpecError(f"{self.name} must be bool")
            comparable = bool(value)
        elif self.value_type == "str":
            if not isinstance(value, str):
                raise StrategySpecError(f"{self.name} must be str")
            comparable = value
        else:
            raise StrategySpecError(
                f"{self.name} has unsupported schema type:{self.value_type}"
            )
        if self.enum and value not in self.enum:
            raise StrategySpecError(
                f"{self.name} must be one of {','.join(map(str, self.enum))}"
            )
        if isinstance(comparable, float):
            if self.min_value is not None and comparable < float(self.min_value):
                raise StrategySpecError(f"{self.name} must be >= {self.min_value}")
            if self.max_value is not None and comparable > float(self.max_value):
                raise StrategySpecError(f"{self.name} must be <= {self.max_value}")


@dataclass(frozen=True)
class StrategySpec:
    strategy_name: str
    strategy_version: str
    accepted_parameter_names: tuple[str, ...]
    required_parameter_names: tuple[str, ...]
    behavior_affecting_parameter_names: tuple[str, ...]
    metadata_only_parameter_names: tuple[str, ...]
    research_only_parameter_names: tuple[str, ...]
    default_parameters: dict[str, Any]
    decision_contract_version: str
    required_data: tuple[str, ...]
    optional_data: tuple[str, ...]
    exit_policy_schema: dict[str, Any]
    parameter_schema: tuple[StrategyParameterSchema, ...] = ()
    rule_spec: StrategyRuleSpec | None = None
    feature_definitions: tuple[FeatureDefinition, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "default_parameters", deep_freeze(self.default_parameters)
        )
        object.__setattr__(
            self, "exit_policy_schema", deep_freeze(self.exit_policy_schema)
        )
        if self.rule_spec is not None:
            unknown = sorted(
                set(self.rule_spec.parameter_names())
                - set(self.accepted_parameter_names)
            )
            if unknown:
                raise StrategySpecError(
                    "strategy rule spec references unknown parameter(s): "
                    + ",".join(unknown)
                )
        validate_feature_definition_set(self.feature_definitions)
        unknown_feature_parameters = sorted(
            {
                name
                for feature in self.feature_definitions
                for name in (
                    *feature.lookback_parameter_names,
                    *feature.warm_up_parameter_names,
                )
                if name not in self.accepted_parameter_names
            }
        )
        if unknown_feature_parameters:
            raise StrategySpecError(
                "strategy feature references unknown parameter(s): "
                + ",".join(unknown_feature_parameters)
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "accepted_parameter_names": list(self.accepted_parameter_names),
            "required_parameter_names": list(self.required_parameter_names),
            "behavior_affecting_parameter_names": list(
                self.behavior_affecting_parameter_names
            ),
            "metadata_only_parameter_names": list(self.metadata_only_parameter_names),
            "research_only_parameter_names": list(self.research_only_parameter_names),
            "default_parameters": canonical_mutable(self.default_parameters),
            "decision_contract_version": self.decision_contract_version,
            "required_data": list(self.required_data),
            "optional_data": list(self.optional_data),
            "exit_policy_schema": canonical_mutable(self.exit_policy_schema),
            "parameter_schema": [item.as_dict() for item in self.parameter_schema],
            "rule_spec": self.rule_spec.as_dict()
            if self.rule_spec is not None
            else None,
            "feature_definitions": [
                item.as_dict() for item in self.feature_definitions
            ],
        }

    def validate_parameters(self, parameter_values: dict[str, Any]) -> None:
        schemas = {item.name: item for item in self.parameter_schema}
        for parameter_schema in schemas.values():
            if (
                parameter_schema.required
                and parameter_schema.name not in parameter_values
            ):
                raise StrategySpecError(
                    f"missing required strategy parameter(s): {parameter_schema.name}"
                )
        if schemas:
            unknown = sorted(set(parameter_values) - set(self.accepted_parameter_names))
            if unknown:
                raise StrategySpecError(
                    f"unknown strategy parameter(s): {','.join(unknown)}"
                )
        for name, value in parameter_values.items():
            active_schema = schemas.get(name)
            if active_schema is not None:
                active_schema.validate(value)

    def spec_hash(self) -> str:
        return sha256_prefixed(self.as_dict())


def strategy_spec_for_name(
    strategy_name: str, *, registry: "StrategyRegistry | None" = None
) -> StrategySpec:
    """Resolve only through an explicitly supplied composition authority."""
    if registry is None:
        raise StrategySpecError("explicit strategy registry required")
    try:
        return registry.resolve(strategy_name).spec
    except ValueError as exc:
        raise StrategySpecError(
            f"unsupported research strategy: {strategy_name}"
        ) from exc


def runtime_bound_behavior_parameter_names(
    strategy_name: str, *, registry: Any
) -> tuple[str, ...]:
    return runtime_bound_behavior_parameter_names_from_spec(
        strategy_spec_for_name(strategy_name, registry=registry)
    )


def runtime_bound_behavior_parameter_names_from_spec(
    spec: StrategySpec,
) -> tuple[str, ...]:
    """Return runtime-bound behavior names using only the supplied authority."""
    research_only = set(spec.research_only_parameter_names)
    return tuple(
        name
        for name in spec.behavior_affecting_parameter_names
        if name not in research_only
    )


def validate_parameter_space_against_strategy_spec(
    *,
    strategy_name: str,
    parameter_space: dict[str, tuple[object, ...]],
    research_classification: str,
    spec: StrategySpec | None = None,
    registry: Any | None = None,
) -> StrategySpec:
    spec = spec or strategy_spec_for_name(strategy_name, registry=registry)
    accepted = set(spec.accepted_parameter_names)
    unknown = sorted(key for key in parameter_space if key not in accepted)
    if unknown:
        raise StrategySpecError(f"unknown strategy parameter(s): {','.join(unknown)}")
    missing = sorted(
        key for key in spec.required_parameter_names if key not in parameter_space
    )
    if missing:
        raise StrategySpecError(
            f"missing required strategy parameter(s): {','.join(missing)}"
        )
    metadata = sorted(
        key for key in parameter_space if key in set(spec.metadata_only_parameter_names)
    )
    if metadata and requires_candidate_validation(research_classification):
        raise StrategySpecError(
            "metadata-only strategy parameter(s) cannot be optimized for validation-bound manifests: "
            + ",".join(metadata)
        )
    research_only = sorted(
        key for key in parameter_space if key in set(spec.research_only_parameter_names)
    )
    if research_only and requires_candidate_validation(research_classification):
        raise StrategySpecError(
            "research-only strategy parameter(s) cannot be optimized for validation-bound manifests: "
            + ",".join(research_only)
        )
    if requires_candidate_validation(research_classification):
        runtime_bound_behavior = sorted(
            runtime_bound_behavior_parameter_names_from_spec(spec)
        )
        missing_behavior = [
            key for key in runtime_bound_behavior if key not in parameter_space
        ]
        if missing_behavior:
            raise StrategySpecError(
                "validation-bound manifests must declare every runtime-bound behavior-affecting "
                "strategy parameter: " + ",".join(missing_behavior)
            )
    _validate_exit_policy_parameter_values(parameter_space, spec=spec)
    return spec


def strategy_parameter_source_map(
    strategy_name: str,
    parameter_values: dict[str, Any],
    *,
    fee_rate: float | None = None,
    slippage_bps: float | None = None,
    registry: Any | None = None,
) -> dict[str, str]:
    return parameter_source_map_from_spec(
        strategy_spec_for_name(strategy_name, registry=registry),
        parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )


def parameter_source_map_from_spec(
    spec: StrategySpec,
    parameter_values: dict[str, Any],
    *,
    fee_rate: float | None = None,
    slippage_bps: float | None = None,
) -> dict[str, str]:
    raw = dict(parameter_values)
    sources = {key: "strategy_spec_default" for key in spec.default_parameters}
    for key in raw:
        sources[key] = "raw_parameter_values"
    if (
        fee_rate is not None
        and "LIVE_FEE_RATE_ESTIMATE" in spec.accepted_parameter_names
        and "LIVE_FEE_RATE_ESTIMATE" not in raw
    ):
        sources["LIVE_FEE_RATE_ESTIMATE"] = "cost_model_fee_rate"
    if (
        slippage_bps is not None
        and "STRATEGY_ENTRY_SLIPPAGE_BPS" in spec.accepted_parameter_names
        and "STRATEGY_ENTRY_SLIPPAGE_BPS" not in raw
    ):
        sources["STRATEGY_ENTRY_SLIPPAGE_BPS"] = "cost_model_slippage_bps"
    return sources


def materialize_strategy_parameters(
    strategy_name: str,
    parameter_values: dict[str, Any],
    *,
    fee_rate: float | None = None,
    slippage_bps: float | None = None,
    registry: Any | None = None,
) -> dict[str, Any]:
    return materialize_parameters_from_spec(
        strategy_spec_for_name(strategy_name, registry=registry),
        parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
    )


def materialize_parameters_from_spec(
    spec: StrategySpec,
    parameter_values: dict[str, Any],
    *,
    fee_rate: float | None = None,
    slippage_bps: float | None = None,
) -> dict[str, Any]:
    values = {**spec.default_parameters, **dict(parameter_values)}
    spec.validate_parameters(values)
    if (
        fee_rate is not None
        and "LIVE_FEE_RATE_ESTIMATE" in spec.accepted_parameter_names
        and "LIVE_FEE_RATE_ESTIMATE" not in parameter_values
    ):
        values["LIVE_FEE_RATE_ESTIMATE"] = float(fee_rate)
    if (
        slippage_bps is not None
        and "STRATEGY_ENTRY_SLIPPAGE_BPS" in spec.accepted_parameter_names
        and "STRATEGY_ENTRY_SLIPPAGE_BPS" not in parameter_values
    ):
        values["STRATEGY_ENTRY_SLIPPAGE_BPS"] = float(slippage_bps)
    _validate_exit_policy_materialized_values(values, spec=spec)
    return values


def materialized_strategy_parameters_hash(parameter_values: dict[str, Any]) -> str:
    return sha256_prefixed(dict(parameter_values))


COMMON_EXIT_RULE_NAMES = frozenset({"stop_loss", "max_holding_time", "take_profit"})


def exit_policy_materialization_from_parameters(
    strategy_name: str,
    parameter_values: dict[str, Any],
    *,
    materialization_mode: str = "research_validation",
    registry: Any | None = None,
) -> Any:
    from .strategy_contract import normalize_exit_policy_materialization

    if registry is None:
        raise StrategySpecError("explicit strategy registry required")
    spec = strategy_spec_for_name(strategy_name, registry=registry)
    plugin = registry.resolve(strategy_name)
    materializer = getattr(plugin, "exit_policy_materializer", None)
    if materializer is not None:
        result = materializer(strategy_name, dict(parameter_values))
        return normalize_exit_policy_materialization(
            result,
            strategy_name=strategy_name,
            materializer=materializer,
            default_source="plugin_exit_policy_materializer",
            default_mode=materialization_mode,
        )
    schema_rules = tuple(
        str(rule).strip().lower() for rule in spec.exit_policy_schema.get("rules") or ()
    )
    if not schema_rules:
        policy = _no_exit_policy(strategy_name)
        return normalize_exit_policy_materialization(
            {
                "exit_policy": policy,
                "exit_policy_config": {
                    "schema_version": 1,
                    "strategy_name": strategy_name,
                    "rules": [],
                },
                "exit_policy_source": "default_no_exit_materializer",
                "exit_policy_materialization_mode": materialization_mode,
            },
            strategy_name=strategy_name,
            materializer=None,
            default_source="default_no_exit_materializer",
            default_mode=materialization_mode,
        )
    strategy_owned = sorted(set(schema_rules) - COMMON_EXIT_RULE_NAMES)
    if strategy_owned:
        raise StrategySpecError(
            "strategy exit policy materializer required for strategy-owned rule(s): "
            + ",".join(strategy_owned)
        )
    policy = _common_exit_policy_from_parameters(
        strategy_name, parameter_values, spec=spec
    )
    return normalize_exit_policy_materialization(
        {
            "exit_policy": policy,
            "exit_policy_config": _common_exit_policy_config(policy),
            "exit_policy_source": "default_common_exit_policy_materializer",
            "exit_policy_materialization_mode": materialization_mode,
        },
        strategy_name=strategy_name,
        materializer=None,
        default_source="default_common_exit_policy_materializer",
        default_mode=materialization_mode,
    )


def exit_policy_from_parameters(
    strategy_name: str, parameter_values: dict[str, Any], *, registry: Any | None = None
) -> dict[str, Any]:
    return dict(
        exit_policy_materialization_from_parameters(
            strategy_name, parameter_values, registry=registry
        ).exit_policy
    )


def _no_exit_policy(strategy_name: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "strategy_name": strategy_name,
        "rules": [],
        "common_rules": [],
        "strategy_rules": [],
        "entry_exit_policy": "strategy_emits_no_exit_intent",
        "stop_loss": {"enabled": False, "disabled_when_zero": True},
        "max_holding_time": {"enabled": False, "disabled_when_zero": True},
        "take_profit": {"enabled": False, "disabled_when_zero": True},
    }


def _common_exit_policy_from_parameters(
    strategy_name: str, parameter_values: dict[str, Any], *, spec: StrategySpec
) -> dict[str, Any]:
    values = materialize_parameters_from_spec(spec, parameter_values)
    rules = _normalize_exit_rule_names(str(values.get("STRATEGY_EXIT_RULES") or ""))
    _validate_common_exit_rule_names(",".join(rules))
    common_rules = tuple(rule for rule in rules if rule in COMMON_EXIT_RULE_NAMES)
    stop_loss_ratio = float(values.get("STRATEGY_EXIT_STOP_LOSS_RATIO") or 0.0)
    max_holding_min = int(values.get("STRATEGY_EXIT_MAX_HOLDING_MIN") or 0)
    take_profit_ratio = float(values.get("TAKE_PROFIT_RATIO") or 0.0)
    trailing_stop_ratio = float(values.get("TRAILING_STOP_RATIO") or 0.0)
    break_even_stop_enabled = bool(values.get("BREAK_EVEN_STOP_ENABLED"))
    opposite_signal_exit_enabled = bool(values.get("OPPOSITE_SIGNAL_EXIT_ENABLED"))
    regime_change_exit_enabled = bool(values.get("REGIME_CHANGE_EXIT_ENABLED"))
    return {
        "schema_version": 1,
        "strategy_name": strategy_name,
        "rules": list(rules),
        "common_rules": list(common_rules),
        "strategy_rules": [],
        "stop_loss": {
            "enabled": "stop_loss" in rules and stop_loss_ratio > 0.0,
            "stop_loss_ratio": stop_loss_ratio,
            "disabled_when_zero": True,
            "evaluation_price_basis": "closed_candle_mark",
            "intrabar_stop_modeled": False,
            "limitation_reasons": [
                "intra_candle_path_unavailable",
                "candle_close_stop_may_exit_later_than_real_stop",
            ],
        },
        "max_holding_time": {
            "enabled": "max_holding_time" in rules and max_holding_min > 0,
            "max_holding_min": max_holding_min,
            "disabled_when_zero": True,
        },
        "take_profit": {
            "enabled": "take_profit" in rules and take_profit_ratio > 0.0,
            "take_profit_ratio": take_profit_ratio,
            "disabled_when_zero": True,
            "evaluation_price_basis": "closed_candle_mark",
        },
        "trailing_stop": {
            "enabled": trailing_stop_ratio > 0.0,
            "trailing_stop_ratio": trailing_stop_ratio,
            "disabled_when_zero": True,
            "evaluation_status": "diagnostic_policy_bound_not_runtime_evaluated",
        },
        "break_even_stop": {
            "enabled": break_even_stop_enabled,
            "evaluation_status": "diagnostic_policy_bound_not_runtime_evaluated",
        },
        "opposite_signal_exit": {
            "enabled": opposite_signal_exit_enabled,
            "evaluation_status": "diagnostic_policy_bound_not_runtime_evaluated",
        },
        "regime_change_exit": {
            "enabled": regime_change_exit_enabled,
            "evaluation_status": "diagnostic_policy_bound_not_runtime_evaluated",
        },
    }


def _common_exit_policy_config(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "strategy_name": policy.get("strategy_name"),
        "rules": list(policy.get("rules") or []),
        "stop_loss": dict(policy.get("stop_loss") or {}),
        "max_holding_time": dict(policy.get("max_holding_time") or {}),
        "take_profit": dict(policy.get("take_profit") or {}),
        "trailing_stop": dict(policy.get("trailing_stop") or {}),
        "break_even_stop": dict(policy.get("break_even_stop") or {}),
        "opposite_signal_exit": dict(policy.get("opposite_signal_exit") or {}),
        "regime_change_exit": dict(policy.get("regime_change_exit") or {}),
    }


def exit_policy_hash(policy: dict[str, Any]) -> str:
    return sha256_prefixed(policy)


def _normalize_exit_rule_names(raw: str) -> tuple[str, ...]:
    return tuple(token.strip().lower() for token in raw.split(",") if token.strip())


def _allowed_exit_rule_names(spec: StrategySpec) -> set[str]:
    configured = spec.exit_policy_schema.get("allowed_rules")
    if configured is None:
        configured = spec.exit_policy_schema.get("rules") or ()
    return {str(item).strip().lower() for item in configured if str(item).strip()}


def _validate_strategy_exit_rule_names(raw: object, *, spec: StrategySpec) -> None:
    if not isinstance(raw, str):
        raise StrategySpecError("STRATEGY_EXIT_RULES must be str")
    unsupported = sorted(
        set(_normalize_exit_rule_names(raw)) - _allowed_exit_rule_names(spec)
    )
    if unsupported:
        raise StrategySpecError(
            "STRATEGY_EXIT_RULES contains unsupported rule(s): " + ",".join(unsupported)
        )


def _validate_exit_policy_parameter_values(
    parameter_space: dict[str, tuple[object, ...]], *, spec: StrategySpec
) -> None:
    rules_values = parameter_space.get("STRATEGY_EXIT_RULES")
    if rules_values is not None:
        for raw_rules in rules_values:
            _validate_strategy_exit_rule_names(raw_rules, spec=spec)
    ratio_values = parameter_space.get("STRATEGY_EXIT_STOP_LOSS_RATIO")
    _validate_ratio_rule_pair(
        values=ratio_values,
        rules_values=rules_values,
        parameter_name="STRATEGY_EXIT_STOP_LOSS_RATIO",
        rule_name="stop_loss",
    )
    _validate_ratio_rule_pair(
        values=parameter_space.get("STRATEGY_EXIT_TAKE_PROFIT_RATIO"),
        rules_values=rules_values,
        parameter_name="STRATEGY_EXIT_TAKE_PROFIT_RATIO",
        rule_name="take_profit",
    )
    _validate_ratio_rule_pair(
        values=parameter_space.get("STRATEGY_EXIT_MIN_EDGE_RATIO"),
        rules_values=rules_values,
        parameter_name="STRATEGY_EXIT_MIN_EDGE_RATIO",
        rule_name="edge_invalidation",
    )


def _validate_exit_policy_materialized_values(
    values: dict[str, Any], *, spec: StrategySpec
) -> None:
    stop_loss_ratio = _non_negative_float(
        "STRATEGY_EXIT_STOP_LOSS_RATIO",
        values.get("STRATEGY_EXIT_STOP_LOSS_RATIO", 0.0),
    )
    take_profit_ratio = _non_negative_float(
        "STRATEGY_EXIT_TAKE_PROFIT_RATIO",
        values.get("STRATEGY_EXIT_TAKE_PROFIT_RATIO", 0.0),
    )
    min_edge_ratio = _non_negative_float(
        "STRATEGY_EXIT_MIN_EDGE_RATIO", values.get("STRATEGY_EXIT_MIN_EDGE_RATIO", 0.0)
    )
    _validate_strategy_exit_rule_names(
        values.get("STRATEGY_EXIT_RULES") or "", spec=spec
    )
    rules = _normalize_exit_rule_names(str(values.get("STRATEGY_EXIT_RULES") or ""))
    if stop_loss_ratio > 0.0 and "stop_loss" not in rules:
        raise StrategySpecError(
            "STRATEGY_EXIT_STOP_LOSS_RATIO is positive but STRATEGY_EXIT_RULES does not include stop_loss"
        )
    if take_profit_ratio > 0.0 and "take_profit" not in rules:
        raise StrategySpecError(
            "STRATEGY_EXIT_TAKE_PROFIT_RATIO is positive but STRATEGY_EXIT_RULES does not include take_profit"
        )
    if min_edge_ratio > 0.0 and "edge_invalidation" not in rules:
        raise StrategySpecError(
            "STRATEGY_EXIT_MIN_EDGE_RATIO is positive but STRATEGY_EXIT_RULES does not include edge_invalidation"
        )


def _validate_ratio_rule_pair(
    *,
    values: tuple[object, ...] | None,
    rules_values: tuple[object, ...] | None,
    parameter_name: str,
    rule_name: str,
) -> None:
    if values is None:
        return
    for raw_ratio in values:
        ratio = _non_negative_float(parameter_name, raw_ratio)
        if ratio <= 0.0 or rules_values is None:
            continue
        for raw_rules in rules_values:
            rules = _normalize_exit_rule_names(str(raw_rules or ""))
            if rule_name not in rules:
                raise StrategySpecError(
                    f"{parameter_name} is positive but STRATEGY_EXIT_RULES does not include {rule_name}"
                )


def _non_negative_float(name: str, value: object) -> float:
    try:
        numeric = cast(str | bytes | bytearray | SupportsFloat | SupportsIndex, value)
        resolved = float(numeric)
    except (TypeError, ValueError) as exc:
        raise StrategySpecError(
            f"{name} must be a finite value >= 0, got {value!r}"
        ) from exc
    if not math.isfinite(resolved) or resolved < 0.0:
        raise StrategySpecError(f"{name} must be a finite value >= 0, got {value!r}")
    return resolved


def _validate_common_exit_rule_names(
    raw: object,
    *,
    allow_strategy_owned_rule: str | None = None,
) -> None:
    if not isinstance(raw, str):
        raise StrategySpecError("STRATEGY_EXIT_RULES must be str")
    supported = set(COMMON_EXIT_RULE_NAMES)
    if allow_strategy_owned_rule:
        supported.add(str(allow_strategy_owned_rule))
    rules = _normalize_exit_rule_names(raw)
    unsupported = sorted(set(rules) - supported)
    if unsupported:
        raise StrategySpecError(
            "STRATEGY_EXIT_RULES contains unsupported rule(s): " + ",".join(unsupported)
        )
