"""Executable authority, boundary, and migration contracts for multi-asset research.

The JSON manifests packaged beside this module are deliberately stricter than
an informal architecture diagram.  They enumerate every Python module in this
package, bind each shared semantic to one owner, and drive AST checks that fail
when a new implementation bypasses those owners.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, cast


ARCHITECTURE_SCHEMA_VERSION = 1
PACKAGE_PREFIX = "market_research.research.multi_asset"
REQUIRED_AUTHORITIES = frozenset(
    {
        "calendar_unit",
        "data",
        "evidence",
        "exposure",
        "identity",
        "ledger",
        "lifecycle",
        "market_state",
        "valuation",
    }
)
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RESPONSIBILITY_BEGIN = "<!-- BEGIN GENERATED MULTI-ASSET RESPONSIBILITY MAP -->"
_RESPONSIBILITY_END = "<!-- END GENERATED MULTI-ASSET RESPONSIBILITY MAP -->"


class ArchitectureManifestError(ValueError):
    """A packaged architecture contract is malformed or has drifted."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _content_hash(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _reject_constant(value: str) -> object:
    raise ArchitectureManifestError(f"non_finite_json_constant:{value}")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ArchitectureManifestError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ArchitectureManifestError(f"{label}_object_required")
    return cast(Mapping[str, object], value)


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise ArchitectureManifestError(f"{label}_array_required")
    return cast(Sequence[object], value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArchitectureManifestError(f"{label}_text_required")
    return value


def _identifier(value: object, label: str) -> str:
    result = _text(value, label)
    if not _ID.fullmatch(result):
        raise ArchitectureManifestError(f"{label}_invalid_identifier")
    return result


def _strings(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    values = tuple(_text(item, f"{label}.item") for item in _sequence(value, label))
    if not allow_empty and not values:
        raise ArchitectureManifestError(f"{label}_empty")
    if values != tuple(sorted(set(values))):
        raise ArchitectureManifestError(f"{label}_not_sorted_unique")
    return values


def _exact(
    value: Mapping[str, object],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = ",".join(sorted(expected - actual))
        unknown = ",".join(sorted(actual - expected))
        raise ArchitectureManifestError(
            f"{label}_fields_invalid:missing={missing}:unknown={unknown}"
        )


def _load_payload(path: Path, *, kind: str) -> Mapping[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ArchitectureManifestError(f"manifest_unreadable:{path}") from exc
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchitectureManifestError(f"manifest_invalid_json:{path}") from exc
    payload = _mapping(value, kind)
    if payload.get("manifest_kind") != kind:
        raise ArchitectureManifestError(f"manifest_kind_mismatch:{path}")
    claimed_hash = payload.get("content_hash")
    if not isinstance(claimed_hash, str) or not _HASH.fullmatch(claimed_hash):
        raise ArchitectureManifestError(f"manifest_content_hash_invalid:{path}")
    hash_input = dict(payload)
    del hash_input["content_hash"]
    if claimed_hash != _content_hash(hash_input):
        raise ArchitectureManifestError(f"manifest_content_hash_mismatch:{path}")
    return payload


@dataclass(frozen=True, slots=True, order=True)
class SymbolRef:
    module: str
    symbol: str
    kind: str

    @classmethod
    def from_dict(cls, value: object, label: str) -> SymbolRef:
        payload = _mapping(value, label)
        _exact(payload, frozenset({"kind", "module", "symbol"}), label)
        module = _text(payload["module"], f"{label}.module")
        symbol = _text(payload["symbol"], f"{label}.symbol")
        kind = _text(payload["kind"], f"{label}.kind")
        if not module.startswith("market_research."):
            raise ArchitectureManifestError(f"{label}.module_outside_research")
        if kind not in {"class", "function"}:
            raise ArchitectureManifestError(f"{label}.kind_invalid")
        return cls(module=module, symbol=symbol, kind=kind)


def _refs(value: object, label: str) -> tuple[SymbolRef, ...]:
    result = tuple(
        SymbolRef.from_dict(item, f"{label}.{index}")
        for index, item in enumerate(_sequence(value, label))
    )
    if not result:
        raise ArchitectureManifestError(f"{label}_empty")
    if result != tuple(sorted(set(result))):
        raise ArchitectureManifestError(f"{label}_not_sorted_unique")
    return result


@dataclass(frozen=True, slots=True, order=True)
class ConsumerContract:
    module: str
    symbol: str
    required_symbols: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object, label: str) -> ConsumerContract:
        payload = _mapping(value, label)
        _exact(
            payload,
            frozenset({"module", "required_symbols", "symbol"}),
            label,
        )
        module = _text(payload["module"], f"{label}.module")
        if not module.startswith("market_research."):
            raise ArchitectureManifestError(f"{label}.module_outside_research")
        return cls(
            module=module,
            symbol=_text(payload["symbol"], f"{label}.symbol"),
            required_symbols=_strings(
                payload["required_symbols"],
                f"{label}.required_symbols",
            ),
        )


def _consumers(value: object, label: str) -> tuple[ConsumerContract, ...]:
    result = tuple(
        ConsumerContract.from_dict(item, f"{label}.{index}")
        for index, item in enumerate(_sequence(value, label))
    )
    if not result:
        raise ArchitectureManifestError(f"{label}_empty")
    if result != tuple(sorted(set(result))):
        raise ArchitectureManifestError(f"{label}_not_sorted_unique")
    return result


@dataclass(frozen=True, slots=True)
class AuthorityContract:
    authority_id: str
    responsibility: str
    authoritative_module: str
    sole_producer: str
    public_entries: tuple[SymbolRef, ...]
    constructor_symbols: tuple[str, ...]
    allowed_constructor_modules: tuple[str, ...]
    forbidden_constructor_symbols: tuple[str, ...]
    consumers: tuple[ConsumerContract, ...]
    evidence_role: str
    legacy_migration_id: str

    @classmethod
    def from_dict(cls, value: object, label: str) -> AuthorityContract:
        payload = _mapping(value, label)
        _exact(
            payload,
            frozenset(
                {
                    "allowed_constructor_modules",
                    "authoritative_module",
                    "authority_id",
                    "constructor_symbols",
                    "consumers",
                    "evidence_role",
                    "forbidden_constructor_symbols",
                    "legacy_migration_id",
                    "public_entries",
                    "responsibility",
                    "sole_producer",
                }
            ),
            label,
        )
        authority_id = _identifier(payload["authority_id"], f"{label}.authority_id")
        authoritative_module = _text(
            payload["authoritative_module"],
            f"{label}.authoritative_module",
        )
        allowed = _strings(
            payload["allowed_constructor_modules"],
            f"{label}.allowed_constructor_modules",
        )
        if authoritative_module not in allowed:
            raise ArchitectureManifestError(
                f"{label}.authoritative_module_not_constructor_authority"
            )
        return cls(
            authority_id=authority_id,
            responsibility=_text(
                payload["responsibility"],
                f"{label}.responsibility",
            ),
            authoritative_module=authoritative_module,
            sole_producer=_text(payload["sole_producer"], f"{label}.sole_producer"),
            public_entries=_refs(
                payload["public_entries"],
                f"{label}.public_entries",
            ),
            constructor_symbols=_strings(
                payload["constructor_symbols"],
                f"{label}.constructor_symbols",
            ),
            allowed_constructor_modules=allowed,
            forbidden_constructor_symbols=_strings(
                payload["forbidden_constructor_symbols"],
                f"{label}.forbidden_constructor_symbols",
            ),
            consumers=_consumers(payload["consumers"], f"{label}.consumers"),
            evidence_role=_text(payload["evidence_role"], f"{label}.evidence_role"),
            legacy_migration_id=_identifier(
                payload["legacy_migration_id"],
                f"{label}.legacy_migration_id",
            ),
        )


@dataclass(frozen=True, slots=True)
class AuthorityManifest:
    manifest_id: str
    package_prefix: str
    authorities: tuple[AuthorityContract, ...]
    content_hash: str

    @classmethod
    def load(cls, path: Path) -> AuthorityManifest:
        payload = _load_payload(path, kind="multi_asset_authorities")
        _exact(
            payload,
            frozenset(
                {
                    "authorities",
                    "content_hash",
                    "manifest_id",
                    "manifest_kind",
                    "package_prefix",
                    "schema_version",
                }
            ),
            "authority_manifest",
        )
        if payload["schema_version"] != ARCHITECTURE_SCHEMA_VERSION:
            raise ArchitectureManifestError("authority_schema_version_unsupported")
        if payload["package_prefix"] != PACKAGE_PREFIX:
            raise ArchitectureManifestError("authority_package_prefix_invalid")
        authorities = tuple(
            AuthorityContract.from_dict(item, f"authorities.{index}")
            for index, item in enumerate(
                _sequence(payload["authorities"], "authorities")
            )
        )
        ids = tuple(item.authority_id for item in authorities)
        if ids != tuple(sorted(set(ids))):
            raise ArchitectureManifestError("authorities_not_sorted_unique")
        if set(ids) != REQUIRED_AUTHORITIES:
            raise ArchitectureManifestError("authority_inventory_incomplete")
        return cls(
            manifest_id=_identifier(payload["manifest_id"], "authority.manifest_id"),
            package_prefix=PACKAGE_PREFIX,
            authorities=authorities,
            content_hash=cast(str, payload["content_hash"]),
        )


@dataclass(frozen=True, slots=True, order=True)
class ModuleConformance:
    module: str
    layer: str
    authority_ids: tuple[str, ...]
    public_api_policy: str

    @classmethod
    def from_dict(cls, value: object, label: str) -> ModuleConformance:
        payload = _mapping(value, label)
        _exact(
            payload,
            frozenset(
                {"authority_ids", "layer", "module", "public_api_policy"}
            ),
            label,
        )
        policy = _text(payload["public_api_policy"], f"{label}.public_api_policy")
        if policy not in {"explicit_exports", "internal_support", "module_contract"}:
            raise ArchitectureManifestError(f"{label}.public_api_policy_invalid")
        return cls(
            module=_text(payload["module"], f"{label}.module"),
            layer=_text(payload["layer"], f"{label}.layer"),
            authority_ids=_strings(
                payload["authority_ids"],
                f"{label}.authority_ids",
                allow_empty=True,
            ),
            public_api_policy=policy,
        )


@dataclass(frozen=True, slots=True, order=True)
class ConstructorGuard:
    rule_id: str
    category: str
    symbols: tuple[str, ...]
    allowed_modules: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object, label: str) -> ConstructorGuard:
        payload = _mapping(value, label)
        _exact(
            payload,
            frozenset({"allowed_modules", "category", "rule_id", "symbols"}),
            label,
        )
        return cls(
            rule_id=_identifier(payload["rule_id"], f"{label}.rule_id"),
            category=_identifier(payload["category"], f"{label}.category"),
            symbols=_strings(payload["symbols"], f"{label}.symbols"),
            allowed_modules=_strings(
                payload["allowed_modules"],
                f"{label}.allowed_modules",
            ),
        )


@dataclass(frozen=True, slots=True, order=True)
class PriceAllowance:
    module: str
    qualname: str
    occurrence: str
    semantic: str

    @classmethod
    def from_dict(cls, value: object, label: str) -> PriceAllowance:
        payload = _mapping(value, label)
        _exact(
            payload,
            frozenset({"module", "occurrence", "qualname", "semantic"}),
            label,
        )
        occurrence = _text(payload["occurrence"], f"{label}.occurrence")
        if occurrence not in {"argument", "field", "method"}:
            raise ArchitectureManifestError(f"{label}.occurrence_invalid")
        return cls(
            module=_text(payload["module"], f"{label}.module"),
            qualname=_text(payload["qualname"], f"{label}.qualname"),
            occurrence=occurrence,
            semantic=_text(payload["semantic"], f"{label}.semantic"),
        )


@dataclass(frozen=True, slots=True)
class ContinuousFuturePolicy:
    signal_symbols: tuple[str, ...]
    signal_attribute_names: tuple[str, ...]
    trade_sink_calls: tuple[str, ...]
    trade_sink_keywords: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object, label: str) -> ContinuousFuturePolicy:
        payload = _mapping(value, label)
        _exact(
            payload,
            frozenset(
                {
                    "signal_attribute_names",
                    "signal_symbols",
                    "trade_sink_calls",
                    "trade_sink_keywords",
                }
            ),
            label,
        )
        return cls(
            signal_symbols=_strings(
                payload["signal_symbols"],
                f"{label}.signal_symbols",
            ),
            signal_attribute_names=_strings(
                payload["signal_attribute_names"],
                f"{label}.signal_attribute_names",
            ),
            trade_sink_calls=_strings(
                payload["trade_sink_calls"],
                f"{label}.trade_sink_calls",
            ),
            trade_sink_keywords=_strings(
                payload["trade_sink_keywords"],
                f"{label}.trade_sink_keywords",
            ),
        )


@dataclass(frozen=True, slots=True)
class SupplierAnalyticsPolicy:
    supplier_type: str
    supplier_attribute_names: tuple[str, ...]
    authoritative_targets: tuple[str, ...]
    comparison_module: str

    @classmethod
    def from_dict(cls, value: object, label: str) -> SupplierAnalyticsPolicy:
        payload = _mapping(value, label)
        _exact(
            payload,
            frozenset(
                {
                    "authoritative_targets",
                    "comparison_module",
                    "supplier_attribute_names",
                    "supplier_type",
                }
            ),
            label,
        )
        return cls(
            supplier_type=_text(payload["supplier_type"], f"{label}.supplier_type"),
            supplier_attribute_names=_strings(
                payload["supplier_attribute_names"],
                f"{label}.supplier_attribute_names",
            ),
            authoritative_targets=_strings(
                payload["authoritative_targets"],
                f"{label}.authoritative_targets",
            ),
            comparison_module=_text(
                payload["comparison_module"],
                f"{label}.comparison_module",
            ),
        )


@dataclass(frozen=True, slots=True)
class ReceiptPolicy:
    certification_keywords: tuple[str, ...]
    receipt_suffixes: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object, label: str) -> ReceiptPolicy:
        payload = _mapping(value, label)
        _exact(
            payload,
            frozenset({"certification_keywords", "receipt_suffixes"}),
            label,
        )
        return cls(
            certification_keywords=_strings(
                payload["certification_keywords"],
                f"{label}.certification_keywords",
            ),
            receipt_suffixes=_strings(
                payload["receipt_suffixes"],
                f"{label}.receipt_suffixes",
            ),
        )


