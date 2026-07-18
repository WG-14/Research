"""Research-only strategy declarations.

This module deliberately has no operational strategy, replay, or order-entry
dependency.  It is the contract consumed by ``market-research``.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
import re
from types import CodeType
from typing import Any, Callable, Iterable, Mapping, Protocol

from .decision_event import ResearchDecisionEvent
from .hashing import sha256_prefixed
from .immutable_contract import canonical_mutable, deep_freeze
from .strategy_spec import StrategySpec


ResearchEventBuilder = Callable[..., Iterable[ResearchDecisionEvent]]
DiagnosticCountBuilder = Callable[[dict[str, object]], dict[str, Any]]
ResearchDataRequirementBuilder = Callable[
    [object | None], "ResearchStrategyDataRequirements"
]
ResearchDecisionBuilder = Callable[..., Any]
ResearchPayloadAdapter = Callable[[dict[str, object], Any], dict[str, object]]
ExitPolicyMaterializer = Callable[[str, dict[str, Any]], Any]


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class MaterializedParameterSet:
    """Immutable compiler-owned parameter values exposed to an extension."""

    values: Mapping[str, object]
    sources: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", deep_freeze(self.values))
        object.__setattr__(self, "sources", deep_freeze(self.sources))


@dataclass(frozen=True, slots=True)
class ParameterExtensionContext:
    """Non-materialization metadata available to a parameter extension."""

    strategy_name: str
    strategy_version: str
    policy_materialization_mode: str


@dataclass(frozen=True, slots=True)
class ParameterExtensionResult:
    """Final parameter values and explicit provenance for every changed key."""

    values: Mapping[str, object]
    source_overrides: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", deep_freeze(self.values))
        object.__setattr__(self, "source_overrides", deep_freeze(self.source_overrides))


class ResearchParameterExtension(Protocol):
    def __call__(
        self,
        *,
        materialized: MaterializedParameterSet,
        context: ParameterExtensionContext,
    ) -> ParameterExtensionResult: ...


def is_sha256_hash(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _source_artifact(value: Any) -> str:
    try:
        return inspect.getsource(value)
    except (OSError, TypeError):
        code = getattr(value, "__code__", None)
        if isinstance(code, CodeType):
            return json.dumps(
                {
                    "code": _stable_code_payload(code),
                    "defaults": _stable_code_value(
                        getattr(value, "__defaults__", None)
                    ),
                    "kwdefaults": _stable_code_value(
                        getattr(value, "__kwdefaults__", None)
                    ),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        return repr(type(value))


def _stable_code_payload(code: CodeType) -> dict[str, object]:
    """Return behavior bytecode without CPython's mutable quickening state."""
    return {
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "nlocals": code.co_nlocals,
        "stacksize": code.co_stacksize,
        "flags": code.co_flags,
        "bytecode": code.co_code.hex(),
        "exceptiontable": code.co_exceptiontable.hex(),
        "constants": [_stable_code_value(item) for item in code.co_consts],
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
    }


