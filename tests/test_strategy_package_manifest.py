from __future__ import annotations

import ast
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import market_research.builtin_strategies as builtin_discovery
import market_research.research_composition.builtin_registry as composition
from market_research.builtin_strategies import (
    BuiltinStrategyDiscoveryReport,
    discover_builtin_strategy_factories,
)
from market_research.research.strategy_manifest import (
    StrategyManifestError,
    load_builtin_strategy_manifest,
    parse_strategy_manifest,
)


def test_every_builtin_package_manifest_is_bound_and_complete() -> None:
    catalog = composition.builtin_strategy_catalog()

    assert set(catalog.manifests) == {
        "buy_and_hold_baseline",
        "noop_baseline",
        "sma_with_filter",
        "threshold_research_only",
    }
    assert catalog.failures == ()
    for name, manifest in catalog.manifests.items():
        plugin = catalog.registry.resolve(name)
        manifest.validate_plugin(plugin)
        assert plugin.package_manifest_hash == manifest.content_hash()
        assert manifest.status == "ACTIVE"
        assert manifest.permissions["network"] == "denied"
        assert manifest.permissions["database_write"] is False
        assert set(manifest.hypothesis) == {
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
        schemas = plugin.spec.parameter_schema
        assert {item.name for item in schemas} == set(
            plugin.spec.accepted_parameter_names
        )
        assert all(
            item.description and item.unit and item.since_version for item in schemas
        )
        assert all(item.runtime_mutable is False for item in schemas)


def test_manifest_is_strict_and_contract_mismatch_fails_before_execution() -> None:
    path = Path("src/market_research/builtin_strategies/noop_baseline.strategy.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["unknown_legacy_field"] = True
    with pytest.raises(StrategyManifestError, match="fields_invalid"):
        parse_strategy_manifest(payload)

    manifest = load_builtin_strategy_manifest(
        "market_research.builtin_strategies.noop_baseline"
    )
    plugin = composition.builtin_strategy_registry().resolve("noop_baseline")
    with pytest.raises(StrategyManifestError, match="identity_mismatch"):
        replace(manifest, strategy_version="incompatible.v999").validate_plugin(plugin)


def test_one_import_failure_does_not_hide_other_strategy_factories(monkeypatch) -> None:
    def good() -> object:
        return object()

    modules = {
        "fixture.alpha": SimpleNamespace(STRATEGY_PLUGIN_FACTORY=good),
        "fixture.broken": ImportError("missing dependency"),
        "fixture.helper": SimpleNamespace(),
    }

    def import_one(name: str) -> object:
        value = modules[name]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(
        builtin_discovery,
        "iter_modules",
        lambda *_args, **_kwargs: [SimpleNamespace(name=name) for name in modules],
    )
    monkeypatch.setattr(builtin_discovery, "import_module", import_one)

    report = builtin_discovery.discover_builtin_strategy_modules()
    assert report.factories == (good,)
    assert [(item.module_name, item.reason_code) for item in report.failures] == [
        ("fixture.broken", "module_import_failed:ImportError")
    ]


def test_catalog_boots_when_one_package_factory_is_missing(monkeypatch) -> None:
    factories = tuple(
        factory
        for factory in discover_builtin_strategy_factories()
        if "threshold_research_only" not in factory.__module__
    )
    monkeypatch.setattr(
        composition,
        "discover_builtin_strategy_modules",
        lambda: BuiltinStrategyDiscoveryReport(factories, ()),
    )
    composition.builtin_strategy_catalog.cache_clear()
    try:
        catalog = composition.builtin_strategy_catalog()
        assert "threshold_research_only" not in catalog.registry.plugins
        assert set(catalog.registry.plugins) == {
            "buy_and_hold_baseline",
            "noop_baseline",
            "sma_with_filter",
        }
    finally:
        composition.builtin_strategy_catalog.cache_clear()


def test_builtin_strategy_modules_cannot_import_database_or_network_clients() -> None:
    forbidden_roots = {
        "django",
        "psycopg",
        "requests",
        "socket",
        "sqlite3",
        "sqlalchemy",
        "urllib",
    }
    violations: list[tuple[str, str]] = []
    for path in Path("src/market_research/builtin_strategies").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [item.name for item in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for name in names:
                if name.split(".", 1)[0] in forbidden_roots:
                    violations.append((str(path), name))
    assert violations == []
