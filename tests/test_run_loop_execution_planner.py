from __future__ import annotations

from dataclasses import dataclass

from bithumb_bot.execution_service import ExecutionDecisionSummary, ExecutionSubmitPlan
from bithumb_bot.run_loop_execution_planner import ExecutionPlanner


@dataclass(frozen=True)
class _Readiness:
    payload: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return dict(self.payload)


def _summary(*, target_plan: ExecutionSubmitPlan | None = None) -> ExecutionDecisionSummary:
    return ExecutionDecisionSummary(
        raw_signal="BUY" if target_plan is not None else "HOLD",
        final_signal="BUY" if target_plan is not None else "HOLD",
        final_action="REBALANCE_TO_TARGET" if target_plan is not None else "STRATEGY_HOLD",
        submit_expected=target_plan is not None and target_plan.submit_expected,
        pre_submit_proof_status="passed" if target_plan is not None else "not_required",
        block_reason="none" if target_plan is not None else "raw_hold_no_entry_or_exit_signal",
        strategy_sell_candidate=None,
        residual_sell_candidate=None,
        target_exposure_krw=100_000.0 if target_plan is not None else None,
        current_effective_exposure_krw=0.0 if target_plan is not None else None,
        tracked_residual_exposure_krw=None,
        buy_delta_krw=100_000.0 if target_plan is not None else None,
        residual_live_sell_mode="telemetry",
        residual_buy_sizing_mode="telemetry",
        residual_submit_plan=None,
        buy_submit_plan=None,
        target_shadow_decision={"target_policy_action": "use_existing_target"} if target_plan else None,
        target_submit_plan=target_plan,
    )


def test_run_loop_execution_planner_materializes_context_with_policy_hashes() -> None:
    plan = ExecutionSubmitPlan(
        side="BUY",
        source="target_delta",
        authority="canonical_target_delta_sizing",
        final_action="REBALANCE_TO_TARGET",
        qty=0.001,
        notional_krw=100_000.0,
        target_exposure_krw=100_000.0,
        current_effective_exposure_krw=0.0,
        delta_krw=100_000.0,
        submit_expected=True,
        pre_submit_proof_status="passed",
        block_reason="none",
        idempotency_key="target-plan-key",
    )
    planner = ExecutionPlanner(
        readiness_snapshot_builder=lambda _conn: _Readiness(
            {
                "residual_inventory_state": "none",
                "policy_input_hash": "sha256:policy-input",
            }
        ),
        target_state_resolver=lambda *_args, **_kwargs: {
            "previous_target_exposure_krw": 0.0,
            "target_policy_metadata": {"target_origin": "runtime_state"},
        },
        summary_builder=lambda **_kwargs: _summary(target_plan=plan),
    )

    result = planner.plan_strategy_decision(
        object(),
        decision_context={
            "strategy": "sma_with_filter",
            "policy_input_hash": "sha256:policy-input",
            "policy_decision_hash": "sha256:policy-decision",
        },
        signal="BUY",
        reason="cross_up",
        updated_ts=123,
    )

    assert result.planning_error is None
    assert result.execution_decision_summary is not None
    assert result.execution_decision["target_submit_plan"]["source"] == "target_delta"  # type: ignore[index]
    assert result.context["execution_decision"] == result.execution_decision
    assert result.context["policy_input_hash"] == "sha256:policy-input"
    assert result.context["policy_decision_hash"] == "sha256:policy-decision"
    assert result.context["target_origin"] == "runtime_state"


def test_run_loop_execution_planner_failure_returns_block_recovery_payload() -> None:
    planner = ExecutionPlanner(
        readiness_snapshot_builder=lambda _conn: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = planner.plan_strategy_decision(
        object(),
        decision_context={"strategy": "sma_with_filter"},
        signal="BUY",
        reason="cross_up",
        updated_ts=123,
    )

    assert result.execution_decision_summary is None
    assert result.execution_decision["final_action"] == "BLOCK_RECOVERY"
    assert result.execution_decision["submit_expected"] is False
    assert result.execution_decision["pre_submit_proof_status"] == "failed"
    assert result.execution_decision["block_reason"] == "execution_decision_unavailable:RuntimeError"
    assert result.context["execution_decision"] == result.execution_decision
