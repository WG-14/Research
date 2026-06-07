from __future__ import annotations

import ast
from pathlib import Path

from bithumb_bot.research.execution_planning import _execution_plan_evidence, _research_execution_plan_bundle


def _function_defs(path: str) -> set[str]:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    return {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}


def test_research_execution_fallback_lives_only_in_compatibility_module() -> None:
    assert "_research_execution_submit_plan" not in _function_defs(
        "src/bithumb_bot/research/execution_planning.py"
    )
    assert "_research_execution_submit_plan" in _function_defs(
        "src/bithumb_bot/research/compatibility_execution_planning.py"
    )


def test_promotion_grade_research_planning_does_not_top_level_import_compatibility_fallback() -> None:
    tree = ast.parse(Path("src/bithumb_bot/research/execution_planning.py").read_text(encoding="utf-8"))
    top_level_imports = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]

    assert all(
        "compatibility_execution_planning" not in ast.unparse(node)
        for node in top_level_imports
    )


def test_live_production_modules_do_not_import_research_compatibility_execution_module() -> None:
    production_files = (
        "src/bithumb_bot/execution_service.py",
        "src/bithumb_bot/broker/live.py",
        "src/bithumb_bot/run_loop_execution_planner.py",
    )
    forbidden = (
        "compatibility_execution_planning",
        "_research_execution_submit_plan",
        "research_compatibility_execution_intent",
    )
    violations = [
        path
        for path in production_files
        for token in forbidden
        if token in Path(path).read_text(encoding="utf-8")
    ]

    assert violations == []


def test_fallback_generation_conditions_are_explicit_and_diagnostic_only() -> None:
    disabled = _research_execution_plan_bundle(
        side="BUY",
        cash=1_000_000.0,
        buy_fraction=1.0,
        sellable_qty=0.0,
        reference_price=10.0,
        policy_decision=None,
        candle_ts=123,
        allow_compatibility_fallback=False,
        promotion_grade_required=False,
    )
    assert disabled.submit_plan is None
    assert disabled.reason_code == "research_compatibility_submit_plan_disabled"

    promotion_blocked = _research_execution_plan_bundle(
        side="BUY",
        cash=1_000_000.0,
        buy_fraction=1.0,
        sellable_qty=0.0,
        reference_price=10.0,
        policy_decision=None,
        candle_ts=123,
        allow_compatibility_fallback=True,
        promotion_grade_required=True,
    )
    assert promotion_blocked.submit_plan is None
    assert promotion_blocked.reason_code == "promotion_requires_typed_execution_submit_plan"

    fallback = _research_execution_plan_bundle(
        side="BUY",
        cash=1_000_000.0,
        buy_fraction=1.0,
        sellable_qty=0.0,
        reference_price=10.0,
        policy_decision=None,
        candle_ts=123,
        allow_compatibility_fallback=True,
        promotion_grade_required=False,
    )
    evidence = _execution_plan_evidence(fallback)

    assert fallback.submit_plan is not None
    assert evidence["artifact_grade"] == "diagnostic_only"
    assert evidence["promotion_grade"] is False
    assert evidence["live_authoritative"] is False
    assert fallback.submit_plan.as_dict()["promotion_grade"] is False
