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
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any("strategies" in module for module in imports)


def test_boundary_manifest_classifies_four_roles():
    payload = json.loads(Path("docs/architecture-boundaries.json").read_text())
    assert {"research_core", "strategy_sdk", "builtin_strategies", "composition_root"} <= set(payload)


def test_composition_imports_builtin_package_ownership_surfaces():
    source = Path("src/market_research/research_composition/builtin_registry.py").read_text()
    assert "market_research.builtin_strategies" in source
    assert "market_research.research.strategies" not in source


def test_architecture_manifest_paths_are_all_scanned_and_strategy_files_classified():
    payload = json.loads(Path("docs/architecture-boundaries.json").read_text())
    roles = ("research_core", "strategy_sdk", "builtin_strategies",
             "compatibility_strategy_event_adapters", "composition_root")
    classified = set().union(*(_declared_files(payload, role) for role in roles))
    expected = set().union(*(set(Path(root).rglob("*.py")) for root in (
        "src/market_research/research_core", "src/market_research/strategy_sdk",
        "src/market_research/builtin_strategies", "src/market_research/research/strategies",
        "src/market_research/research_composition")))
    assert expected - classified == set()
    assert all(path.exists() for path in classified)


def test_all_declared_core_and_sdk_files_obey_dependency_direction():
    payload = json.loads(Path("docs/architecture-boundaries.json").read_text())
    violations = []
    for role in ("research_core", "strategy_sdk"):
        for path in _declared_files(payload, role):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                values = ([node.module or ""] if isinstance(node, ast.ImportFrom)
                          else [item.name for item in node.names] if isinstance(node, ast.Import) else [])
                if any("builtin_strategies" in value or "research_composition" in value for value in values):
                    violations.append((str(path), values))
    assert violations == []


def test_only_composition_imports_concrete_plugin_factories():
    violations = []
    for path in Path("src/market_research").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and "builtin_strategies" in (node.module or "")
                    and any(alias.name.startswith("build_") and alias.name.endswith("_plugin") for alias in node.names)
                    and "research_composition" not in path.parts):
                violations.append(str(path))
    assert violations == []


def test_generic_strategy_spec_rejects_legacy_concrete_spec_exports():
    source = Path("src/market_research/research/strategy_spec.py").read_text()
    names = ("SMA_WITH_FILTER_SPEC", "BUY_AND_HOLD_BASELINE_SPEC",
             "NOOP_BASELINE_SPEC", "THRESHOLD_RESEARCH_ONLY_SPEC")
    assert all(name not in source for name in names)
    import market_research.research.strategy_spec as generic
    for name in names:
        with pytest.raises(AttributeError):
            getattr(generic, name)
