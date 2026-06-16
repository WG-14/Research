from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = REPO_ROOT / "src" / "bithumb_bot" / "research"
RESEARCH_CLI = REPO_ROOT / "src" / "bithumb_bot" / "cli" / "commands" / "research.py"
WORKLOAD_BUDGET_SCRIPT = REPO_ROOT / "scripts" / "check_research_workload_budget.py"
PROCESSOR_CONSTANT_NAMES = {"max_workers", "processors"}
MEMORY_CONSTANT_NAMES = {"memory", "memory_mb", "max_total_memory_mb"}
MEMORY_HARDCODE_VALUES = {12288}


def _research_python_files() -> list[Path]:
    return sorted(RESEARCH_ROOT.rglob("*.py"))


def _policy_scan_files() -> list[Path]:
    return [*_research_python_files(), RESEARCH_CLI, WORKLOAD_BUDGET_SCRIPT]


def test_no_hardcoded_wsl_worker_count() -> None:
    offenders: list[str] = []
    for path in _policy_scan_files():
        text = path.read_text(encoding="utf-8")
        if "max_workers = 8" in text or "max_workers: int = 8" in text or "processors = 8" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
        if "12 * 1024" in text and path.name != "resource_planner.py":
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []


def test_policy_scans_research_cli_and_workload_budget_script() -> None:
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in _policy_scan_files()}

    assert "src/bithumb_bot/cli/commands/research.py" in scanned
    assert "scripts/check_research_workload_budget.py" in scanned


def test_no_hardcoded_memory_budget_variants() -> None:
    offenders: list[str] = []
    for path in _policy_scan_files():
        source = path.read_text(encoding="utf-8")
        compact = source.replace(" ", "")
        if "12*1024" in compact:
            offenders.append(str(path.relative_to(REPO_ROOT)))
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if _assigned_constant_name(node) in MEMORY_CONSTANT_NAMES and _constant_int_value(node) in MEMORY_HARDCODE_VALUES:
                offenders.append(str(path.relative_to(REPO_ROOT)))
                break

    assert offenders == []


def test_no_hardcoded_processor_count_ast_variants() -> None:
    offenders: list[str] = []
    for path in _policy_scan_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if _assigned_constant_name(node) in PROCESSOR_CONSTANT_NAMES and _constant_int_value(node) == 8:
                offenders.append(str(path.relative_to(REPO_ROOT)))
                break

    assert offenders == []


def test_research_execution_policy_defaults_do_not_force_eight_workers() -> None:
    source = (RESEARCH_ROOT / "experiment_manifest.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    defaults: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ResearchExecutionPolicy":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if isinstance(stmt.value, ast.Constant):
                        defaults[stmt.target.id] = stmt.value.value
    assert defaults["max_workers"] == 1


def test_candidate_scenario_split_not_unconditional_default() -> None:
    source = (RESEARCH_ROOT / "experiment_manifest.py").read_text(encoding="utf-8")
    assert 'work_unit: str = "candidate_scenario"' in source
    assert 'work_unit: str = "candidate_scenario_split"' not in source


def test_memory_retention_defaults_not_increased_for_cpu_utilization() -> None:
    source = (RESEARCH_ROOT / "experiment_manifest.py").read_text(encoding="utf-8")
    assert "max_decisions_retained: int | None = 0" in source
    assert "max_equity_points_retained: int | None = 0" in source


def _assigned_constant_name(node: ast.AST) -> str | None:
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
    if isinstance(value, ast.Constant) and isinstance(value.value, int):
        return int(value.value)
    return None
