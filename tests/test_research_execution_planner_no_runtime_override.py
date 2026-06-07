from __future__ import annotations

from pathlib import Path

from bithumb_bot.core.sma_policy import EntryExecutionIntent, PositionSnapshot, StrategyDecisionV2
from bithumb_bot.execution_service import ExecutionSubmitPlan
from bithumb_bot.research import backtest_pipeline
from bithumb_bot.research.execution_planner_stage import DefaultExecutionPlanner, ExecutionPlanningRequest


class _Candle:
    ts = 1_700_000_000_000
    close = 100.0


class _Ledger:
    cash = 1_000_000.0


def _typed_decision() -> StrategyDecisionV2:
    return StrategyDecisionV2(
        strategy_name="sma_with_filter",
        raw_signal="BUY",
        raw_reason="typed_raw",
        entry_signal="BUY",
        entry_reason="typed_entry",
        exit_signal="HOLD",
        exit_reason="no_exit",
        final_signal="BUY",
        final_reason="typed_final",
        blocked_filters=(),
        entry_blocked=False,
        entry_block_reason=None,
        exit_rule=None,
        exit_evaluations=(),
        protective_exit_overrode_entry=False,
        exit_filter_suppression_prevented=False,
        position_snapshot=PositionSnapshot(in_position=False, entry_allowed=True, exit_allowed=False),
        execution_intent=EntryExecutionIntent(
            side="BUY",
            intent="enter",
            pair="KRW-BTC",
            requires_execution_sizing=True,
            budget_fraction_of_cash=0.5,
            max_budget_krw=100_000.0,
        ),
        entry_decision=object(),  # type: ignore[arg-type]
        trace={},
        policy_hash="sha256:pure",
        policy_contract_hash="sha256:contract",
        policy_input_hash="sha256:input",
        policy_decision_hash="sha256:decision",
    )


def test_default_execution_planner_uses_default_bundle_builder_in_promotion_mode(monkeypatch) -> None:
    def _forbidden_override(**_kwargs):
        raise AssertionError("backtest_pipeline override used")

    monkeypatch.setattr(backtest_pipeline, "_research_execution_plan_bundle", _forbidden_override)

    result = DefaultExecutionPlanner().plan(
        ExecutionPlanningRequest(
            candle=_Candle(),
            event=object(),
            ledger=_Ledger(),
            strategy_name="sma_with_filter",
            action="BUY",
            decision_reason="unit_buy",
            sellable_qty=0.0,
            buy_fraction=0.99,
            promotion_grade_policy_required=True,
            allow_execution_compatibility_fallback=False,
            policy_drives_execution=True,
            policy_decision=_typed_decision(),
        )
    )

    assert isinstance(result.plan_bundle.submit_plan, ExecutionSubmitPlan)
    assert result.evidence["promotion_grade"] is True
    assert result.evidence.get("planner_override_used") is not True


def test_backtest_pipeline_override_is_not_used_for_promotion_grade_planning() -> None:
    source = Path("src/bithumb_bot/research/execution_planner_stage.py").read_text(encoding="utf-8")

    assert "_research_test_compat_attr" not in source
    assert "_default_research_execution_plan_bundle(" in source


def test_execution_simulator_override_usage_is_recorded_when_allowed() -> None:
    source = Path("src/bithumb_bot/research/execution_simulator_stage.py").read_text(encoding="utf-8")

    assert "planner_override_used" in source
    assert "override_source" in source
    assert "if promotion_grade_policy_required" in source
