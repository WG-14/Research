from __future__ import annotations

from dataclasses import fields, replace

import pytest

from bithumb_bot.config import settings
from bithumb_bot.execution_service import (
    ExecutionDecisionSummary,
    ExecutionObservabilityPayload,
    ExecutionSubmitPlan,
    LiveSignalExecutionService,
    TypedExecutionRequest,
)
from bithumb_bot.submit_authority_policy import evaluate_submit_authority_policy


@pytest.fixture(autouse=True)
def _restore_settings():
    old_values = {field.name: getattr(settings, field.name) for field in fields(type(settings))}
    yield
    for key, value in old_values.items():
        object.__setattr__(settings, key, value)


class _Broker:
    pass


def _settings(*, mode: str, dry_run: bool, armed: bool, engine: str = "target_delta"):
    return type(
        "Settings",
        (),
        {
            "MODE": mode,
            "LIVE_DRY_RUN": dry_run,
            "LIVE_REAL_ORDER_ARMED": armed,
            "EXECUTION_ENGINE": engine,
            "RESIDUAL_LIVE_SELL_MODE": "enabled",
        },
    )()


def _plan(
    *,
    side: str = "BUY",
    source: str = "target_delta",
    authority: str = "canonical_target_delta_sizing",
    submit_expected: bool = True,
    proof: str = "passed",
    extra: dict[str, object] | None = None,
) -> ExecutionSubmitPlan:
    return ExecutionSubmitPlan(
        side=side,
        source=source,
        authority=authority,
        final_action="REBALANCE_TO_TARGET" if source == "target_delta" else "ENTER_STRATEGY_POSITION",
        qty=0.001,
        notional_krw=100_000.0,
        target_exposure_krw=100_000.0,
        current_effective_exposure_krw=0.0,
        delta_krw=100_000.0,
        submit_expected=submit_expected,
        pre_submit_proof_status=proof,
        block_reason="none" if submit_expected else "blocked",
        idempotency_key="unit-key",
        extra_payload={
            "portfolio_target_authoritative": True,
            "portfolio_target_hash": "sha256:portfolio-target",
            "allocation_decision_hash": "sha256:allocation",
            "strategy_contribution_hash": "sha256:contribution",
            **dict(extra or {}),
        },
    )


def _approved(plan: ExecutionSubmitPlan) -> ExecutionSubmitPlan:
    plan_hash = plan.content_hash()
    extra = dict(plan.extra_payload)
    extra.update(
        {
            "submit_plan_hash": plan_hash,
            "pre_submit_risk_status": "ALLOW",
            "pre_submit_risk_decision_hash": "sha256:" + "1" * 64,
            "pre_submit_risk_policy_hash": "sha256:" + "2" * 64,
            "effective_pre_submit_risk_policy_hash": "sha256:" + "2" * 64,
            "pre_submit_risk_input_hash": "sha256:" + "3" * 64,
            "pre_submit_risk_evidence_hash": "sha256:" + "4" * 64,
            "pre_submit_risk_plan_hash": plan_hash,
            "pre_submit_risk_reason_code": "OK",
            "pre_submit_risk_state_source": "unit",
            "risk_policy_source": "strategy_risk_profiles",
            "pre_submit_risk_policy_composition_rule": "most_restrictive_selected_strategy_policy",
            "strategy_risk_profile_hashes": ["sha256:" + "8" * 64],
        }
    )
    return replace(plan, extra_payload=extra)


def _summary(*, target: ExecutionSubmitPlan | None = None, buy: ExecutionSubmitPlan | None = None, residual: ExecutionSubmitPlan | None = None) -> ExecutionDecisionSummary:
    plan = target or residual or buy
    return ExecutionDecisionSummary(
        raw_signal="BUY" if plan is None else plan.side,
        final_signal="BUY" if plan is None else plan.side,
        final_action="STRATEGY_HOLD" if plan is None else plan.final_action,
        submit_expected=False if plan is None else plan.submit_expected,
        pre_submit_proof_status="not_required" if plan is None else plan.pre_submit_proof_status,
        block_reason="no_plan" if plan is None else plan.block_reason,
        strategy_sell_candidate=None,
        residual_sell_candidate=None,
        target_exposure_krw=None if plan is None else plan.target_exposure_krw,
        current_effective_exposure_krw=None if plan is None else plan.current_effective_exposure_krw,
        tracked_residual_exposure_krw=None,
        buy_delta_krw=None if plan is None else plan.delta_krw,
        residual_live_sell_mode="enabled",
        residual_buy_sizing_mode="telemetry",
        residual_submit_plan=residual,
        buy_submit_plan=buy,
        target_shadow_decision=None,
        target_submit_plan=target,
    )


