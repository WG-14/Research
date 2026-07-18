import ast
import json
import pytest
from pathlib import Path


def _declared_files(payload, role):
    result = set()
    for raw in payload[role]:
        path = Path(raw)
        result.update(path.rglob("*.py") if path.is_dir() else (path,))
    return result


def test_core_has_no_concrete_strategy_imports():
    for name in ("simulation_engine.py", "validation_protocol.py"):
        tree = ast.parse((Path("src/market_research/research") / name).read_text())
        imports = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        assert not any("strategies" in module for module in imports)


def test_boundary_manifest_classifies_four_roles():
    payload = json.loads(Path("docs/architecture-boundaries.json").read_text())
    assert {
        "research_core",
        "strategy_sdk",
        "builtin_strategies",
        "composition_root",
    } <= set(payload)


def test_composition_imports_builtin_package_ownership_surfaces():
    source = Path(
        "src/market_research/research_composition/builtin_registry.py"
    ).read_text()
    assert "market_research.builtin_strategies" in source
    assert "market_research.research.strategies" not in source
    assert "build_sma_with_filter_plugin" not in source
    assert "build_buy_and_hold_baseline_plugin" not in source


def test_architecture_manifest_paths_are_all_scanned_and_strategy_files_classified():
    payload = json.loads(Path("docs/architecture-boundaries.json").read_text())
    roles = (
        "research_core",
        "strategy_sdk",
        "builtin_strategies",
        "compatibility_strategy_event_adapters",
        "compatibility_strategy_adapters",
        "composition_root",
    )
    classified = set().union(*(_declared_files(payload, role) for role in roles))
    expected = set().union(
        *(
            set(Path(root).rglob("*.py"))
            for root in (
                "src/market_research/research_core",
                "src/market_research/strategy_sdk",
                "src/market_research/builtin_strategies",
                "src/market_research/research/strategies",
                "src/market_research/research_composition",
            )
        )
    )
    assert expected - classified == set()
    assert all(path.exists() for path in classified)


def test_all_declared_core_and_sdk_files_obey_dependency_direction():
    payload = json.loads(Path("docs/architecture-boundaries.json").read_text())
    violations = []
    for role in ("research_core", "strategy_sdk"):
        for path in _declared_files(payload, role):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                values = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [item.name for item in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                if any(
                    "builtin_strategies" in value or "research_composition" in value
                    for value in values
                ):
                    violations.append((str(path), values))
    assert violations == []


def test_core_and_compatibility_modules_do_not_import_composition():
    payload = json.loads(Path("docs/architecture-boundaries.json").read_text())
    violations = []
    for role in (
        "research_core",
        "strategy_sdk",
        "compatibility_strategy_event_adapters",
        "compatibility_strategy_adapters",
    ):
        for path in _declared_files(payload, role):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                values = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [item.name for item in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                if any("research_composition" in value for value in values):
                    violations.append((str(path), values))
    assert violations == []


def test_only_composition_imports_concrete_plugin_factories():
    violations = []
    for path in Path("src/market_research").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and "builtin_strategies" in (node.module or "")
                and any(
                    alias.name.startswith("build_") and alias.name.endswith("_plugin")
                    for alias in node.names
                )
                and "research_composition" not in path.parts
            ):
                violations.append(str(path))
    assert violations == []


def test_builtin_strategy_discovery_uses_explicit_markers_in_stable_order(monkeypatch):
    from types import SimpleNamespace
    import market_research.builtin_strategies as builtins

    def first():
        return "first"

    def second():
        return "second"

    modules = {
        "fixture.alpha": SimpleNamespace(STRATEGY_PLUGIN_FACTORY=first),
        "fixture.helper": SimpleNamespace(),
        "fixture.zeta": SimpleNamespace(STRATEGY_PLUGIN_FACTORY=second),
    }
    monkeypatch.setattr(
        builtins,
        "iter_modules",
        lambda *_args, **_kwargs: [
            SimpleNamespace(name=name) for name in reversed(modules)
        ],
    )
    monkeypatch.setattr(builtins, "import_module", modules.__getitem__)

    assert builtins.discover_builtin_strategy_factories() == (first, second)


def test_platform_documentation_matches_strategy_discovery_and_partial_exit_contracts():
    documentation = Path("docs/investment-research-platform.md").read_text(
        encoding="utf-8"
    )

    assert "stable marker discovery" in documentation
    assert "directory scanning were not selected" not in documentation
    assert "opt-in partial exits" in documentation
    assert "no partial exits" not in documentation
    assert "selection artifact schema 2" in documentation
    assert "Reproduction receipt schema 9" in documentation
    assert "result-affecting environment" in documentation
    assert "same backtest or walk-forward path" in documentation
    assert "`quote_ts` and `quote_available_at_ts`" in documentation
    assert "portfolio effective time cannot precede" in documentation


def test_generic_strategy_spec_rejects_legacy_concrete_spec_exports():
    source = Path("src/market_research/research/strategy_spec.py").read_text()
    names = (
        "SMA_WITH_FILTER_SPEC",
        "BUY_AND_HOLD_BASELINE_SPEC",
        "NOOP_BASELINE_SPEC",
        "THRESHOLD_RESEARCH_ONLY_SPEC",
    )
    assert all(name not in source for name in names)
    import market_research.research.strategy_spec as generic

    for name in names:
        with pytest.raises(AttributeError):
            getattr(generic, name)


def test_sdk_strategy_spec_requires_explicit_registry_and_has_no_catalog_import():
    source = Path("src/market_research/research/strategy_spec.py").read_text()
    assert "strategy_catalog" not in source
    from market_research.research.strategy_spec import (
        StrategySpecError,
        strategy_spec_for_name,
    )

    with pytest.raises(StrategySpecError, match="explicit strategy registry required"):
        strategy_spec_for_name("noop_baseline")


def test_compatibility_exit_adapter_has_no_concrete_strategy_import():
    source = Path("src/market_research/research/exit_rules.py").read_text()
    assert "builtin_strategies" not in source


def test_retired_exit_rule_api_has_no_runtime_references():
    retired = Path("src/market_research/research/exit_rules.py")
    violations = []
    for path in Path("src/market_research").rglob("*.py"):
        if path == retired:
            continue
        source = path.read_text(encoding="utf-8")
        if any(
            marker in source
            for marker in (
                "create_exit_rules",
                "research.exit_rules",
                "strategy.exit_rules",
            )
        ):
            violations.append(str(path))
    assert violations == []