@dataclass(frozen=True, slots=True)
class LedgerPolicy:
    allowed_ledger_class_names: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object, label: str) -> LedgerPolicy:
        payload = _mapping(value, label)
        _exact(payload, frozenset({"allowed_ledger_class_names"}), label)
        return cls(
            allowed_ledger_class_names=_strings(
                payload["allowed_ledger_class_names"],
                f"{label}.allowed_ledger_class_names",
            )
        )


@dataclass(frozen=True, slots=True)
class ResearchOnlyPolicy:
    forbidden_call_names: tuple[str, ...]
    forbidden_definition_names: tuple[str, ...]
    forbidden_field_names: tuple[str, ...]
    forbidden_import_prefixes: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object, label: str) -> ResearchOnlyPolicy:
        payload = _mapping(value, label)
        _exact(
            payload,
            frozenset(
                {
                    "forbidden_call_names",
                    "forbidden_definition_names",
                    "forbidden_field_names",
                    "forbidden_import_prefixes",
                }
            ),
            label,
        )
        return cls(
            forbidden_call_names=_strings(
                payload["forbidden_call_names"],
                f"{label}.forbidden_call_names",
            ),
            forbidden_definition_names=_strings(
                payload["forbidden_definition_names"],
                f"{label}.forbidden_definition_names",
            ),
            forbidden_field_names=_strings(
                payload["forbidden_field_names"],
                f"{label}.forbidden_field_names",
            ),
            forbidden_import_prefixes=_strings(
                payload["forbidden_import_prefixes"],
                f"{label}.forbidden_import_prefixes",
            ),
        )


