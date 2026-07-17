import ast
from pathlib import Path


def test_all_core_modules_forbid_builtin_and_composition_imports():
    violations = []
    paths = list(Path("src/market_research/research_core").rglob("*.py")) + [
        Path("src/market_research/research/simulation_engine.py"),
        Path("src/market_research/research/portfolio_ledger.py"),
    ]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = node.module if isinstance(node, ast.ImportFrom) else None
            names = (
                [item.name for item in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            values = [module or "", *names]
            if any(
                "builtin_strategies" in value or "research_composition" in value
                for value in values
            ):
                violations.append((path, values))
    assert violations == []


def test_core_imports_without_builtin_registry():
    import market_research.research_core as core

    assert callable(core.run_common_simulation_backtest)
