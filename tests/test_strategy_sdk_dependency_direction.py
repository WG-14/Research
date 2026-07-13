import ast
from pathlib import Path


def _imports(root: Path):
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                yield path, node.module or ""
            elif isinstance(node, ast.Import):
                for name in node.names:
                    yield path, name.name


def test_all_sdk_modules_forbid_builtin_and_composition_imports():
    forbidden = ("builtin_strategies", "research_composition")
    violations = [(path, module) for path, module in _imports(Path("src/market_research/strategy_sdk"))
                  if any(name in module for name in forbidden)]
    assert violations == []


def test_local_and_nested_imports_are_checked():
    source = "def f():\n    from market_research.builtin_strategies import x\n"
    tree = ast.parse(source)
    assert any(isinstance(node, ast.ImportFrom) and "builtin_strategies" in (node.module or "")
               for node in ast.walk(tree))