def test_mode_aware_submit_authority_matrix() -> None:
    target = _approved(_plan()).as_final_payload()
    legacy_buy = _plan(source="strategy_position", authority="configured_strategy_order_size")
    residual = _approved(_plan(
        side="SELL",
        source="residual_inventory",
        authority="residual_inventory_policy",
        extra={"portfolio_target_authoritative": False},
    )).as_final_payload()

    assert evaluate_submit_authority_policy(
        legacy_buy,
        settings_obj=_settings(mode="paper", dry_run=True, armed=False, engine="lot_native"),
        plan_kind="buy",
    ).allowed
    assert evaluate_submit_authority_policy(
        legacy_buy,
        settings_obj=_settings(mode="live", dry_run=True, armed=False, engine="lot_native"),
        plan_kind="buy",
    ).reason == "live_dry_run_non_submitting"
    assert evaluate_submit_authority_policy(
        target,
        settings_obj=_settings(mode="live", dry_run=False, armed=True),
        plan_kind="target",
    ).allowed
    rejected = evaluate_submit_authority_policy(
        legacy_buy,
        settings_obj=_settings(mode="live", dry_run=False, armed=True),
        plan_kind="buy",
    )
    assert rejected.allowed is False
    assert rejected.reason == "live_real_order_buy_plan_rejected_target_delta_required"
    assert evaluate_submit_authority_policy(
        residual,
        settings_obj=_settings(mode="live", dry_run=False, armed=True),
        plan_kind="residual",
    ).allowed


def test_live_real_order_requires_operational_pre_submit_risk_proof() -> None:
    missing = evaluate_submit_authority_policy(
        _plan().as_final_payload(),
        settings_obj=_settings(mode="live", dry_run=False, armed=True),
        plan_kind="target",
    )
    assert missing.allowed is False
    assert missing.reason == "live_real_order_pre_submit_risk_not_allow"
    assert missing.as_dict()["pre_submit_risk_approval_status"] == "blocked"

    approved = evaluate_submit_authority_policy(
        _approved(_plan()).as_final_payload(),
        settings_obj=_settings(mode="live", dry_run=False, armed=True),
        plan_kind="target",
    )
    assert approved.allowed is True
    assert approved.as_dict()["pre_submit_risk_approval_status"] == "approved"


def test_live_real_order_schema_valid_plan_still_requires_final_submit_payload() -> None:
    raw_typed_plan = _approved(_plan())

    rejected = evaluate_submit_authority_policy(
        raw_typed_plan,
        settings_obj=_settings(mode="live", dry_run=False, armed=True),
        plan_kind="target",
    )

    assert rejected.allowed is False
    assert rejected.reason == "live_real_order_submit_plan_missing_final_schema"


@pytest.mark.parametrize(
    ("source", "authority"),
    [
        ("research_backtest", "research_compatibility_execution_intent"),
        ("strategy_position", "strategy_execution_intent"),
        ("strategy_position", "configured_strategy_order_size"),
    ],
)
def test_live_real_order_submit_authority_matrix_rejects_legacy_buy_sources(
    source: str,
    authority: str,
) -> None:
    decision = evaluate_submit_authority_policy(
        _plan(source=source, authority=authority).as_dict(),
        settings_obj=_settings(mode="live", dry_run=False, armed=True),
        plan_kind="buy",
    )

    assert decision.allowed is False
    assert decision.reason == "live_real_order_buy_plan_rejected_target_delta_required"


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"portfolio_target_authoritative": False}, "live_real_order_target_plan_missing_authoritative_portfolio_target"),
        ({"portfolio_target_hash": ""}, "live_real_order_target_plan_missing_portfolio_target_hash"),
        ({"allocation_decision_hash": ""}, "live_real_order_target_plan_missing_allocation_decision_hash"),
        ({"strategy_contribution_hash": ""}, "live_real_order_target_plan_missing_strategy_contribution_hash"),
    ],
)
def test_live_real_order_submit_authority_matrix_accepts_target_delta_only_with_required_hashes(
    extra: dict[str, object],
    reason: str,
) -> None:
    allowed = evaluate_submit_authority_policy(
        _approved(_plan()).as_final_payload(),
        settings_obj=_settings(mode="live", dry_run=False, armed=True),
        plan_kind="target",
    )
    assert allowed.allowed is True
    assert allowed.reason == "allowed_target_delta"

    rejected = evaluate_submit_authority_policy(
        _approved(_plan(extra=extra)).as_final_payload(),
        settings_obj=_settings(mode="live", dry_run=False, armed=True),
        plan_kind="target",
    )
    assert rejected.allowed is False
    assert rejected.reason == reason