def _stable_code_value(value: Any) -> object:
    if isinstance(value, CodeType):
        return {"code_object": _stable_code_payload(value)}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, tuple):
        return {"tuple": [_stable_code_value(item) for item in value]}
    if isinstance(value, frozenset):
        items = [_stable_code_value(item) for item in value]
        return {"frozenset": sorted(items, key=repr)}
    if isinstance(value, list):
        return {"list": [_stable_code_value(item) for item in value]}
    if isinstance(value, dict):
        items = [
            (_stable_code_value(key), _stable_code_value(item))
            for key, item in value.items()
        ]
        return {"dict": sorted(items, key=repr)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if value is Ellipsis:
        return {"ellipsis": True}
    if isinstance(value, complex):
        return {"complex": [value.real, value.imag]}
    return {
        "type": f"{type(value).__module__}:{type(value).__qualname__}",
        "repr": repr(value),
    }


def _transitive_behavior_binding(hook: Callable[..., Any]) -> dict[str, str]:
    """Bind the deterministic, cycle-safe graph of strategy-owned behavior."""
    components: dict[str, str] = {}
    visited_values: set[int] = set()
    root_module = str(getattr(hook, "__module__", ""))
    root_namespace = root_module.split(".", 1)[0]

    def is_strategy_owned_module(module: str) -> bool:
        if module.startswith("market_research"):
            return True
        if not root_module:
            return False
        return module == root_module or (
            bool(root_namespace) and module.startswith(f"{root_namespace}.")
        )

    def visit(value: Any, *, identity_hint: str | None = None) -> None:
        if not (inspect.isfunction(value) or inspect.isclass(value)):
            return
        module = str(getattr(value, "__module__", ""))
        if not is_strategy_owned_module(module):
            return
        identity = identity_hint or (
            f"{module}:{getattr(value, '__qualname__', type(value).__qualname__)}"
        )
        value_identity = id(value)
        if value_identity in visited_values:
            return
        visited_values.add(value_identity)
        components[identity] = sha256_prefixed(_source_artifact(value))
        if inspect.isclass(value):
            for member_name, member in sorted(vars(value).items()):
                if inspect.isfunction(member) or inspect.isclass(member):
                    # dataclasses creates methods with a shared synthetic
                    # ``__create_fn__.<locals>`` qualname.  Bind class members
                    # through their owning class so two generated methods can
                    # neither alias nor make the hash depend on import order.
                    visit(member, identity_hint=f"{identity}.{member_name}")
            return
        code = getattr(value, "__code__", None)
        globals_map = getattr(value, "__globals__", {})
        for name in sorted(set(getattr(code, "co_names", ()))):
            referenced = globals_map.get(name)
            referenced_qualname = str(getattr(referenced, "__qualname__", ""))
            visit(
                referenced,
                identity_hint=(
                    f"{identity}.<global:{name}>"
                    if "<locals>" in referenced_qualname
                    else None
                ),
            )
        for cell_index, cell in enumerate(getattr(value, "__closure__", ()) or ()):
            try:
                visit(
                    cell.cell_contents,
                    identity_hint=f"{identity}.<closure:{cell_index}>",
                )
            except ValueError:
                continue

    visit(hook)
    return {identity: components[identity] for identity in sorted(components)}


@dataclass(frozen=True, slots=True)
class StrategyCapabilityContract:
    """Versioned, fail-closed declaration of behavior required by a strategy."""

    schema_version: int = 1
    instrument_count: int = 1
    direction: str = "long_only"
    max_concurrent_positions: int = 1
    pyramiding: bool = False
    partial_exit: bool = False
    max_intents_per_decision: int = 1
    portfolio_mode: str = "single_asset_cash_qty"

    def as_dict(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())


ENGINE_SUPPORTED_CAPABILITIES = StrategyCapabilityContract()


@dataclass(frozen=True, slots=True)
class EngineCapabilitySupport:
    """Independent limits supported by the common execution/accounting path."""

    max_instrument_count: int = 1
    directions: tuple[str, ...] = ("long_only",)
    max_concurrent_positions: int = 1
    pyramiding: bool = False
    partial_exit: bool = True
    max_intents_per_decision: int = 1
    portfolio_modes: tuple[str, ...] = ("single_asset_cash_qty",)

    def unsupported_fields(
        self, required: StrategyCapabilityContract
    ) -> tuple[str, ...]:
        unsupported: list[str] = []
        if required.instrument_count > self.max_instrument_count:
            unsupported.append("instrument_count")
        if required.direction not in self.directions:
            unsupported.append("direction")
        if required.max_concurrent_positions > self.max_concurrent_positions:
            unsupported.append("max_concurrent_positions")
        if required.pyramiding and not self.pyramiding:
            unsupported.append("pyramiding")
        if required.partial_exit and not self.partial_exit:
            unsupported.append("partial_exit")
        if required.max_intents_per_decision > self.max_intents_per_decision:
            unsupported.append("max_intents_per_decision")
        if required.portfolio_mode not in self.portfolio_modes:
            unsupported.append("portfolio_mode")
        return tuple(unsupported)


ENGINE_CAPABILITY_SUPPORT = EngineCapabilitySupport()


@dataclass(frozen=True, slots=True)
class CompiledStrategyContract:
    """The sole authoritative interpretation of a candidate/scenario strategy."""

    schema_version: int
    strategy_name: str
    strategy_version: str
    raw_parameters: Mapping[str, object]
    materialized_parameters: Mapping[str, object]
    parameter_source_map: Mapping[str, str]
    materialized_parameters_hash: str
    data_requirements: Mapping[str, object]
    exit_policy: Mapping[str, object] | None
    exit_mode: str
    capability_contract: Mapping[str, object]
    capability_contract_hash: str
    strategy_plugin_contract_hash: str
    strategy_registry_hash: str
    compiled_contract_hash: str

    def __post_init__(self) -> None:
        for field_name in (
            "raw_parameters",
            "materialized_parameters",
            "parameter_source_map",
            "data_requirements",
            "capability_contract",
        ):
            object.__setattr__(self, field_name, deep_freeze(getattr(self, field_name)))
        if self.exit_policy is not None:
            object.__setattr__(self, "exit_policy", deep_freeze(self.exit_policy))

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "raw_parameters": canonical_mutable(self.raw_parameters),
            "materialized_parameters": canonical_mutable(self.materialized_parameters),
            "parameter_source_map": canonical_mutable(self.parameter_source_map),
            "materialized_parameters_hash": self.materialized_parameters_hash,
            "data_requirements": canonical_mutable(self.data_requirements),
            "exit_policy": canonical_mutable(self.exit_policy)
            if self.exit_policy is not None
            else None,
            "exit_mode": self.exit_mode,
            "capability_contract": canonical_mutable(self.capability_contract),
            "capability_contract_hash": self.capability_contract_hash,
            "strategy_plugin_contract_hash": self.strategy_plugin_contract_hash,
            "strategy_registry_hash": self.strategy_registry_hash,
            "compiled_contract_hash": self.compiled_contract_hash,
        }