@dataclass(frozen=True, slots=True)
class BoundaryManifest:
    manifest_id: str
    package_prefix: str
    generated_document: str
    modules: tuple[ModuleConformance, ...]
    constructor_guards: tuple[ConstructorGuard, ...]
    generic_price_allowlist: tuple[PriceAllowance, ...]
    continuous_future_policy: ContinuousFuturePolicy
    supplier_analytics_policy: SupplierAnalyticsPolicy
    receipt_policy: ReceiptPolicy
    ledger_policy: LedgerPolicy
    research_only_policy: ResearchOnlyPolicy
    content_hash: str

    @classmethod
    def load(cls, path: Path) -> BoundaryManifest:
        payload = _load_payload(path, kind="multi_asset_boundaries")
        _exact(
            payload,
            frozenset(
                {
                    "constructor_guards",
                    "content_hash",
                    "continuous_future_policy",
                    "generated_document",
                    "generic_price_allowlist",
                    "ledger_policy",
                    "manifest_id",
                    "manifest_kind",
                    "modules",
                    "package_prefix",
                    "receipt_policy",
                    "research_only_policy",
                    "schema_version",
                    "supplier_analytics_policy",
                }
            ),
            "boundary_manifest",
        )
        if payload["schema_version"] != ARCHITECTURE_SCHEMA_VERSION:
            raise ArchitectureManifestError("boundary_schema_version_unsupported")
        if payload["package_prefix"] != PACKAGE_PREFIX:
            raise ArchitectureManifestError("boundary_package_prefix_invalid")
        modules = tuple(
            ModuleConformance.from_dict(item, f"modules.{index}")
            for index, item in enumerate(_sequence(payload["modules"], "modules"))
        )
        if modules != tuple(sorted(set(modules))):
            raise ArchitectureManifestError("modules_not_sorted_unique")
        guards = tuple(
            ConstructorGuard.from_dict(item, f"constructor_guards.{index}")
            for index, item in enumerate(
                _sequence(payload["constructor_guards"], "constructor_guards")
            )
        )
        if guards != tuple(sorted(set(guards))):
            raise ArchitectureManifestError("constructor_guards_not_sorted_unique")
        prices = tuple(
            PriceAllowance.from_dict(item, f"generic_price_allowlist.{index}")
            for index, item in enumerate(
                _sequence(
                    payload["generic_price_allowlist"],
                    "generic_price_allowlist",
                )
            )
        )
        if prices != tuple(sorted(set(prices))):
            raise ArchitectureManifestError(
                "generic_price_allowlist_not_sorted_unique"
            )
        return cls(
            manifest_id=_identifier(payload["manifest_id"], "boundary.manifest_id"),
            package_prefix=PACKAGE_PREFIX,
            generated_document=_text(
                payload["generated_document"],
                "boundary.generated_document",
            ),
            modules=modules,
            constructor_guards=guards,
            generic_price_allowlist=prices,
            continuous_future_policy=ContinuousFuturePolicy.from_dict(
                payload["continuous_future_policy"],
                "continuous_future_policy",
            ),
            supplier_analytics_policy=SupplierAnalyticsPolicy.from_dict(
                payload["supplier_analytics_policy"],
                "supplier_analytics_policy",
            ),
            receipt_policy=ReceiptPolicy.from_dict(
                payload["receipt_policy"],
                "receipt_policy",
            ),
            ledger_policy=LedgerPolicy.from_dict(
                payload["ledger_policy"],
                "ledger_policy",
            ),
            research_only_policy=ResearchOnlyPolicy.from_dict(
                payload["research_only_policy"],
                "research_only_policy",
            ),
            content_hash=cast(str, payload["content_hash"]),
        )


