"""Versioned, fail-closed package metadata for research strategies.

The executable plugin contract deliberately stays small.  This sidecar owns
the human, governance, permission, resource, hypothesis, and compatibility
metadata that must be checked before a plugin enters a production registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from importlib import resources
import json
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping

from .hashing import sha256_prefixed
from .immutable_contract import canonical_mutable, deep_freeze


STRATEGY_MANIFEST_SCHEMA_VERSION = 1
SUPPORTED_STRATEGY_CONTRACT_VERSIONS = (6,)
STRATEGY_LIFECYCLE_STATES = frozenset(
    {
        "DRAFT",
        "VALIDATING",
        "APPROVED",
        "ACTIVE",
        "SUSPENDED",
        "RETIRED",
        "ARCHIVED",
        "REJECTED",
        "QUARANTINED",
    }
)

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{1,127}$")
_ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_HYPOTHESIS_FIELDS = frozenset(
    {
        "observed_phenomenon",
        "economic_rationale",
        "expected_mechanism",
        "applicable_conditions",
        "failure_conditions",
        "entry_conditions",
        "exit_conditions",
        "invalidation_conditions",
        "time_limit",
        "data_leakage_risks",
        "known_limitations",
        "retirement_criteria",
    }
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "strategy_id",
        "display_name",
        "strategy_version",
        "contract_version",
        "status",
        "owner",
        "supported_assets",
        "supported_markets",
        "required_data",
        "entrypoint",
        "parameter_schema_source",
        "output_schema",
        "resource_limits",
        "permissions",
        "supported_platform_contract_versions",
        "aliases",
        "hypothesis",
    }
)


class StrategyManifestError(ValueError):
    """A strategy package cannot be admitted to the catalog."""


@dataclass(frozen=True, slots=True)
class StrategyManifest:
    schema_version: int
    strategy_id: str
    display_name: str
    strategy_version: str
    contract_version: str
    status: str
    owner: Mapping[str, object]
    supported_assets: tuple[str, ...]
    supported_markets: tuple[str, ...]
    required_data: tuple[Mapping[str, object], ...]
    entrypoint: str
    parameter_schema_source: str
    output_schema: Mapping[str, object]
    resource_limits: Mapping[str, object]
    permissions: Mapping[str, object]
    supported_platform_contract_versions: tuple[int, ...]
    aliases: tuple[str, ...]
    hypothesis: Mapping[str, object]

    def __post_init__(self) -> None:
        for name in (
            "owner",
            "output_schema",
            "resource_limits",
            "permissions",
            "hypothesis",
        ):
            object.__setattr__(self, name, deep_freeze(getattr(self, name)))
        object.__setattr__(
            self,
            "required_data",
            tuple(deep_freeze(item) for item in self.required_data),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "display_name": self.display_name,
            "strategy_version": self.strategy_version,
            "contract_version": self.contract_version,
            "status": self.status,
            "owner": canonical_mutable(self.owner),
            "supported_assets": list(self.supported_assets),
            "supported_markets": list(self.supported_markets),
            "required_data": [canonical_mutable(item) for item in self.required_data],
            "entrypoint": self.entrypoint,
            "parameter_schema_source": self.parameter_schema_source,
            "output_schema": canonical_mutable(self.output_schema),
            "resource_limits": canonical_mutable(self.resource_limits),
            "permissions": canonical_mutable(self.permissions),
            "supported_platform_contract_versions": list(
                self.supported_platform_contract_versions
            ),
            "aliases": list(self.aliases),
            "hypothesis": canonical_mutable(self.hypothesis),
        }

    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def validate_plugin(self, plugin: object) -> None:
        name = str(getattr(plugin, "name", ""))
        version = str(getattr(plugin, "version", ""))
        spec = getattr(plugin, "spec", None)
        if name != self.strategy_id or version != self.strategy_version:
            raise StrategyManifestError(
                f"strategy_manifest_plugin_identity_mismatch:{self.strategy_id}"
            )
        if spec is None or getattr(spec, "strategy_name", None) != self.strategy_id:
            raise StrategyManifestError(
                f"strategy_manifest_spec_identity_mismatch:{self.strategy_id}"
            )
        if getattr(spec, "strategy_version", None) != self.strategy_version:
            raise StrategyManifestError(
                f"strategy_manifest_spec_version_mismatch:{self.strategy_id}"
            )
        declared_required = tuple(
            sorted(str(item["name"]) for item in self.required_data if item["required"])
        )
        if declared_required != tuple(sorted(getattr(plugin, "required_data", ()))):
            raise StrategyManifestError(
                f"strategy_manifest_required_data_mismatch:{self.strategy_id}"
            )
        if self.parameter_schema_source == "strategy_spec" and len(
            getattr(spec, "parameter_schema", ())
        ) != len(getattr(spec, "accepted_parameter_names", ())):
            raise StrategyManifestError(
                f"strategy_manifest_parameter_schema_incomplete:{self.strategy_id}"
            )
        module, factory = self.entrypoint.split(":", 1)
        if module != getattr(
            plugin, "reconstruction_module", None
        ) or factory != getattr(plugin, "reconstruction_qualname", None):
            raise StrategyManifestError(
                f"strategy_manifest_entrypoint_mismatch:{self.strategy_id}"
            )
        bound_hash = getattr(plugin, "package_manifest_hash", None)
        if bound_hash != self.content_hash():
            raise StrategyManifestError(
                f"strategy_manifest_hash_binding_mismatch:{self.strategy_id}"
            )

    @property
    def selectable(self) -> bool:
        return self.status == "ACTIVE"


def parse_strategy_manifest(value: object) -> StrategyManifest:
    if not isinstance(value, dict) or set(value) != _TOP_LEVEL_FIELDS:
        raise StrategyManifestError("strategy_manifest_fields_invalid")
    if value.get("schema_version") != STRATEGY_MANIFEST_SCHEMA_VERSION:
        raise StrategyManifestError("strategy_manifest_schema_version_unsupported")
    strategy_id = _required_text(value, "strategy_id")
    if _IDENTIFIER.fullmatch(strategy_id) is None:
        raise StrategyManifestError("strategy_manifest_strategy_id_invalid")
    entrypoint = _required_text(value, "entrypoint")
    if _ENTRYPOINT.fullmatch(entrypoint) is None:
        raise StrategyManifestError("strategy_manifest_entrypoint_invalid")
    status = _required_text(value, "status").upper()
    if status not in STRATEGY_LIFECYCLE_STATES:
        raise StrategyManifestError("strategy_manifest_status_invalid")
    owner = _mapping(value, "owner")
    if set(owner) != {"team", "responsibility"} or not all(
        isinstance(owner.get(key), str) and str(owner[key]).strip() for key in owner
    ):
        raise StrategyManifestError("strategy_manifest_owner_invalid")
    required_data = _required_data(value.get("required_data"))
    output_schema = _mapping(value, "output_schema")
    if set(output_schema) != {"decision_stream", "common_result"} or not all(
        isinstance(item, str) and item.strip() for item in output_schema.values()
    ):
        raise StrategyManifestError("strategy_manifest_output_schema_invalid")
    limits = _mapping(value, "resource_limits")
    required_limit_fields = {
        "max_runtime_seconds",
        "max_memory_mb",
        "max_cpu_cores",
        "max_output_bytes",
        "max_parallel_runs",
    }
    if set(limits) != required_limit_fields or any(
        not isinstance(limits[field], (int, float))
        or isinstance(limits[field], bool)
        or float(limits[field]) <= 0
        for field in required_limit_fields
    ):
        raise StrategyManifestError("strategy_manifest_resource_limits_invalid")
    permissions = _mapping(value, "permissions")
    if set(permissions) != {
        "network",
        "database_write",
        "filesystem_reads",
        "filesystem_writes",
    }:
        raise StrategyManifestError("strategy_manifest_permissions_invalid")
    if (
        permissions.get("network") != "denied"
        or permissions.get("database_write") is not False
    ):
        raise StrategyManifestError("strategy_manifest_permission_escalation")
    _text_sequence(permissions.get("filesystem_reads"), "filesystem_reads")
    _text_sequence(permissions.get("filesystem_writes"), "filesystem_writes")
    contracts = value.get("supported_platform_contract_versions")
    if (
        not isinstance(contracts, list)
        or not contracts
        or any(not isinstance(item, int) for item in contracts)
        or not set(contracts).intersection(SUPPORTED_STRATEGY_CONTRACT_VERSIONS)
    ):
        raise StrategyManifestError("strategy_manifest_contract_incompatible")
    hypothesis = _mapping(value, "hypothesis")
    if set(hypothesis) != _REQUIRED_HYPOTHESIS_FIELDS or any(
        not _nonempty_hypothesis_value(hypothesis[field])
        for field in _REQUIRED_HYPOTHESIS_FIELDS
    ):
        raise StrategyManifestError("strategy_manifest_hypothesis_incomplete")
    parameter_schema_source = _required_text(value, "parameter_schema_source")
    if parameter_schema_source != "strategy_spec":
        raise StrategyManifestError("strategy_manifest_parameter_schema_source_invalid")
    contract_version = _required_text(value, "contract_version")
    if contract_version != "research-strategy-plugin.v6":
        raise StrategyManifestError("strategy_manifest_contract_version_invalid")
    return StrategyManifest(
        schema_version=STRATEGY_MANIFEST_SCHEMA_VERSION,
        strategy_id=strategy_id,
        display_name=_required_text(value, "display_name"),
        strategy_version=_required_text(value, "strategy_version"),
        contract_version=contract_version,
        status=status,
        owner=MappingProxyType(dict(owner)),
        supported_assets=_text_sequence(
            value.get("supported_assets"), "supported_assets"
        ),
        supported_markets=_text_sequence(
            value.get("supported_markets"), "supported_markets"
        ),
        required_data=required_data,
        entrypoint=entrypoint,
        parameter_schema_source=parameter_schema_source,
        output_schema=MappingProxyType(dict(output_schema)),
        resource_limits=MappingProxyType(dict(limits)),
        permissions=MappingProxyType(dict(permissions)),
        supported_platform_contract_versions=tuple(contracts),
        aliases=_text_sequence(value.get("aliases"), "aliases", allow_empty=True),
        hypothesis=MappingProxyType(dict(hypothesis)),
    )


def load_builtin_strategy_manifest(module_name: str) -> StrategyManifest:
    package, _, leaf = module_name.rpartition(".")
    if package != "market_research.builtin_strategies" or not leaf:
        raise StrategyManifestError("builtin_strategy_manifest_module_invalid")
    strategy_module = import_module(module_name)
    module_file = getattr(strategy_module, "__file__", None)
    resource = (
        Path(module_file).resolve().with_name(f"{leaf}.strategy.json")
        if isinstance(module_file, str) and module_file
        else resources.files(package).joinpath(f"{leaf}.strategy.json")
    )
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (
        FileNotFoundError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise StrategyManifestError(
            f"builtin_strategy_manifest_unavailable:{leaf}"
        ) from exc
    return parse_strategy_manifest(payload)


def builtin_strategy_manifest_hash(module_name: str) -> str:
    return load_builtin_strategy_manifest(module_name).content_hash()


def _required_text(value: Mapping[str, object], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise StrategyManifestError(f"strategy_manifest_{field}_invalid")
    return raw.strip()


def _mapping(value: Mapping[str, object], field: str) -> Mapping[str, Any]:
    raw = value.get(field)
    if not isinstance(raw, dict):
        raise StrategyManifestError(f"strategy_manifest_{field}_invalid")
    return raw


def _text_sequence(
    value: object, field: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or any(not isinstance(item, str) or not item.strip() for item in value)
        or len(set(value)) != len(value)
    ):
        raise StrategyManifestError(f"strategy_manifest_{field}_invalid")
    return tuple(str(item).strip() for item in value)


def _required_data(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or not value:
        raise StrategyManifestError("strategy_manifest_required_data_invalid")
    result: list[Mapping[str, object]] = []
    names: set[str] = set()
    fields = {"name", "required", "fields", "timeframe", "timezone", "min_rows"}
    for item in value:
        if not isinstance(item, dict) or set(item) != fields:
            raise StrategyManifestError("strategy_manifest_required_data_invalid")
        name = _required_text(item, "name")
        if name in names or not isinstance(item.get("required"), bool):
            raise StrategyManifestError("strategy_manifest_required_data_invalid")
        names.add(name)
        _text_sequence(item.get("fields"), "required_data_fields")
        if not isinstance(item.get("min_rows"), int) or int(item["min_rows"]) < 0:
            raise StrategyManifestError("strategy_manifest_required_data_invalid")
        if not all(
            isinstance(item.get(field), str) and str(item[field]).strip()
            for field in ("timeframe", "timezone")
        ):
            raise StrategyManifestError("strategy_manifest_required_data_invalid")
        result.append(MappingProxyType(dict(item)))
    return tuple(result)


def _nonempty_hypothesis_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value) and all(
            isinstance(item, str) and item.strip() for item in value
        )
    return False


__all__ = [
    "STRATEGY_LIFECYCLE_STATES",
    "STRATEGY_MANIFEST_SCHEMA_VERSION",
    "SUPPORTED_STRATEGY_CONTRACT_VERSIONS",
    "StrategyManifest",
    "StrategyManifestError",
    "builtin_strategy_manifest_hash",
    "load_builtin_strategy_manifest",
    "parse_strategy_manifest",
]
