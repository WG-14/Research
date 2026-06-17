from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = REPO_ROOT / "src" / "bithumb_bot" / "research"
RESEARCH_CLI = REPO_ROOT / "src" / "bithumb_bot" / "cli" / "commands" / "research.py"
WORKLOAD_BUDGET_SCRIPT = REPO_ROOT / "scripts" / "check_research_workload_budget.py"


def _production_scan_files() -> list[Path]:
    return sorted(RESEARCH_ROOT.rglob("*.py")) + [RESEARCH_CLI, WORKLOAD_BUDGET_SCRIPT]


def test_no_hardcoded_auto_execution_worker_count() -> None:
    offenders: list[str] = []
    for path in _production_scan_files():
        source = path.read_text(encoding="utf-8")
        if "max_workers = 8" in source or "max_workers: int = 8" in source:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if _assigned_name(node) == "max_workers" and _constant_int_value(node) == 8:
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
                break

    assert offenders == []


def test_research_execution_policy_defaults_not_changed_to_parallel() -> None:
    source = (RESEARCH_ROOT / "experiment_manifest.py").read_text(encoding="utf-8")
    defaults = _research_execution_policy_defaults(source)

    assert defaults["mode"] == "serial"
    assert defaults["max_workers"] == 1


def test_runner_canonicalization_does_not_insert_user_explicit_execution_block() -> None:
    source = (RESEARCH_ROOT / "validation_protocol.py").read_text(encoding="utf-8")
    function = _function_source(source, "_canonicalize_runner_default_execution")

    assert '["execution"] =' not in function
    assert "research_run_copy[\"execution\"]" not in function
    assert ".setdefault(\"execution\"" not in function
    assert ".setdefault('execution'" not in function


def test_no_wsl_memory_hardcode_in_research_execution_policy() -> None:
    offenders: list[str] = []
    for path in sorted(RESEARCH_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        compact = source.replace(" ", "")
        if "12*1024" in compact:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if _constant_int_value(node) in {11961, 12288} or _is_12_times_1024(node):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
                break

    assert offenders == []


def _research_execution_policy_defaults(source: str) -> dict[str, object]:
    tree = ast.parse(source)
    defaults: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ResearchExecutionPolicy":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if isinstance(stmt.value, ast.Constant):
                        defaults[stmt.target.id] = stmt.value.value
    return defaults


def _function_source(source: str, function_name: str) -> str:
    tree = ast.parse(source)
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"function not found: {function_name}")


def _assigned_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                return target.id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def _constant_int_value(node: ast.AST) -> int | None:
    value: ast.AST | None = None
    if isinstance(node, ast.Assign):
        value = node.value
    elif isinstance(node, ast.AnnAssign):
        value = node.value
    elif isinstance(node, ast.Constant):
        value = node
    if isinstance(value, ast.Constant) and isinstance(value.value, int):
        return int(value.value)
    return None


def _is_12_times_1024(node: ast.AST) -> bool:
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
        return False
    left = node.left
    right = node.right
    return (
        isinstance(left, ast.Constant)
        and isinstance(right, ast.Constant)
        and {left.value, right.value} == {12, 1024}
    )
