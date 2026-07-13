import ast
import json
from pathlib import Path


def test_core_has_no_concrete_strategy_imports():
    for name in ("simulation_engine.py", "validation_protocol.py"):
        tree = ast.parse((Path("src/market_research/research") / name).read_text())
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any("strategies" in module for module in imports)


def test_boundary_manifest_classifies_four_roles():
    payload = json.loads(Path("docs/architecture-boundaries.json").read_text())
    assert {"research_core", "strategy_sdk", "builtin_strategies", "composition_root"} <= set(payload)