@dataclass(frozen=True, slots=True)
class ExitPolicyMaterialization:
    exit_policy: dict[str, Any]
    exit_policy_hash: str
    exit_policy_contract_hash: str
    exit_policy_config: dict[str, Any]
    exit_policy_config_hash: str
    exit_policy_source: str
    exit_policy_materialization_mode: str

    def as_dict(self) -> dict[str, object]:
        return {
            "exit_policy": dict(self.exit_policy),
            "exit_policy_hash": self.exit_policy_hash,
            "exit_policy_contract_hash": self.exit_policy_contract_hash,
            "exit_policy_config": dict(self.exit_policy_config),
            "exit_policy_config_hash": self.exit_policy_config_hash,
            "exit_policy_source": self.exit_policy_source,
            "exit_policy_materialization_mode": self.exit_policy_materialization_mode,
        }


def normalize_exit_policy_materialization(
    result: Any,
    *,
    strategy_name: str,
    materializer: Callable[..., Any] | None,
    default_source: str,
    default_mode: str,
) -> ExitPolicyMaterialization:
    if isinstance(result, ExitPolicyMaterialization):
        return result
    if not isinstance(result, dict) or not isinstance(result.get("exit_policy"), dict):
        raise ValueError(f"research_exit_policy_materializer_invalid:{strategy_name}")
    policy = dict(result["exit_policy"])
    config = dict(result.get("exit_policy_config") or policy)
    source = str(result.get("exit_policy_source") or default_source)
    contract_payload = {
        "schema_version": 1,
        "strategy_name": strategy_name,
        "materializer_module": materializer.__module__ if materializer else None,
        "materializer_qualname": materializer.__qualname__ if materializer else None,
        "exit_policy_source": source,
    }
    return ExitPolicyMaterialization(
        exit_policy=policy,
        exit_policy_hash=str(result.get("exit_policy_hash") or sha256_prefixed(policy)),
        exit_policy_contract_hash=str(
            result.get("exit_policy_contract_hash") or sha256_prefixed(contract_payload)
        ),
        exit_policy_config=config,
        exit_policy_config_hash=str(
            result.get("exit_policy_config_hash") or sha256_prefixed(config)
        ),
        exit_policy_source=source,
        exit_policy_materialization_mode=str(
            result.get("exit_policy_materialization_mode") or default_mode
        ),
    )


