"""Single authority for compiling raw strategy inputs into executable evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, NoReturn

from .backtest_types import BacktestRunContext
from .hashing import sha256_prefixed
from .strategy_contract import (
    CompiledStrategyContract,
    ENGINE_CAPABILITY_SUPPORT,
    ENGINE_SUPPORTED_CAPABILITIES,
    MaterializedParameterSet,
    ParameterExtensionContext,
    ParameterExtensionResult,
    is_sha256_hash,
    normalize_exit_policy_materialization,
)
from .strategy_registry import StrategyRegistry
from .immutable_contract import canonical_mutable
from .strategy_spec import (
    materialize_parameters_from_spec,
    parameter_source_map_from_spec,
)


class StrategyCompilationError(ValueError):
    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(reason_code + (f":{detail}" if detail else ""))


class StrategyCompiler:
    SCHEMA_VERSION = 2

    def __init__(self, registry: StrategyRegistry) -> None:
        self.registry = registry

    def compile(
        self,
        *,
        strategy_name: str,
        raw_parameters: dict[str, Any],
        fee_rate: float,
        slippage_bps: float,
        context: BacktestRunContext | None = None,
    ) -> CompiledStrategyContract:
        plugin = self.registry.resolve(strategy_name)
        required = plugin.required_capabilities
        mismatches = list(ENGINE_CAPABILITY_SUPPORT.unsupported_fields(required))
        if mismatches:
            raise StrategyCompilationError(
                "unsupported_strategy_capability", ",".join(mismatches)
            )
        raw = dict(raw_parameters)
        baseline = materialize_parameters_from_spec(
            plugin.spec, raw, fee_rate=fee_rate, slippage_bps=slippage_bps
        )
        sources = parameter_source_map_from_spec(
            plugin.spec, raw, fee_rate=fee_rate, slippage_bps=slippage_bps
        )
        if plugin.parameter_materializer is not None:
            result = plugin.parameter_materializer(
                materialized=MaterializedParameterSet(values=baseline, sources=sources),
                context=ParameterExtensionContext(
                    strategy_name=plugin.name,
                    strategy_version=plugin.version,
                    policy_materialization_mode=str(
                        getattr(context, "policy_materialization_mode", "research_only")
                    ),
                ),
            )
            if not isinstance(result, ParameterExtensionResult):
                raise StrategyCompilationError("parameter_extension_result_invalid")
            values = dict(result.values)
            if set(values) != set(baseline):
                raise StrategyCompilationError("parameter_extension_key_set_changed")
            changed = {key for key in values if values[key] != baseline[key]}
            overrides = dict(result.source_overrides)
            if set(overrides) != changed:
                raise StrategyCompilationError(
                    "parameter_extension_source_overrides_invalid",
                    ",".join(sorted(changed.symmetric_difference(overrides))),
                )
            invalid_sources = sorted(
                key
                for key, value in overrides.items()
                if value != "plugin_parameter_materializer"
            )
            if invalid_sources:
                raise StrategyCompilationError(
                    "parameter_extension_source_override_invalid",
                    ",".join(invalid_sources),
                )
            sources.update(overrides)
        else:
            values = baseline
        values = dict(values)
        plugin.spec.validate_parameters(values)
        missing = sorted(set(values) - set(sources))
        if missing:
            raise StrategyCompilationError(
                "unrecorded_behavior_default", ",".join(missing)
            )
        policy = None
        if plugin.exit_policy_materializer is not None:
            policy = normalize_exit_policy_materialization(
                plugin.exit_policy_materializer(plugin.name, values),
                strategy_name=plugin.name,
                materializer=plugin.exit_policy_materializer,
                default_source="strategy_plugin",
                default_mode=str(
                    getattr(context, "policy_materialization_mode", "research_only")
                ),
            ).exit_policy
        requirements = plugin.data_requirements(values).capability_contract_payload()
        capability = required.as_dict()
        base = {
            "schema_version": self.SCHEMA_VERSION,
            "strategy_name": plugin.name,
            "strategy_version": plugin.version,
            "raw_parameters": raw,
            "materialized_parameters": values,
            "parameter_source_map": sources,
            "materialized_parameters_hash": sha256_prefixed(values),
            "data_requirements": requirements,
            "exit_policy": policy,
            "exit_mode": plugin.exit_mode,
            "capability_contract": capability,
            "capability_contract_hash": required.contract_hash(),
            "strategy_plugin_contract_hash": plugin.contract_hash(),
            "strategy_registry_hash": self.registry.execution_scope_hash(plugin.name),
        }
        compiled_hash = sha256_prefixed(base)
        contract = CompiledStrategyContract(
            schema_version=self.SCHEMA_VERSION,
            strategy_name=plugin.name,
            strategy_version=plugin.version,
            raw_parameters=raw,
            materialized_parameters=values,
            parameter_source_map=sources,
            materialized_parameters_hash=sha256_prefixed(values),
            data_requirements=requirements,
            exit_policy=dict(policy) if policy is not None else None,
            exit_mode=plugin.exit_mode,
            capability_contract=capability,
            capability_contract_hash=required.contract_hash(),
            strategy_plugin_contract_hash=plugin.contract_hash(),
            strategy_registry_hash=self.registry.execution_scope_hash(plugin.name),
            compiled_contract_hash=compiled_hash,
        )
        return validate_compiled_strategy_contract(contract)


_COMPILED_REQUIRED_FIELDS = frozenset(CompiledStrategyContract.__dataclass_fields__)


ALLOWED_PARAMETER_SOURCES = frozenset(
    {
        "raw_parameter_values",
        "strategy_spec_default",
        "plugin_parameter_materializer",
        "cost_model_fee_rate",
        "cost_model_slippage_bps",
    }
)
"""Versioned v2 parameter-value authorities accepted by compiled contracts."""

_DATA_REQUIREMENT_FIELDS = frozenset(
    {"schema_version", "required_data", "optional_data", "capabilities"}
)
_DATA_CAPABILITY_FIELDS = frozenset(
    {
        "name",
        "required",
        "min_coverage_pct",
        "source",
        "notes",
        "lookback_rows",
        "min_rows",
    }
)
_CAPABILITY_DIRECTIONS = frozenset({"long_only"})
_CAPABILITY_PORTFOLIO_MODES = frozenset({"single_asset_cash_qty"})


def _invalid(reason: str, detail: str = "") -> NoReturn:
    raise StrategyCompilationError(reason, detail)


def _is_canonical_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _is_canonical_json_value(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_is_canonical_json_value(item) for item in value)
    return False


def _typed_string_key_mapping(value: Mapping[object, object]) -> dict[str, object]:
    typed: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            _invalid("compiled_contract_noncanonical_nested_value")
        typed[key] = item
    return typed


def _validated_parameter_sources(
    values: Mapping[str, object],
    *,
    parameter_keys: set[str],
) -> dict[str, str]:
    if set(values) != parameter_keys:
        _invalid("compiled_contract_parameter_source_map_invalid")
    sources: dict[str, str] = {}
    for key, value in values.items():
        if (
            not isinstance(value, str)
            or not value
            or value not in ALLOWED_PARAMETER_SOURCES
        ):
            _invalid("compiled_contract_parameter_source_map_invalid")
        sources[key] = value
    return sources


def _validate_non_negative_optional_int(value: object, field: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        _invalid("compiled_contract_data_requirements_invalid", field)


def _validate_data_requirements(requirements: Mapping[str, object]) -> None:
    if (
        set(requirements) != _DATA_REQUIREMENT_FIELDS
        or requirements.get("schema_version") != 1
    ):
        _invalid("compiled_contract_data_requirements_invalid", "field_set_or_schema")
    required = requirements.get("required_data")
    optional = requirements.get("optional_data")
    capabilities = requirements.get("capabilities")
    if (
        not isinstance(required, (list, tuple))
        or not isinstance(optional, (list, tuple))
        or not isinstance(capabilities, (list, tuple))
    ):
        _invalid("compiled_contract_data_requirements_invalid", "collection_type")
    for label, values in (("required_data", required), ("optional_data", optional)):
        if any(not isinstance(item, str) or not item.strip() for item in values):
            _invalid("compiled_contract_data_requirements_invalid", label)
        if len(set(values)) != len(values):
            _invalid(
                "compiled_contract_data_requirements_invalid", f"duplicate_{label}"
            )
    if set(required) & set(optional):
        _invalid(
            "compiled_contract_data_requirements_invalid", "required_optional_conflict"
        )
    names: list[str] = []
    for index, item in enumerate(capabilities):
        if not isinstance(item, Mapping) or set(item) != _DATA_CAPABILITY_FIELDS:
            _invalid(
                "compiled_contract_data_requirements_invalid",
                f"capability_{index}_field_set",
            )
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            _invalid(
                "compiled_contract_data_requirements_invalid",
                f"capability_{index}_name",
            )
        if not isinstance(item.get("required"), bool):
            _invalid(
                "compiled_contract_data_requirements_invalid",
                f"capability_{index}_required",
            )
        coverage = item.get("min_coverage_pct")
        if coverage is not None and (
            isinstance(coverage, bool)
            or not isinstance(coverage, (int, float))
            or not math.isfinite(float(coverage))
            or not 0 <= float(coverage) <= 100
        ):
            _invalid(
                "compiled_contract_data_requirements_invalid",
                f"capability_{index}_coverage",
            )
        _validate_non_negative_optional_int(
            item.get("lookback_rows"), f"capability_{index}_lookback_rows"
        )
        _validate_non_negative_optional_int(
            item.get("min_rows"), f"capability_{index}_min_rows"
        )
        for field in ("source", "notes"):
            if item.get(field) is not None and not isinstance(item.get(field), str):
                _invalid(
                    "compiled_contract_data_requirements_invalid",
                    f"capability_{index}_{field}",
                )
        names.append(name)
    if len(set(names)) != len(names):
        _invalid("compiled_contract_data_requirements_invalid", "duplicate_capability")


def _validate_capability_contract(capability: Mapping[str, object]) -> None:
    fields = set(ENGINE_SUPPORTED_CAPABILITIES.as_dict())
    if set(capability) != fields or capability.get("schema_version") != 1:
        _invalid("compiled_contract_capability_payload_invalid", "field_set_or_schema")
    for field in (
        "instrument_count",
        "max_concurrent_positions",
        "max_intents_per_decision",
    ):
        value = capability.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            _invalid("compiled_contract_capability_payload_invalid", field)
    for field in ("pyramiding", "partial_exit"):
        if not isinstance(capability.get(field), bool):
            _invalid("compiled_contract_capability_payload_invalid", field)
    if capability.get("direction") not in _CAPABILITY_DIRECTIONS:
        _invalid("compiled_contract_capability_payload_invalid", "direction")
    if capability.get("portfolio_mode") not in _CAPABILITY_PORTFOLIO_MODES:
        _invalid("compiled_contract_capability_payload_invalid", "portfolio_mode")


def _validate_exit_policy(
    policy: object, *, exit_mode: object, strategy_name: str
) -> None:
    if exit_mode not in {"strategy_owned", "common_typed_policy"}:
        _invalid("compiled_contract_nested_payload_invalid", "exit_mode")
    if policy is None:
        if exit_mode == "common_typed_policy":
            _invalid("compiled_contract_exit_policy_invalid", "common_policy_missing")
        return
    if not isinstance(policy, Mapping):
        _invalid("compiled_contract_exit_policy_invalid", "mapping_required")
    if not {"schema_version", "strategy_name", "rules"}.issubset(policy):
        _invalid("compiled_contract_exit_policy_invalid", "required_field_missing")
    if (
        policy.get("schema_version") != 1
        or policy.get("strategy_name") != strategy_name
    ):
        _invalid("compiled_contract_exit_policy_invalid", "identity_or_schema")
    rules = policy.get("rules")
    if not isinstance(rules, (list, tuple)):
        _invalid("compiled_contract_exit_policy_invalid", "rules_collection")
    if any(not isinstance(rule, str) or not rule.strip() for rule in rules):
        _invalid("compiled_contract_exit_policy_invalid", "rule_name")
    if len(set(rules)) != len(rules):
        _invalid("compiled_contract_exit_policy_invalid", "duplicate_rule")
    if not _is_canonical_json_value(policy):
        _invalid("compiled_contract_exit_policy_invalid", "noncanonical_nested_value")


def validate_compiled_strategy_contract(
    contract_or_payload: CompiledStrategyContract | Mapping[str, Any],
    *,
    expected_strategy_name: str | None = None,
    expected_strategy_version: str | None = None,
    expected_registry_hash: str | None = None,
    expected_plugin_hash: str | None = None,
    expected_compiled_hash: str | None = None,
) -> CompiledStrategyContract:
    """Validate and freeze one complete v2 contract regardless of input representation."""
    original_contract = (
        contract_or_payload
        if isinstance(contract_or_payload, CompiledStrategyContract)
        else None
    )
    if isinstance(contract_or_payload, CompiledStrategyContract):
        payload = contract_or_payload.as_dict()
    elif isinstance(contract_or_payload, Mapping):
        payload = canonical_mutable(contract_or_payload)
    else:
        raise StrategyCompilationError("compiled_contract_payload_invalid")
    missing = sorted(_COMPILED_REQUIRED_FIELDS - set(payload))
    unknown = sorted(set(payload) - _COMPILED_REQUIRED_FIELDS)
    if missing:
        raise StrategyCompilationError(
            "compiled_contract_required_field_missing", ",".join(missing)
        )
    if unknown:
        raise StrategyCompilationError(
            "compiled_contract_unknown_field", ",".join(unknown)
        )
    material = dict(payload)
    recorded_value = material.pop("compiled_contract_hash")
    schema_version = material.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != StrategyCompiler.SCHEMA_VERSION
    ):
        raise StrategyCompilationError("compiled_contract_schema_version_unsupported")

    raw_parameters_value = material.get("raw_parameters")
    materialized_parameters_value = material.get("materialized_parameters")
    parameter_source_map_value = material.get("parameter_source_map")
    data_requirements_value = material.get("data_requirements")
    capability_contract_value = material.get("capability_contract")
    if not isinstance(raw_parameters_value, dict):
        raise StrategyCompilationError("compiled_contract_nested_payload_invalid")
    if not isinstance(materialized_parameters_value, dict):
        raise StrategyCompilationError("compiled_contract_nested_payload_invalid")
    if not isinstance(parameter_source_map_value, dict):
        raise StrategyCompilationError("compiled_contract_nested_payload_invalid")
    if not isinstance(data_requirements_value, dict):
        raise StrategyCompilationError("compiled_contract_nested_payload_invalid")
    if not isinstance(capability_contract_value, dict):
        raise StrategyCompilationError("compiled_contract_nested_payload_invalid")

    strategy_name = material.get("strategy_name")
    if not isinstance(strategy_name, str) or not strategy_name:
        raise StrategyCompilationError(
            "compiled_contract_nested_payload_invalid", "strategy_name"
        )

    strategy_version = material.get("strategy_version")
    if not isinstance(strategy_version, str) or not strategy_version:
        raise StrategyCompilationError(
            "compiled_contract_nested_payload_invalid", "strategy_version"
        )
    if not _is_canonical_json_value(material):
        raise StrategyCompilationError("compiled_contract_noncanonical_nested_value")

    raw_parameters = _typed_string_key_mapping(raw_parameters_value)
    materialized_parameters = _typed_string_key_mapping(materialized_parameters_value)
    parameter_source_values = _typed_string_key_mapping(parameter_source_map_value)
    data_requirements = _typed_string_key_mapping(data_requirements_value)
    capability = _typed_string_key_mapping(capability_contract_value)
    parameter_keys = set(materialized_parameters)
    parameter_source_map = _validated_parameter_sources(
        parameter_source_values,
        parameter_keys=parameter_keys,
    )
    _validate_data_requirements(data_requirements)
    _validate_capability_contract(capability)

    exit_policy_value = material.get("exit_policy")
    exit_mode_value = material.get("exit_mode")
    _validate_exit_policy(
        exit_policy_value,
        exit_mode=exit_mode_value,
        strategy_name=strategy_name,
    )
    if not isinstance(exit_mode_value, str):
        raise StrategyCompilationError(
            "compiled_contract_nested_payload_invalid", "exit_mode"
        )
    exit_mode = exit_mode_value
    exit_policy = (
        _typed_string_key_mapping(exit_policy_value)
        if isinstance(exit_policy_value, Mapping)
        else None
    )

    hash_fields = (
        "materialized_parameters_hash",
        "capability_contract_hash",
        "strategy_plugin_contract_hash",
        "strategy_registry_hash",
    )
    validated_hashes: dict[str, str] = {}
    for name in hash_fields:
        value = material.get(name)
        if not isinstance(value, str) or not is_sha256_hash(value):
            raise StrategyCompilationError(
                "compiled_contract_hash_format_invalid", name
            )
        validated_hashes[name] = value
    if not isinstance(recorded_value, str) or not is_sha256_hash(recorded_value):
        raise StrategyCompilationError(
            "compiled_contract_hash_format_invalid", "compiled_contract_hash"
        )
    recorded = recorded_value
    if (
        sha256_prefixed(materialized_parameters)
        != validated_hashes["materialized_parameters_hash"]
    ):
        raise StrategyCompilationError("materialized_parameters_hash_mismatch")
    if sha256_prefixed(capability) != validated_hashes["capability_contract_hash"]:
        raise StrategyCompilationError("capability_contract_hash_mismatch")
    if sha256_prefixed(material) != recorded:
        raise StrategyCompilationError("compiled_contract_hash_mismatch")
    expectations = (
        ("strategy_name", strategy_name, expected_strategy_name),
        ("strategy_version", strategy_version, expected_strategy_version),
        (
            "strategy_registry_hash",
            validated_hashes["strategy_registry_hash"],
            expected_registry_hash,
        ),
        (
            "strategy_plugin_contract_hash",
            validated_hashes["strategy_plugin_contract_hash"],
            expected_plugin_hash,
        ),
    )
    for name, actual, expected in expectations:
        if expected is not None and actual != expected:
            raise StrategyCompilationError("compiled_contract_identity_mismatch", name)
    if expected_compiled_hash is not None and recorded != expected_compiled_hash:
        raise StrategyCompilationError(
            "compiled_contract_identity_mismatch", "compiled_contract_hash"
        )
    if original_contract is not None:
        return original_contract
    return CompiledStrategyContract(
        schema_version=schema_version,
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        raw_parameters=raw_parameters,
        materialized_parameters=materialized_parameters,
        parameter_source_map=parameter_source_map,
        materialized_parameters_hash=validated_hashes["materialized_parameters_hash"],
        data_requirements=data_requirements,
        exit_policy=exit_policy,
        exit_mode=exit_mode,
        capability_contract=capability,
        capability_contract_hash=validated_hashes["capability_contract_hash"],
        strategy_plugin_contract_hash=validated_hashes["strategy_plugin_contract_hash"],
        strategy_registry_hash=validated_hashes["strategy_registry_hash"],
        compiled_contract_hash=recorded,
    )


def compiled_contract_from_payload(
    payload: dict[str, Any],
    *,
    expected_strategy_name: str | None = None,
    expected_strategy_version: str | None = None,
    expected_registry_hash: str | None = None,
    expected_plugin_hash: str | None = None,
    expected_compiled_hash: str | None = None,
) -> CompiledStrategyContract:
    """Compatibility name for the shared compiled-contract validator."""
    return validate_compiled_strategy_contract(
        payload,
        expected_strategy_name=expected_strategy_name,
        expected_strategy_version=expected_strategy_version,
        expected_registry_hash=expected_registry_hash,
        expected_plugin_hash=expected_plugin_hash,
        expected_compiled_hash=expected_compiled_hash,
    )
