"""Single authority for compiling raw strategy inputs into executable evidence."""
from __future__ import annotations

from typing import Any

from .backtest_types import BacktestRunContext
from .hashing import sha256_prefixed
from .strategy_contract import (CompiledStrategyContract, ENGINE_SUPPORTED_CAPABILITIES,
                                ResearchStrategyPlugin, is_sha256_hash,
                                normalize_exit_policy_materialization)
from .strategy_registry import StrategyRegistry
from .strategy_spec import materialize_strategy_parameters, strategy_parameter_source_map


class StrategyCompilationError(ValueError):
    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(reason_code + (f":{detail}" if detail else ""))


class StrategyCompiler:
    SCHEMA_VERSION = 2

    def __init__(self, registry: StrategyRegistry) -> None:
        self.registry = registry

    def compile(self, *, strategy_name: str, raw_parameters: dict[str, Any], fee_rate: float,
                slippage_bps: float, context: BacktestRunContext | None = None) -> CompiledStrategyContract:
        plugin = self.registry.resolve(strategy_name)
        required = plugin.required_capabilities
        supported = ENGINE_SUPPORTED_CAPABILITIES
        mismatches = [name for name in required.__dataclass_fields__
                      if name != "schema_version" and getattr(required, name) != getattr(supported, name)]
        if mismatches:
            raise StrategyCompilationError("unsupported_strategy_capability", ",".join(mismatches))
        raw = dict(raw_parameters)
        baseline = materialize_strategy_parameters(
            plugin.name, raw, fee_rate=fee_rate, slippage_bps=slippage_bps
        )
        if plugin.parameter_materializer is not None:
            values = plugin.parameter_materializer(plugin=plugin, parameter_values=raw, fee_rate=fee_rate,
                                                   slippage_bps=slippage_bps, context=context)
        else:
            values = materialize_strategy_parameters(plugin.name, raw, fee_rate=fee_rate, slippage_bps=slippage_bps)
        values = dict(values)
        sources = strategy_parameter_source_map(plugin.name, raw, fee_rate=fee_rate, slippage_bps=slippage_bps)
        # Record the authority for the final value. A materializer which replaces
        # a spec/default value supersedes the source which merely introduced it.
        if plugin.parameter_materializer is not None:
            for key, value in values.items():
                if key not in baseline or baseline[key] != value:
                    sources[key] = "plugin_parameter_materializer"
        missing = sorted(set(values) - set(sources))
        if missing:
            raise StrategyCompilationError("unrecorded_behavior_default", ",".join(missing))
        policy = None
        if plugin.exit_policy_materializer is not None:
            policy = normalize_exit_policy_materialization(
                plugin.exit_policy_materializer(plugin.name, values), strategy_name=plugin.name,
                materializer=plugin.exit_policy_materializer, default_source="strategy_plugin",
                default_mode=str(getattr(context, "policy_materialization_mode", "research_only")),
            ).exit_policy
        requirements = plugin.data_requirements(values).capability_contract_payload()
        capability = required.as_dict()
        base = {
            "schema_version": self.SCHEMA_VERSION, "strategy_name": plugin.name,
            "strategy_version": plugin.version, "raw_parameters": raw,
            "materialized_parameters": values, "parameter_source_map": sources,
            "materialized_parameters_hash": sha256_prefixed(values), "data_requirements": requirements,
            "exit_policy": policy, "exit_mode": plugin.exit_mode, "capability_contract": capability,
            "capability_contract_hash": required.contract_hash(),
            "strategy_plugin_contract_hash": plugin.contract_hash(),
            "strategy_registry_hash": self.registry.content_hash,
        }
        compiled_hash = sha256_prefixed(base)
        return CompiledStrategyContract(
            schema_version=self.SCHEMA_VERSION, strategy_name=plugin.name, strategy_version=plugin.version,
            raw_parameters=raw, materialized_parameters=values,
            parameter_source_map=sources, materialized_parameters_hash=sha256_prefixed(values),
            data_requirements=requirements,
            exit_policy=dict(policy) if policy is not None else None,
            exit_mode=plugin.exit_mode, capability_contract=capability,
            capability_contract_hash=required.contract_hash(), strategy_plugin_contract_hash=plugin.contract_hash(),
            strategy_registry_hash=self.registry.content_hash, compiled_contract_hash=compiled_hash,
        )


_COMPILED_REQUIRED_FIELDS = frozenset(CompiledStrategyContract.__dataclass_fields__)