def generic_diagnostics_count_builder(payload: dict[str, object]) -> dict[str, Any]:
    raw_signal = str(payload.get("raw_signal") or "").upper()
    final_signal = str(payload.get("final_signal") or "").upper()
    entry_signal = str(payload.get("entry_signal") or "").upper()
    defaults = {
        "raw_signal_count": 0,
        "final_signal_count": 0,
        "entry_signal_count": 0,
        "exit_signal_count": 0,
    }
    counts: dict[str, int] = {}
    if raw_signal in {"BUY", "SELL"}:
        counts["raw_signal_count"] = 1
    if final_signal in {"BUY", "SELL"}:
        counts["final_signal_count"] = 1
    if entry_signal == "BUY":
        counts["entry_signal_count"] = 1
    if str(payload.get("exit_signal") or "").upper() == "SELL":
        counts["exit_signal_count"] = 1
    return {
        "strategy_diagnostics_namespace": payload.get("strategy_diagnostics_namespace"),
        "strategy_diagnostic_count_defaults": defaults,
        "strategy_diagnostic_counts": counts,
    }


@dataclass(frozen=True, slots=True)
class ResearchDataRequirement:
    name: str
    required: bool = True
    min_coverage_pct: float | None = None
    source: str | None = None
    notes: str | None = None
    lookback_rows: int | None = None
    min_rows: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "min_coverage_pct": self.min_coverage_pct,
            "source": self.source,
            "notes": self.notes,
            "lookback_rows": self.lookback_rows,
            "min_rows": self.min_rows,
        }


@dataclass(frozen=True, slots=True)
class ResearchStrategyDataRequirements:
    required_data: tuple[str, ...]
    optional_data: tuple[str, ...] = ()
    capabilities: tuple[ResearchDataRequirement, ...] = ()

    def normalized_capabilities(self) -> tuple[ResearchDataRequirement, ...]:
        values: dict[str, ResearchDataRequirement] = {}
        for name in self.required_data:
            normalized = str(name).strip().lower()
            if normalized:
                values[normalized] = ResearchDataRequirement(name=normalized)
        for name in self.optional_data:
            normalized = str(name).strip().lower()
            if normalized and normalized not in values:
                values[normalized] = ResearchDataRequirement(
                    name=normalized, required=False
                )
        for capability in self.capabilities:
            normalized = str(capability.name).strip().lower()
            if normalized:
                values[normalized] = ResearchDataRequirement(
                    name=normalized,
                    required=bool(capability.required),
                    min_coverage_pct=capability.min_coverage_pct,
                    source=capability.source,
                    notes=capability.notes,
                    lookback_rows=capability.lookback_rows,
                    min_rows=capability.min_rows,
                )
        return tuple(values[name] for name in sorted(values))

    def capability_contract_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "required_data": list(self.required_data),
            "optional_data": list(self.optional_data),
            "capabilities": [item.as_dict() for item in self.normalized_capabilities()],
        }