@pytest.mark.parametrize(
    ("side", "source", "authority", "residual_mode", "reason"),
    [
        ("SELL", "residual_inventory", "residual_inventory_policy", "enabled", "allowed_residual_inventory_policy"),
        ("BUY", "residual_inventory", "residual_inventory_policy", "enabled", "live_real_order_residual_plan_invalid_side"),
        ("SELL", "target_delta", "residual_inventory_policy", "enabled", "live_real_order_residual_plan_invalid_source"),
        ("SELL", "residual_inventory", "strategy_execution_intent", "enabled", "live_real_order_residual_plan_invalid_authority"),
        ("SELL", "residual_inventory", "residual_inventory_policy", "telemetry", "live_real_order_residual_policy_not_enabled"),
    ],
)
def test_live_real_order_submit_authority_matrix_accepts_residual_sell_exception_only(
    side: str,
    source: str,
    authority: str,
    residual_mode: str,
    reason: str,
) -> None:
    settings_obj = _settings(mode="live", dry_run=False, armed=True)
    settings_obj.RESIDUAL_LIVE_SELL_MODE = residual_mode
    decision = evaluate_submit_authority_policy(
        _approved(_plan(side=side, source=source, authority=authority)).as_final_payload(),
        settings_obj=settings_obj,
        plan_kind="residual",
    )

    assert decision.allowed is (reason == "allowed_residual_inventory_policy")
    assert decision.reason == reason


def test_live_dry_run_submit_authority_is_non_submitting() -> None:
    decision = evaluate_submit_authority_policy(
        _approved(_plan()).as_final_payload(),
        settings_obj=_settings(mode="live", dry_run=True, armed=False),
        plan_kind="target",
    )

    assert decision.allowed is False
    assert decision.reason == "live_dry_run_non_submitting"


@pytest.mark.parametrize(
    ("source", "authority"),
    [
        ("strategy_position", "configured_strategy_order_size"),
        ("strategy_position", "strategy_execution_intent"),
        ("strategy_position", "research_compatibility_execution_intent"),
        ("strategy_position", "residual_inventory_delta"),
    ],
)
def test_live_real_order_rejects_legacy_buy_before_executor(source: str, authority: str) -> None:
    object.__setattr__(settings, "MODE", "live")
    object.__setattr__(settings, "LIVE_DRY_RUN", False)
    object.__setattr__(settings, "LIVE_REAL_ORDER_ARMED", True)
    object.__setattr__(settings, "EXECUTION_ENGINE", "target_delta")
    called = False

    def _executor(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "called"}

    buy = _plan(source=source, authority=authority)
    service = LiveSignalExecutionService(
        broker=_Broker(),
        executor=_executor,
        harmless_dust_recorder=lambda **_kwargs: False,
    )
    request = TypedExecutionRequest(
        signal="BUY",
        ts=1,
        market_price=100_000_000.0,
        execution_decision_summary=_summary(buy=buy),
        observability_payload=ExecutionObservabilityPayload({"execution_decision": _summary(buy=buy).as_dict()}),
    )

    assert service.execute(request) is None
    assert called is False


def test_live_real_order_missing_target_plan_fails_closed_before_executor() -> None:
    object.__setattr__(settings, "MODE", "live")
    object.__setattr__(settings, "LIVE_DRY_RUN", False)
    object.__setattr__(settings, "LIVE_REAL_ORDER_ARMED", True)
    object.__setattr__(settings, "EXECUTION_ENGINE", "target_delta")
    called = False

    def _executor(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "called"}

    service = LiveSignalExecutionService(
        broker=_Broker(),
        executor=_executor,
        harmless_dust_recorder=lambda **_kwargs: False,
    )
    request = TypedExecutionRequest(
        signal="BUY",
        ts=1,
        market_price=100_000_000.0,
        execution_decision_summary=_summary(),
        observability_payload=ExecutionObservabilityPayload({"execution_decision": _summary().as_dict()}),
    )

    assert service.execute(request) is None
    assert called is False


def test_live_real_order_accepts_only_valid_residual_sell_exception_before_executor() -> None:
    object.__setattr__(settings, "MODE", "live")
    object.__setattr__(settings, "LIVE_DRY_RUN", False)
    object.__setattr__(settings, "LIVE_REAL_ORDER_ARMED", True)
    object.__setattr__(settings, "EXECUTION_ENGINE", "target_delta")
    object.__setattr__(settings, "RESIDUAL_LIVE_SELL_MODE", "enabled")
    calls: list[dict[str, object]] = []

    def _executor(*_args, **kwargs):
        calls.append(dict(kwargs))
        return {"status": "called"}

    residual = _approved(_plan(
        side="SELL",
        source="residual_inventory",
        authority="residual_inventory_policy",
        extra={"portfolio_target_authoritative": False},
    ))
    service = LiveSignalExecutionService(
        broker=_Broker(),
        executor=_executor,
        harmless_dust_recorder=lambda **_kwargs: False,
    )
    request = TypedExecutionRequest(
        signal="SELL",
        ts=1,
        market_price=100_000_000.0,
        execution_decision_summary=_summary(residual=residual),
        observability_payload=ExecutionObservabilityPayload({"execution_decision": _summary(residual=residual).as_dict()}),
    )

    assert service.execute(request) == {"status": "called"}
    assert len(calls) == 1

    object.__setattr__(settings, "RESIDUAL_LIVE_SELL_MODE", "telemetry")
    calls.clear()
    assert service.execute(request) is None
    assert calls == []