@dataclass(frozen=True, slots=True, order=True)
class LegacySymbol:
    module: str
    symbol: str

    @classmethod
    def from_dict(cls, value: object, label: str) -> LegacySymbol:
        payload = _mapping(value, label)
        _exact(payload, frozenset({"module", "symbol"}), label)
        return cls(
            module=_text(payload["module"], f"{label}.module"),
            symbol=_text(payload["symbol"], f"{label}.symbol"),
        )


def _legacy_symbols(value: object, label: str) -> tuple[LegacySymbol, ...]:
    result = tuple(
        LegacySymbol.from_dict(item, f"{label}.{index}")
        for index, item in enumerate(_sequence(value, label))
    )
    if not result:
        raise ArchitectureManifestError(f"{label}_empty")
    if result != tuple(sorted(set(result))):
        raise ArchitectureManifestError(f"{label}_not_sorted_unique")
    return result


@dataclass(frozen=True, slots=True, order=True)
class MigrationContract:
    migration_id: str
    authority_id: str
    status: str
    legacy_symbols: tuple[LegacySymbol, ...]
    replacements: tuple[SymbolRef, ...]
    production_callers: tuple[ConsumerContract, ...]
    deprecation_policy: str

    @classmethod
    def from_dict(cls, value: object, label: str) -> MigrationContract:
        payload = _mapping(value, label)
        _exact(
            payload,
            frozenset(
                {
                    "authority_id",
                    "deprecation_policy",
                    "legacy_symbols",
                    "migration_id",
                    "production_callers",
                    "replacements",
                    "status",
                }
            ),
            label,
        )
        status = _text(payload["status"], f"{label}.status")
        if status != "migrated":
            raise ArchitectureManifestError(f"{label}.status_not_migrated")
        return cls(
            migration_id=_identifier(
                payload["migration_id"],
                f"{label}.migration_id",
            ),
            authority_id=_identifier(
                payload["authority_id"],
                f"{label}.authority_id",
            ),
            status=status,
            legacy_symbols=_legacy_symbols(
                payload["legacy_symbols"],
                f"{label}.legacy_symbols",
            ),
            replacements=_refs(payload["replacements"], f"{label}.replacements"),
            production_callers=_consumers(
                payload["production_callers"],
                f"{label}.production_callers",
            ),
            deprecation_policy=_text(
                payload["deprecation_policy"],
                f"{label}.deprecation_policy",
            ),
        )