@dataclass(frozen=True, slots=True)
class ResearchStrategyPlugin:
    name: str
    version: str
    spec: StrategySpec
    required_data: tuple[str, ...]
    optional_data: tuple[str, ...]
    event_builder: ResearchEventBuilder
    decision_contract_version: str
    diagnostics_namespace: str
    parameter_materializer: ResearchParameterExtension | None = None
    diagnostics_builder: DiagnosticCountBuilder | None = None
    data_requirements_builder: ResearchDataRequirementBuilder | None = None
    decision_builder: ResearchDecisionBuilder | None = None
    payload_adapter: ResearchPayloadAdapter | None = None
    exit_policy_materializer: ExitPolicyMaterializer | None = None
    runtime_factory: Callable[..., Any] | None = None
    exit_decision_builder: Callable[..., Any] | None = None
    exit_mode: str = "strategy_owned"
    required_capabilities: StrategyCapabilityContract = ENGINE_SUPPORTED_CAPABILITIES
    execution_authority: str = "common_simulation_engine"
    reconstruction_module: str | None = None
    reconstruction_qualname: str | None = None
    package_manifest_hash: str | None = None

    def __post_init__(self) -> None:
        name = str(self.name or "").strip().lower()
        if not name:
            raise ValueError("research_strategy_name_missing")
        if not str(self.version or "").strip():
            raise ValueError(f"research_strategy_version_missing:{name}")
        if not str(self.decision_contract_version or "").strip():
            raise ValueError(
                f"research_strategy_decision_contract_version_missing:{name}"
            )
        if self.spec.rule_spec is None:
            raise ValueError(f"research_strategy_rule_spec_missing:{name}")
        if self.event_builder is None:
            raise ValueError(f"research_strategy_event_builder_missing:{name}")
        if self.execution_authority != "common_simulation_engine":
            raise ValueError(
                f"research_strategy_custom_execution_authority_rejected:{name}"
            )
        if self.package_manifest_hash is not None and not is_sha256_hash(
            self.package_manifest_hash
        ):
            raise ValueError(f"research_strategy_package_manifest_hash_invalid:{name}")
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self,
            "required_data",
            tuple(
                sorted(
                    {
                        str(x).strip().lower()
                        for x in self.required_data
                        if str(x).strip()
                    }
                )
            ),
        )
        object.__setattr__(
            self,
            "optional_data",
            tuple(
                sorted(
                    {
                        str(x).strip().lower()
                        for x in self.optional_data
                        if str(x).strip()
                    }
                )
            ),
        )
        if self.diagnostics_builder is None:
            object.__setattr__(
                self, "diagnostics_builder", generic_diagnostics_count_builder
            )
        if self.exit_mode not in {"strategy_owned", "common_typed_policy"}:
            raise ValueError(
                f"research_strategy_exit_mode_invalid:{name}:{self.exit_mode}"
            )
        if (
            self.exit_mode == "common_typed_policy"
            and self.exit_decision_builder is not None
        ):
            raise ValueError(
                f"research_strategy_multiple_exit_authorities:{name}:common_policy_with_strategy_builder"
            )

    def data_requirements(
        self, strategy_spec: object | None = None
    ) -> ResearchStrategyDataRequirements:
        if self.data_requirements_builder is not None:
            return self.data_requirements_builder(strategy_spec)
        return ResearchStrategyDataRequirements(self.required_data, self.optional_data)

    def contract_payload(self) -> dict[str, object]:
        hooks = {}
        for role, hook in (
            ("parameter_materializer", self.parameter_materializer),
            ("runtime_factory", self.runtime_factory),
            ("event_builder_compatibility", self.event_builder),
            ("exit_policy_materializer", self.exit_policy_materializer),
            ("exit_decision_builder", self.exit_decision_builder),
            ("data_requirements_builder", self.data_requirements_builder),
            ("diagnostics_builder", self.diagnostics_builder),
            ("payload_adapter", self.payload_adapter),
        ):
            if hook is not None:
                components = _transitive_behavior_binding(hook)
                hooks[role] = {
                    "module": _callable_module(hook),
                    "qualname": _callable_qualname(hook),
                    "role": role,
                    "hook_contract_version": self.decision_contract_version,
                    "source_artifact_hash": sha256_prefixed(components),
                    "transitive_behavior_components": components,
                }
        return {
            "schema_version": 6,
            "name": self.name,
            "version": self.version,
            "strategy_spec_hash": self.spec.spec_hash(),
            "required_data": list(self.required_data),
            "optional_data": list(self.optional_data),
            "research_data_requirements": self.data_requirements(
                self.spec.default_parameters
            ).capability_contract_payload(),
            "decision_contract_version": self.decision_contract_version,
            "diagnostics_namespace": self.diagnostics_namespace,
            "execution_authority": self.execution_authority,
            "exit_mode": self.exit_mode,
            "required_capabilities": self.required_capabilities.as_dict(),
            "behavior_hooks": hooks,
            "event_builder_module": self.event_builder.__module__,
            "event_builder_qualname": self.event_builder.__qualname__,
            "parameter_materializer_module": (
                _callable_module(self.parameter_materializer)
                if self.parameter_materializer is not None
                else None
            ),
            "source_artifact_binding": "recursive_strategy_owned_components_v4",
            "reconstruction_identity": {
                "module": self.reconstruction_module,
                "qualname": self.reconstruction_qualname,
            },
            "package_manifest_hash": self.package_manifest_hash,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.contract_payload())


def _callable_module(hook: object) -> str:
    return str(getattr(hook, "__module__", type(hook).__module__))


def _callable_qualname(hook: object) -> str:
    return str(getattr(hook, "__qualname__", type(hook).__qualname__))
