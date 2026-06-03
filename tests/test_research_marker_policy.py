from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.factories.research_reports import assert_fast_research_workload, minimal_research_report


EXPENSIVE_RESEARCH_MARKERS = {
    "research_e2e",
    "audit_e2e",
    "walk_forward_e2e",
    "parallel_e2e",
    "slow_research",
    "nightly",
}

PRODUCTION_RESEARCH_ENTRYPOINTS = {
    "run_research_backtest",
    "run_research_walk_forward",
}


def _decorator_marker_names(node: ast.FunctionDef) -> set[str]:
    markers: set[str] = set()
    for decorator in node.decorator_list:
        current = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(current, ast.Attribute):
            markers.add(current.attr)
    return markers


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _uses_injected_evaluator(node: ast.Call) -> bool:
    return any(keyword.arg == "candidate_evaluator" for keyword in node.keywords)


def test_direct_production_research_entrypoints_have_expensive_markers() -> None:
    violations: list[str] = []
    for path in sorted(Path("tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            direct_calls = [
                call
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and _call_name(call) in PRODUCTION_RESEARCH_ENTRYPOINTS
                and not _uses_injected_evaluator(call)
            ]
            if not direct_calls:
                continue
            markers = _decorator_marker_names(node)
            if markers.isdisjoint(EXPENSIVE_RESEARCH_MARKERS):
                violations.append(f"{path}:{node.lineno}:{node.name}")

    assert violations == []


def test_fast_research_workload_budget_rejects_large_strategy_run_count() -> None:
    report = minimal_research_report()
    report["workload_estimate"]["estimated_strategy_runs"] = 4

    with pytest.raises(AssertionError):
        assert_fast_research_workload(report)


def test_fast_research_workload_budget_rejects_tick_and_matrix_growth() -> None:
    report = minimal_research_report()
    report["workload_estimate"].update(
        {
            "candidate_count": 2,
            "scenario_count": 2,
            "split_count": 2,
            "estimated_strategy_runs": 2,
            "estimated_tick_events": 10_001,
        }
    )

    with pytest.raises(AssertionError):
        assert_fast_research_workload(report)


def test_fast_research_workload_budget_rejects_walk_forward_and_complete_external_audit() -> None:
    walk_forward_report = minimal_research_report()
    walk_forward_report["workload_estimate"]["walk_forward_window_count"] = 1
    with pytest.raises(AssertionError):
        assert_fast_research_workload(walk_forward_report)

    audit_report = minimal_research_report()
    audit_report["workload_estimate"]["audit_mode"] = "complete_external"
    with pytest.raises(AssertionError):
        assert_fast_research_workload(audit_report)