@dataclass(frozen=True, slots=True)
class MigrationManifest:
    manifest_id: str
    migrations: tuple[MigrationContract, ...]
    content_hash: str

    @classmethod
    def load(cls, path: Path) -> MigrationManifest:
        payload = _load_payload(path, kind="multi_asset_migrations")
        _exact(
            payload,
            frozenset(
                {
                    "content_hash",
                    "manifest_id",
                    "manifest_kind",
                    "migrations",
                    "schema_version",
                }
            ),
            "migration_manifest",
        )
        if payload["schema_version"] != ARCHITECTURE_SCHEMA_VERSION:
            raise ArchitectureManifestError("migration_schema_version_unsupported")
        migrations = tuple(
            MigrationContract.from_dict(item, f"migrations.{index}")
            for index, item in enumerate(
                _sequence(payload["migrations"], "migrations")
            )
        )
        if migrations != tuple(sorted(set(migrations))):
            raise ArchitectureManifestError("migrations_not_sorted_unique")
        if {item.authority_id for item in migrations} != REQUIRED_AUTHORITIES:
            raise ArchitectureManifestError("migration_authority_inventory_incomplete")
        if len(migrations) != len(REQUIRED_AUTHORITIES):
            raise ArchitectureManifestError("migration_authority_not_one_to_one")
        return cls(
            manifest_id=_identifier(payload["manifest_id"], "migration.manifest_id"),
            migrations=migrations,
            content_hash=cast(str, payload["content_hash"]),
        )


@dataclass(frozen=True, slots=True)
class MultiAssetArchitecture:
    authorities: AuthorityManifest
    boundaries: BoundaryManifest
    migrations: MigrationManifest

    @property
    def content_hash(self) -> str:
        return _content_hash(
            {
                "authority_manifest_hash": self.authorities.content_hash,
                "boundary_manifest_hash": self.boundaries.content_hash,
                "migration_manifest_hash": self.migrations.content_hash,
            }
        )


def default_manifest_directory() -> Path:
    return Path(__file__).with_name("manifests")


def load_multi_asset_architecture(
    manifest_directory: Path | None = None,
) -> MultiAssetArchitecture:
    directory = default_manifest_directory() if manifest_directory is None else (
        manifest_directory
    )
    result = MultiAssetArchitecture(
        authorities=AuthorityManifest.load(directory / "authorities.v1.json"),
        boundaries=BoundaryManifest.load(directory / "boundaries.v1.json"),
        migrations=MigrationManifest.load(directory / "migrations.v1.json"),
    )
    authority_migrations = {
        item.authority_id: item.legacy_migration_id
        for item in result.authorities.authorities
    }
    actual_migrations = {
        item.authority_id: item.migration_id for item in result.migrations.migrations
    }
    if authority_migrations != actual_migrations:
        raise ArchitectureManifestError("authority_migration_binding_mismatch")
    known_authorities = {item.authority_id for item in result.authorities.authorities}
    for module in result.boundaries.modules:
        unknown = set(module.authority_ids) - known_authorities
        if unknown:
            raise ArchitectureManifestError(
                f"module_unknown_authority:{module.module}:{','.join(sorted(unknown))}"
            )
    return result


@dataclass(frozen=True, slots=True, order=True)
class ArchitectureFinding:
    code: str
    module: str
    symbol: str
    line: int
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "module": self.module,
            "symbol": self.symbol,
            "line": self.line,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ArchitectureValidationReport:
    architecture_hash: str
    discovered_modules: tuple[str, ...]
    findings: tuple[ArchitectureFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": ARCHITECTURE_SCHEMA_VERSION,
            "architecture_hash": self.architecture_hash,
            "discovered_modules": list(self.discovered_modules),
            "finding_count": len(self.findings),
            "status": "PASS" if self.ok else "FAIL",
            "findings": [item.as_dict() for item in self.findings],
        }


def _module_path(project_root: Path, module: str) -> Path:
    relative = Path(*module.split("."))
    file_path = project_root / "src" / relative.with_suffix(".py")
    if file_path.is_file():
        return file_path
    package_path = project_root / "src" / relative / "__init__.py"
    return package_path


def discover_multi_asset_modules(project_root: Path) -> tuple[str, ...]:
    package_root = project_root / "src" / Path(*PACKAGE_PREFIX.split("."))
    modules: list[str] = []
    if not package_root.is_dir():
        raise ArchitectureManifestError("multi_asset_package_missing")
    for path in package_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package_root)
        parts = list(relative.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = Path(parts[-1]).stem
        suffix = ".".join(parts)
        modules.append(PACKAGE_PREFIX if not suffix else f"{PACKAGE_PREFIX}.{suffix}")
    return tuple(sorted(modules))


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _annotation_text(value: ast.expr | None) -> str:
    if value is None:
        return ""
    try:
        return ast.unparse(value)
    except (ValueError, TypeError):
        return ""


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            result[child] = parent
    return result


def _qualname(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names)) or "<module>"


def _names_in(node: ast.AST) -> frozenset[str]:
    names: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            names.add(item.id)
        elif isinstance(item, ast.Attribute):
            names.add(item.attr)
    return frozenset(names)


