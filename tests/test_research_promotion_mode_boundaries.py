from __future__ import annotations

from pathlib import Path

from bithumb_bot.research.execution_planning import _execution_plan_evidence, _research_execution_plan_bundle


def test_research_exploratory_allows_legacy_event_first_diagnostic_fallback() -> None:
    bundle = _research_execution_plan_bundle(
        side="BUY",
        cash=1_000_000.0,
        buy_fraction=0.5,
        sellable_qty=0.0,
        reference_price=10.0,
        policy_decision=None,
        candle_ts=123,
        allow_compatibility_fallback=True,
        promotion_grade_required=False,
    )
    evidence = _execution_plan_evidence(bundle)

    assert evidence["compatibility_fallback"] is True
    assert evidence["promotion_grade"] is False
    assert evidence["artifact_grade"] == "diagnostic_only"
    assert evidence["recommended_next_action"] == "regenerate_research_decisions_with_typed_execution_submit_plan"


def test_promotion_mode_rejects_execution_compatibility_fallback() -> None:
    bundle = _research_execution_plan_bundle(
        side="BUY",
        cash=1_000_000.0,
        buy_fraction=0.5,
        sellable_qty=0.0,
        reference_price=10.0,
        policy_decision=None,
        candle_ts=123,
        allow_compatibility_fallback=True,
        promotion_grade_required=True,
    )

    assert bundle.submit_plan is None
    assert bundle.compatibility_fallback is False
    assert bundle.promotion_grade is False
    assert bundle.reason_code == "promotion_requires_typed_execution_submit_plan"


def test_promotion_mode_rejects_missing_strategy_decision_v2() -> None:
    source = Path("src/bithumb_bot/research/strategy_evaluator_stage.py").read_text(encoding="utf-8")

    assert "if promotion_grade_policy_required and policy_decision is None" in source
    assert "research_policy_decision_missing_not_comparable" in source


def test_promotion_mode_rejects_strategy_decision_missing_policy_hashes() -> None:
    source = Path("src/bithumb_bot/research/strategy_evaluator_stage.py").read_text(encoding="utf-8")

    for field in (
        "policy_hash",
        "policy_contract_hash",
        "policy_input_hash",
        "policy_decision_hash",
    ):
        assert field in source
    assert "research_strategy_decision_promotion_fields_missing" in source


def test_research_exploratory_fallback_sets_recommended_next_action() -> None:
    bundle = _research_execution_plan_bundle(
        side="SELL",
        cash=1_000_000.0,
        buy_fraction=0.5,
        sellable_qty=0.25,
        reference_price=10.0,
        policy_decision=None,
        candle_ts=123,
        allow_compatibility_fallback=True,
        promotion_grade_required=False,
    )

    assert bundle.promotion_grade is False
    assert bundle.recommended_next_action != "none"