def compiled_contract_from_payload(
    payload: dict[str, Any], *, expected_strategy_name: str | None = None,
    expected_strategy_version: str | None = None, expected_registry_hash: str | None = None,
    expected_plugin_hash: str | None = None, expected_compiled_hash: str | None = None,
) -> CompiledStrategyContract:
    """Hydrate one complete v2 compiled contract, rejecting partial self-hashes."""
    if not isinstance(payload, dict):
        raise StrategyCompilationError("compiled_contract_payload_invalid")
    missing = sorted(_COMPILED_REQUIRED_FIELDS - set(payload))
    unknown = sorted(set(payload) - _COMPILED_REQUIRED_FIELDS)
    if missing:
        raise StrategyCompilationError("compiled_contract_required_field_missing", ",".join(missing))
    if unknown:
        raise StrategyCompilationError("compiled_contract_unknown_field", ",".join(unknown))
    material = dict(payload)
    recorded = material.pop("compiled_contract_hash")
    if int(material.get("schema_version", -1)) != StrategyCompiler.SCHEMA_VERSION:
        raise StrategyCompilationError("compiled_contract_schema_version_unsupported")
    mapping_fields = (
        "raw_parameters", "materialized_parameters", "parameter_source_map",
        "data_requirements", "capability_contract",
    )
    if any(not isinstance(material.get(name), dict) for name in mapping_fields):
        raise StrategyCompilationError("compiled_contract_nested_payload_invalid")
    if material.get("exit_policy") is not None and not isinstance(material["exit_policy"], dict):
        raise StrategyCompilationError("compiled_contract_nested_payload_invalid", "exit_policy")
    if not isinstance(material.get("strategy_name"), str) or not material["strategy_name"]:
        raise StrategyCompilationError("compiled_contract_nested_payload_invalid", "strategy_name")
    if not isinstance(material.get("strategy_version"), str) or not material["strategy_version"]:
        raise StrategyCompilationError("compiled_contract_nested_payload_invalid", "strategy_version")
    if material.get("exit_mode") not in {"strategy_owned", "common_typed_policy"}:
        raise StrategyCompilationError("compiled_contract_nested_payload_invalid", "exit_mode")
    parameter_keys = set(material["materialized_parameters"])
    if set(material["parameter_source_map"]) != parameter_keys or any(
            not isinstance(value, str) or not value for value in material["parameter_source_map"].values()):
        raise StrategyCompilationError("compiled_contract_parameter_source_map_invalid")
    requirements = material["data_requirements"]
    if (requirements.get("schema_version") != 1
            or not isinstance(requirements.get("required_data"), list)
            or not isinstance(requirements.get("optional_data"), list)
            or not isinstance(requirements.get("capabilities"), list)
            or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                   for item in requirements.get("capabilities", []))):
        raise StrategyCompilationError("compiled_contract_data_requirements_invalid")
    capability = material["capability_contract"]
    capability_fields = set(ENGINE_SUPPORTED_CAPABILITIES.as_dict())
    if set(capability) != capability_fields or capability.get("schema_version") != 1:
        raise StrategyCompilationError("compiled_contract_capability_payload_invalid")
    hash_fields = (
        "materialized_parameters_hash", "capability_contract_hash",
        "strategy_plugin_contract_hash", "strategy_registry_hash",
    )
    for name in hash_fields:
        if not is_sha256_hash(material.get(name)):
            raise StrategyCompilationError("compiled_contract_hash_format_invalid", name)
    if not is_sha256_hash(recorded):
        raise StrategyCompilationError("compiled_contract_hash_format_invalid", "compiled_contract_hash")
    if sha256_prefixed(material["materialized_parameters"]) != material["materialized_parameters_hash"]:
        raise StrategyCompilationError("materialized_parameters_hash_mismatch")
    if sha256_prefixed(material["capability_contract"]) != material["capability_contract_hash"]:
        raise StrategyCompilationError("capability_contract_hash_mismatch")
    if sha256_prefixed(material) != recorded:
        raise StrategyCompilationError("compiled_contract_hash_mismatch")
    expectations = (
        ("strategy_name", expected_strategy_name),
        ("strategy_version", expected_strategy_version),
        ("strategy_registry_hash", expected_registry_hash),
        ("strategy_plugin_contract_hash", expected_plugin_hash),
    )
    for name, expected in expectations:
        if expected is not None and material[name] != expected:
            raise StrategyCompilationError("compiled_contract_identity_mismatch", name)
    if expected_compiled_hash is not None and recorded != expected_compiled_hash:
        raise StrategyCompilationError("compiled_contract_identity_mismatch", "compiled_contract_hash")
    return CompiledStrategyContract(
        schema_version=int(material["schema_version"]), strategy_name=str(material["strategy_name"]),
        strategy_version=str(material["strategy_version"]),
        raw_parameters=dict(material["raw_parameters"]),
        materialized_parameters=dict(material["materialized_parameters"]),
        parameter_source_map=dict(material["parameter_source_map"]),
        materialized_parameters_hash=str(material["materialized_parameters_hash"]),
        data_requirements=dict(material["data_requirements"]),
        exit_policy=(dict(material["exit_policy"]) if material.get("exit_policy") is not None else None),
        exit_mode=str(material["exit_mode"]), capability_contract=dict(material["capability_contract"]),
        capability_contract_hash=str(material["capability_contract_hash"]),
        strategy_plugin_contract_hash=str(material["strategy_plugin_contract_hash"]),
        strategy_registry_hash=str(material["strategy_registry_hash"]), compiled_contract_hash=recorded)