def _tainted_names(tree: ast.AST, type_names: frozenset[str]) -> frozenset[str]:
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
            for argument in arguments:
                if type_names & _names_in(argument.annotation or ast.Constant(None)):
                    result.add(argument.arg)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if type_names & _names_in(node.annotation):
                result.add(node.target.id)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, ast.Call) and _call_name(value) in type_names:
                targets = (
                    node.targets if isinstance(node, ast.Assign) else (node.target,)
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        result.add(target.id)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(
                node.value,
                (ast.Name, ast.Attribute, ast.Subscript),
            ):
                continue
            if not (_names_in(node.value) & result):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in result:
                    result.add(target.id)
                    changed = True
    return frozenset(result)


def _has_taint(
    node: ast.AST,
    *,
    tainted_names: frozenset[str],
    tainted_attributes: frozenset[str],
) -> bool:
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and item.id in tainted_names:
            return True
        if isinstance(item, ast.Attribute) and item.attr in tainted_attributes:
            return True
    return False


def _parse_source(module: str, source: str) -> ast.Module:
    try:
        return ast.parse(source, filename=module)
    except SyntaxError as exc:
        raise ArchitectureManifestError(
            f"module_syntax_error:{module}:{exc.lineno or 0}"
        ) from exc


def scan_python_source(
    module: str,
    source: str,
    boundaries: BoundaryManifest,
) -> tuple[ArchitectureFinding, ...]:
    """Apply manifest-driven negative architecture rules to one module."""

    tree = _parse_source(module, source)
    parents = _parent_map(tree)
    findings: list[ArchitectureFinding] = []

    def add(code: str, node: ast.AST, detail: str) -> None:
        findings.append(
            ArchitectureFinding(
                code=code,
                module=module,
                symbol=_qualname(node, parents),
                line=getattr(node, "lineno", 0),
                detail=detail,
            )
        )

    guard_by_symbol: dict[str, ConstructorGuard] = {}
    factory_token_modules: set[str] = set()
    for guard in boundaries.constructor_guards:
        if guard.category == "caller_certified_receipt":
            factory_token_modules.update(guard.allowed_modules)
        for symbol in guard.symbols:
            if symbol in guard_by_symbol:
                raise ArchitectureManifestError(
                    f"constructor_symbol_has_multiple_guards:{symbol}"
                )
            guard_by_symbol[symbol] = guard

    supplier = boundaries.supplier_analytics_policy
    supplier_names = _tainted_names(tree, frozenset({supplier.supplier_type}))
    continuous = boundaries.continuous_future_policy
    continuous_names = _tainted_names(tree, frozenset(continuous.signal_symbols))
    research_only = boundaries.research_only_policy
    price_allowances = {
        (item.module, item.qualname, item.occurrence)
        for item in boundaries.generic_price_allowlist
    }

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported = (
                tuple(item.name for item in node.names)
                if isinstance(node, ast.Import)
                else ((node.module or ""),)
            )
            for name in imported:
                if any(
                    name == prefix or name.startswith(prefix + ".")
                    for prefix in research_only.forbidden_import_prefixes
                ):
                    add("research_only_forbidden_import", node, name)

        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in research_only.forbidden_definition_names:
                add("research_only_forbidden_definition", node, node.name)
            if (
                isinstance(node, ast.ClassDef)
                and node.name.endswith("Ledger")
                and node.name not in boundaries.ledger_policy.allowed_ledger_class_names
            ):
                add("separate_product_ledger_forbidden", node, node.name)

        if isinstance(node, ast.arg):
            if node.arg in research_only.forbidden_field_names:
                add("research_only_forbidden_field", node, node.arg)
            if node.arg == "price":
                key = (module, _qualname(node, parents), "argument")
                if key not in price_allowances:
                    add("generic_price_semantic_undeclared", node, ".".join(key))

        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in research_only.forbidden_field_names
        ):
            add("research_only_forbidden_field", node, node.target.id)

        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "price"
        ):
            key = (module, _qualname(node, parents), "field")
            if key not in price_allowances:
                add("generic_price_semantic_undeclared", node, ".".join(key))

        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "price"
        ):
            key = (module, _qualname(node, parents), "method")
            if key not in price_allowances:
                add("generic_price_semantic_undeclared", node, ".".join(key))

        if not isinstance(node, ast.Call):
            continue
        called = _call_name(node)
        if called in research_only.forbidden_call_names:
            add("research_only_forbidden_call", node, called)
        matched_guard = guard_by_symbol.get(called)
        if matched_guard is not None and module not in matched_guard.allowed_modules:
            add(
                f"{matched_guard.category}_constructor_bypass",
                node,
                f"{called}:owner={','.join(matched_guard.allowed_modules)}",
            )
        if any(keyword.arg == "_factory_token" for keyword in node.keywords):
            if module not in factory_token_modules:
                add("caller_supplied_factory_token", node, called or "<dynamic>")

        if called in supplier.authoritative_targets:
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue
                if _has_taint(
                    keyword.value,
                    tainted_names=supplier_names,
                    tainted_attributes=frozenset(
                        supplier.supplier_attribute_names
                    ),
                ):
                    add(
                        "direct_supplier_analytics_forbidden",
                        keyword,
                        f"{called}.{keyword.arg}",
                    )

        is_trade_sink = called in continuous.trade_sink_calls
        for keyword in node.keywords:
            if keyword.arg not in continuous.trade_sink_keywords:
                continue
            if _has_taint(
                keyword.value,
                tainted_names=continuous_names,
                tainted_attributes=frozenset(
                    continuous.signal_attribute_names
                ),
            ):
                add(
                    "continuous_future_trade_forbidden",
                    keyword,
                    f"{called}.{keyword.arg}",
                )
        if is_trade_sink:
            for argument in node.args:
                if _has_taint(
                    argument,
                    tainted_names=continuous_names,
                    tainted_attributes=frozenset(
                        continuous.signal_attribute_names
                    ),
                ):
                    add(
                        "continuous_future_trade_forbidden",
                        argument,
                        called,
                    )

        if called.endswith(boundaries.receipt_policy.receipt_suffixes):
            for keyword in node.keywords:
                if (
                    keyword.arg in boundaries.receipt_policy.certification_keywords
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    add(
                        "caller_certified_receipt_forbidden",
                        keyword,
                        f"{called}.{keyword.arg}",
                    )
    return tuple(sorted(set(findings)))


def _definition_node(tree: ast.Module, symbol: str) -> ast.AST | None:
    body: Sequence[ast.stmt] = tree.body
    current: ast.AST | None = None
    for part in symbol.split("."):
        current = next(
            (
                node
                for node in body
                if isinstance(
                    node,
                    (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                )
                and node.name == part
            ),
            None,
        )
        if current is None:
            return None
        body = cast(
            Sequence[ast.stmt],
            getattr(current, "body", ()),
        )
    return current


def _all_exports(tree: ast.Module) -> tuple[str, ...] | None:
    for node in tree.body:
        value: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            )
        ):
            value = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        ):
            value = node.value
        if isinstance(value, (ast.Tuple, ast.List)):
            exports = tuple(
                item.value
                for item in value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
            if len(exports) != len(value.elts):
                raise ArchitectureManifestError("dynamic_all_export_forbidden")
            return exports
    return None


def _top_level_names(tree: ast.Module) -> frozenset[str]:
    result: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            result.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (
                node.targets if isinstance(node, ast.Assign) else (node.target,)
            )
            for target in targets:
                if isinstance(target, ast.Name):
                    result.add(target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for item in node.names:
                result.add(item.asname or item.name.rsplit(".", 1)[-1])
    return frozenset(result)


def _render_entries(entries: Sequence[SymbolRef]) -> str:
    return "<br>".join(
        f"`{item.module}.{item.symbol}`" for item in sorted(entries)
    )


def _render_consumers(consumers: Sequence[ConsumerContract]) -> str:
    return "<br>".join(
        f"`{item.module}.{item.symbol}`" for item in sorted(consumers)
    )


def render_responsibility_table(architecture: MultiAssetArchitecture) -> str:
    migrations = {
        item.authority_id: item for item in architecture.migrations.migrations
    }
    lines = [
        "| Responsibility | Sole producer | Public entry | Constructor authority | Consumers | Evidence role | Legacy migration |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for authority in architecture.authorities.authorities:
        migration = migrations[authority.authority_id]
        constructors = "<br>".join(
            f"`{item}`" for item in authority.allowed_constructor_modules
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    authority.responsibility,
                    (
                        f"`{authority.authoritative_module}."
                        f"{authority.sole_producer}`"
                    ),
                    _render_entries(authority.public_entries),
                    constructors,
                    _render_consumers(authority.consumers),
                    authority.evidence_role,
                    f"`{migration.migration_id}` ({migration.status})",
                )
            )
            + " |"
        )
    return "\n".join(lines)


def render_responsibility_document(
    architecture: MultiAssetArchitecture,
) -> str:
    return (
        "# Generated multi-asset responsibility map\n\n"
        "This file is generated from the packaged authority, boundary, and "
        "migration manifests. Edit the manifests, not this table.\n\n"
        f"Architecture set: `{architecture.content_hash}`\n\n"
        + render_responsibility_table(architecture)
        + "\n"
    )


def render_embedded_responsibility_section(
    architecture: MultiAssetArchitecture,
) -> str:
    return (
        f"{_RESPONSIBILITY_BEGIN}\n"
        "The executable source of truth is the packaged architecture manifest "
        "set. The expanded canonical view is "
        "[`multi-asset-responsibility-map.generated.md`]"
        "(multi-asset-responsibility-map.generated.md).\n\n"
        + render_responsibility_table(architecture)
        + f"\n{_RESPONSIBILITY_END}"
    )


def _embedded_section(document: str) -> str | None:
    start = document.find(_RESPONSIBILITY_BEGIN)
    end = document.find(_RESPONSIBILITY_END)
    if start < 0 or end < start:
        return None
    return document[start : end + len(_RESPONSIBILITY_END)]


def validate_multi_asset_architecture(
    project_root: Path,
    *,
    manifest_directory: Path | None = None,
) -> ArchitectureValidationReport:
    architecture = load_multi_asset_architecture(manifest_directory)
    discovered = discover_multi_asset_modules(project_root)
    declared = tuple(item.module for item in architecture.boundaries.modules)
    findings: list[ArchitectureFinding] = []

    def finding(
        code: str,
        module: str,
        symbol: str,
        detail: str,
        line: int = 0,
    ) -> None:
        findings.append(
            ArchitectureFinding(
                code=code,
                module=module,
                symbol=symbol,
                line=line,
                detail=detail,
            )
        )

    for module in sorted(set(discovered) - set(declared)):
        finding("unmanifested_module", module, "<module>", module)
    for module in sorted(set(declared) - set(discovered)):
        finding("stale_manifest_module", module, "<module>", module)

    trees: dict[str, ast.Module] = {}
    for module in discovered:
        path = _module_path(project_root, module)
        source = path.read_text(encoding="utf-8")
        try:
            tree = _parse_source(module, source)
        except ArchitectureManifestError as exc:
            finding("module_syntax_error", module, "<module>", str(exc))
            continue
        trees[module] = tree
        findings.extend(scan_python_source(module, source, architecture.boundaries))

    conformance = {
        item.module: item for item in architecture.boundaries.modules
    }
    for module, contract in conformance.items():
        conformance_tree = trees.get(module)
        if conformance_tree is None:
            continue
        exports = _all_exports(conformance_tree)
        if contract.public_api_policy == "explicit_exports" and exports is None:
            finding("explicit_exports_missing", module, "__all__", module)
        if exports is not None:
            names = _top_level_names(conformance_tree)
            for export in exports:
                if export not in names:
                    finding("orphan_all_export", module, export, export)

    external_trees: dict[str, ast.Module] = {}

    def tree_for(module: str) -> ast.Module | None:
        if module in trees:
            return trees[module]
        if module in external_trees:
            return external_trees[module]
        path = _module_path(project_root, module)
        if not path.is_file():
            return None
        result = _parse_source(module, path.read_text(encoding="utf-8"))
        external_trees[module] = result
        return result

    for authority in architecture.authorities.authorities:
        public_symbols = {item.symbol for item in authority.public_entries}
        consumed_symbols = {
            required
            for consumer in authority.consumers
            for required in consumer.required_symbols
        }
        for entry in authority.public_entries:
            entry_tree = tree_for(entry.module)
            node = (
                None
                if entry_tree is None
                else _definition_node(entry_tree, entry.symbol)
            )
            if node is None:
                finding(
                    "public_entry_missing",
                    entry.module,
                    entry.symbol,
                    authority.authority_id,
                )
                continue
            if entry.kind == "class" and not isinstance(node, ast.ClassDef):
                finding(
                    "public_entry_kind_mismatch",
                    entry.module,
                    entry.symbol,
                    entry.kind,
                    getattr(node, "lineno", 0),
                )
            if entry.kind == "function" and not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                finding(
                    "public_entry_kind_mismatch",
                    entry.module,
                    entry.symbol,
                    entry.kind,
                    getattr(node, "lineno", 0),
                )
            module_contract = conformance.get(entry.module)
            if module_contract is not None:
                assert entry_tree is not None
                exports = _all_exports(entry_tree)
                if (
                    module_contract.public_api_policy == "explicit_exports"
                    and exports is not None
                    and entry.symbol not in exports
                ):
                    finding(
                        "public_entry_not_exported",
                        entry.module,
                        entry.symbol,
                        authority.authority_id,
                        getattr(node, "lineno", 0),
                    )
            if entry.symbol not in consumed_symbols:
                finding(
                    "orphan_public_entry",
                    entry.module,
                    entry.symbol,
                    authority.authority_id,
                    getattr(node, "lineno", 0),
                )
        for consumer in authority.consumers:
            consumer_tree = tree_for(consumer.module)
            node = (
                None
                if consumer_tree is None
                else _definition_node(consumer_tree, consumer.symbol)
            )
            if node is None:
                finding(
                    "declared_consumer_missing",
                    consumer.module,
                    consumer.symbol,
                    authority.authority_id,
                )
                continue
            names = _names_in(node)
            for required in consumer.required_symbols:
                if required not in public_symbols:
                    finding(
                        "consumer_requires_undeclared_entry",
                        consumer.module,
                        consumer.symbol,
                        required,
                        getattr(node, "lineno", 0),
                    )
                elif required not in names:
                    finding(
                        "declared_consumer_not_migrated",
                        consumer.module,
                        consumer.symbol,
                        required,
                        getattr(node, "lineno", 0),
                    )

    for migration in architecture.migrations.migrations:
        replacement_symbols = {item.symbol for item in migration.replacements}
        for caller in migration.production_callers:
            caller_tree = tree_for(caller.module)
            node = (
                None
                if caller_tree is None
                else _definition_node(caller_tree, caller.symbol)
            )
            if node is None:
                finding(
                    "migration_caller_missing",
                    caller.module,
                    caller.symbol,
                    migration.migration_id,
                )
                continue
            names = _names_in(node)
            for required in caller.required_symbols:
                if required not in replacement_symbols or required not in names:
                    finding(
                        "migration_caller_not_migrated",
                        caller.module,
                        caller.symbol,
                        required,
                        getattr(node, "lineno", 0),
                    )
        for legacy in migration.legacy_symbols:
            for module, tree in {**trees, **external_trees}.items():
                if module == legacy.module:
                    continue
                for node in ast.walk(tree):
                    if (
                        isinstance(node, ast.Name)
                        and node.id == legacy.symbol
                    ) or (
                        isinstance(node, ast.Attribute)
                        and node.attr == legacy.symbol
                    ):
                        finding(
                            "legacy_symbol_still_consumed",
                            module,
                            _qualname(node, _parent_map(tree)),
                            legacy.symbol,
                            getattr(node, "lineno", 0),
                        )

    generated_path = project_root / architecture.boundaries.generated_document
    expected_generated = render_responsibility_document(architecture)
    if (
        not generated_path.is_file()
        or generated_path.read_text(encoding="utf-8") != expected_generated
    ):
        finding(
            "generated_responsibility_map_drift",
            architecture.boundaries.generated_document,
            "<document>",
            architecture.content_hash,
        )
    main_doc_path = project_root / "docs" / "multi-asset-research.md"
    expected_embedded = render_embedded_responsibility_section(architecture)
    actual_embedded = (
        _embedded_section(main_doc_path.read_text(encoding="utf-8"))
        if main_doc_path.is_file()
        else None
    )
    if actual_embedded != expected_embedded:
        finding(
            "embedded_responsibility_map_drift",
            "docs.multi-asset-research",
            "Responsibility map",
            architecture.content_hash,
        )

    return ArchitectureValidationReport(
        architecture_hash=architecture.content_hash,
        discovered_modules=discovered,
        findings=tuple(sorted(set(findings))),
    )


__all__ = [
    "ARCHITECTURE_SCHEMA_VERSION",
    "ArchitectureFinding",
    "ArchitectureManifestError",
    "ArchitectureValidationReport",
    "AuthorityManifest",
    "BoundaryManifest",
    "MigrationManifest",
    "MultiAssetArchitecture",
    "default_manifest_directory",
    "discover_multi_asset_modules",
    "load_multi_asset_architecture",
    "render_embedded_responsibility_section",
    "render_responsibility_document",
    "render_responsibility_table",
    "scan_python_source",
    "validate_multi_asset_architecture",
]
