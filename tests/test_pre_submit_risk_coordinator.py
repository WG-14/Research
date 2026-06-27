from __future__ import annotations

from tests.test_submit_authority_policy import _approved, _plan

from bithumb_bot.submit_authority_policy import is_pre_submit_risk_approved_for_plan


def test_pre_submit_risk_integrity_passes_for_reduce_only_sell() -> None:
    plan = _plan(side="SELL", extra={"target_delta_qty": -0.00109271})
    payload = _approved(
        plan,
        status="REDUCE_ONLY",
        reason_code="POSITION_LOSS_LIMIT",
        allowed_actions=["SELL", "HOLD"],
    ).as_dict()

    approval = is_pre_submit_risk_approved_for_plan(
        payload,
        expected_submit_plan_hash=plan.content_hash(),
    )

    assert approval.approved is True
    assert approval.integrity_valid is True
    assert approval.action_authorized is True


def test_pre_submit_risk_action_authorization_rejects_reduce_only_buy() -> None:
    plan = _plan(side="BUY", extra={"target_delta_qty": 0.00109271})
    payload = _approved(
        plan,
        status="REDUCE_ONLY",
        reason_code="POSITION_LOSS_LIMIT",
        allowed_actions=["SELL", "HOLD"],
    ).as_dict()

    approval = is_pre_submit_risk_approved_for_plan(
        payload,
        expected_submit_plan_hash=plan.content_hash(),
    )

    assert approval.approved is False
    assert approval.integrity_valid is True
    assert approval.action_authorized is False
    assert approval.reason == "live_real_order_pre_submit_risk_reduce_only_not_authorized_for_plan"


def test_pre_submit_risk_plan_hash_mismatch_blocks_before_action_authorization() -> None:
    plan = _plan(side="SELL", extra={"target_delta_qty": -0.00109271})
    payload = _approved(
        plan,
        status="REDUCE_ONLY",
        reason_code="POSITION_LOSS_LIMIT",
        allowed_actions=["SELL", "HOLD"],
        plan_hash_override="sha256:" + "9" * 64,
    ).as_dict()

    approval = is_pre_submit_risk_approved_for_plan(
        payload,
        expected_submit_plan_hash=plan.content_hash(),
    )

    assert approval.approved is False
    assert approval.integrity_valid is False
    assert approval.action_authorized is False
    assert approval.reason == "live_real_order_pre_submit_risk_plan_hash_mismatch"
