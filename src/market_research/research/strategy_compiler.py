"""Single authority for compiling raw strategy inputs into executable evidence."""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

from .backtest_types import BacktestRunContext
from .hashing import sha256_prefixed
from .strategy_contract import (CompiledStrategyContract, ENGINE_SUPPORTED_CAPABILITIES,
                                ResearchStrategyPlugin, normalize_exit_policy_materialization)
from .strategy_registry import StrategyRegistry
from .strategy_spec import materialize_strategy_parameters, strategy_parameter_source_map


class StrategyCompilationError(ValueError):
    def __init__(self, reason_code: str, detail: str = "") -> None:
        self.reason_code = reason_code
        super().__init__(reason_code + (f":{detail}" if detail else ""))


class StrategyCompiler:
    SCHEMA_VERSION = 1

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
        if plugin.parameter_materializer is not None:
            values = plugin.parameter_materializer(plugin=plugin, parameter_values=raw, fee_rate=fee_rate,
                                                   slippage_bps=slippage_bps, context=context)
        else:
            values = materialize_strategy_parameters(plugin.name, raw, fee_rate=fee_rate, slippage_bps=slippage_bps)
        values = dict(values)
        sources = strategy_parameter_source_map(plugin.name, raw, fee_rate=fee_rate, slippage_bps=slippage_bps)
        # A plugin override is authoritative, but its provenance must remain explicit.
        for key in values:
            sources.setdefault(key, "plugin_parameter_materializer")
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
        requirements = plugin.data_requirements().capability_contract_payload()
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
            raw_parameters=MappingProxyType(raw), materialized_parameters=MappingProxyType(values),
            parameter_source_map=MappingProxyType(sources), materialized_parameters_hash=sha256_prefixed(values),
            data_requirements=MappingProxyType(requirements),
            exit_policy=MappingProxyType(dict(policy)) if policy is not None else None,
            exit_mode=plugin.exit_mode, capability_contract=MappingProxyType(capability),
            capability_contract_hash=required.contract_hash(), strategy_plugin_contract_hash=plugin.contract_hash(),
            strategy_registry_hash=self.registry.content_hash, compiled_contract_hash=compiled_hash,
        )


def compile_builtin_strategy(*, strategy_name: str, raw_parameters: dict[str, Any], fee_rate: float,
                             slippage_bps: float, context: BacktestRunContext | None = None) -> CompiledStrategyContract:
    from .builtin_registry import builtin_strategy_registry
    return StrategyCompiler(builtin_strategy_registry()).compile(
        strategy_name=strategy_name, raw_parameters=raw_parameters, fee_rate=fee_rate,
        slippage_bps=slippage_bps, context=context)


def compiled_contract_from_payload(payload: dict[str, Any]) -> CompiledStrategyContract:
    material = dict(payload)
    recorded = str(material.pop("compiled_contract_hash"))
    if sha256_prefixed(material) != recorded:
        raise StrategyCompilationError("compiled_contract_hash_mismatch")
    return CompiledStrategyContract(
        schema_version=int(material["schema_version"]), strategy_name=str(material["strategy_name"]),
        strategy_version=str(material["strategy_version"]),
        raw_parameters=MappingProxyType(dict(material["raw_parameters"])),
        materialized_parameters=MappingProxyType(dict(material["materialized_parameters"])),
        parameter_source_map=MappingProxyType(dict(material["parameter_source_map"])),
        materialized_parameters_hash=str(material["materialized_parameters_hash"]),
        data_requirements=MappingProxyType(dict(material["data_requirements"])),
        exit_policy=(MappingProxyType(dict(material["exit_policy"])) if material.get("exit_policy") is not None else None),
        exit_mode=str(material["exit_mode"]), capability_contract=MappingProxyType(dict(material["capability_contract"])),
        capability_contract_hash=str(material["capability_contract_hash"]),
        strategy_plugin_contract_hash=str(material["strategy_plugin_contract_hash"]),
        strategy_registry_hash=str(material["strategy_registry_hash"]), compiled_contract_hash=recorded)
