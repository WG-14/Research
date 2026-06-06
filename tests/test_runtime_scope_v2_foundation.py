from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from bithumb_bot.db_core import (
    ensure_schema,
    load_strategy_virtual_target_state,
    multi_asset_ledger_authority_status,
    upsert_account_balance,
    upsert_pair_position,
    upsert_strategy_virtual_target_state,
)
from bithumb_bot.execution_plan_batch import (
    ExecutionPlanBatch,
    PairExecutionPlan,
    build_pre_submit_risk_finalization_artifact,
    reject_dict_only_batch_authority,
    verify_pair_plan_replay_complete,
    verify_pre_submit_risk_finalization_artifact,
)
from bithumb_bot.execution_service import (
    ExecutionDecisionSummary,
    ExecutionSubmitPlan,
    ExecutionTargetPlanningInput,
)
from bithumb_bot.runtime.decision_coordinator import DecisionCoordinator
from bithumb_bot.runtime.execution_coordinator import ExecutionCoordinator
from bithumb_bot.portfolio_allocation import (
    PortfolioAllocationInput,
    PortfolioAllocator,
    PortfolioAllocatorConfig,
    SignalAggregator,
)
from bithumb_bot.runtime_data_provider import (
    DecisionClockPolicy,
    RuntimeDataAvailabilityReport,
    RuntimeFeatureSnapshot,
    RuntimeStrategyDataRequirements,
    SQLiteRuntimeDataProvider,
)
from bithumb_bot.runtime_scope import (
    ReplayHashChain,
    RuntimeScopeKey,
    validate_replay_hash_chain,
    validate_scope_key_hash,
)
from bithumb_bot.runtime_scope_replay import verify_runtime_scope_replay_payload
from bithumb_bot.strategy_preference import StrategyPreference
from bithumb_bot.runtime_strategy_set import _validate_feature_snapshot_scope_preflight
from bithumb_bot.virtual_target_state import (
    StrategyVirtualTargetState,
    assert_not_live_submit_authority,
    evolve_strategy_virtual_target_state,
)
from bithumb_bot.research.strategy_registry import DataCapabilityRequirement


def _scope(**overrides: object) -> RuntimeScopeKey:
    payload = {
        "pair": "KRW-BTC",
        "interval": "1m",
        "strategy_instance_id": "sma:btc:1m",
        "strategy_name": "sma_with_filter",
        "runtime_contract_hash": "sha256:" + "1" * 64,
        "approved_profile_hash": "sha256:" + "2" * 64,
        "strategy_parameters_hash": "sha256:" + "3" * 64,
    }
    payload.update(overrides)
    return RuntimeScopeKey(**payload)


def _preference(pair: str, signal: str, *, instance: str) -> StrategyPreference:
    scope = _scope(pair=pair, strategy_instance_id=instance)
    return StrategyPreference(
        strategy_instance_id=instance,
        strategy_name="sma_with_filter",
        pair=pair,
        signal_direction=signal,
        reason="test",
        desired_exposure_krw=100_000.0,
        scope_key_hash=scope.scope_key_hash(),
        runtime_scope_key=scope.as_dict(),
    )


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def test_runtime_scope_key_hash_is_stable_and_non_colliding() -> None:
    key = _scope()
    assert key.scope_key_hash() == _scope().scope_key_hash()
    assert key.scope_key_hash() != _scope(pair="KRW-ETH").scope_key_hash()
    assert key.scope_key_hash() != _scope(interval="5m").scope_key_hash()
    assert key.scope_key_hash() != _scope(approved_profile_hash="sha256:" + "4" * 64).scope_key_hash()
    assert key.scope_key_hash() != _scope(strategy_parameters_hash="sha256:" + "5" * 64).scope_key_hash()


def test_runtime_scope_replay_validation_fails_closed_on_scope_mismatch() -> None:
    key = _scope()
    payload = {
        "runtime_scope_key": key.as_dict(),
        "scope_key_hash": key.scope_key_hash(),
    }
    assert validate_scope_key_hash(payload)["status"] == "pass"
    payload["scope_key_hash"] = _scope(pair="KRW-ETH").scope_key_hash()
    result = validate_scope_key_hash(payload)
    assert result["status"] == "fail"
    assert result["mismatch_reason"] == "scope_key_hash_mismatch"


def test_replay_hash_chain_detects_missing_and_mismatched_layers() -> None:
    chain = ReplayHashChain(
        manifest_hash="sha256:" + "a" * 64,
        scope_key_hash=_scope().scope_key_hash(),
        runtime_data_availability_hash="sha256:" + "b" * 64,
        feature_snapshot_hash="sha256:" + "c" * 64,
        runtime_decision_request_hash="sha256:" + "d" * 64,
        allocation_input_hash="sha256:" + "e" * 64,
        portfolio_target_hash="sha256:" + "f" * 64,
        execution_plan_batch_hash="sha256:" + "7" * 64,
        pair_execution_plan_hash="sha256:" + "6" * 64,
        execution_submit_plan_hash="sha256:" + "0" * 64,
        pre_submit_risk_decision_hash="sha256:" + "9" * 64,
    )
    payload = {"replay_hash_chain": chain.as_dict(), "replay_hash_chain_hash": chain.chain_hash()}
    assert validate_replay_hash_chain(payload)["status"] == "pass"
    payload["replay_hash_chain_hash"] = "sha256:" + "8" * 64
    assert validate_replay_hash_chain(payload)["mismatch_reason"] == "replay_hash_chain_hash_mismatch"

    combined = {
        "runtime_scope_key": _scope().as_dict(),
        "scope_key_hash": _scope().scope_key_hash(),
        "replay_hash_chain": chain.as_dict(),
        "replay_hash_chain_hash": chain.chain_hash(),
    }
    assert verify_runtime_scope_replay_payload(combined)["status"] == "pass"


def test_pair_aware_allocation_uses_previous_exposure_and_price_for_own_pair() -> None:
    preferences = (
        _preference("KRW-BTC", "HOLD", instance="btc-hold"),
        _preference("KRW-ETH", "BUY", instance="eth-buy"),
    )
    config = PortfolioAllocatorConfig(target_exposure_krw=100_000.0)
    allocation_input = PortfolioAllocationInput(
        preference_set=SignalAggregator().aggregate(preferences),
        allocator_config=config,
        previous_target_exposure_by_pair={"KRW-BTC": 42_000.0, "KRW-ETH": 7_000.0},
        reference_price_by_pair={"KRW-BTC": 21_000_000.0, "KRW-ETH": 3_500_000.0},
    )
    decision = PortfolioAllocator(config).allocate(allocation_input)
    btc = decision.target_for_pair("KRW-BTC")
    eth = decision.target_for_pair("KRW-ETH")
    assert btc is not None and btc.target_exposure_krw == pytest.approx(42_000.0)
    assert btc.target_qty == pytest.approx(42_000.0 / 21_000_000.0)
    assert eth is not None and eth.target_exposure_krw == pytest.approx(100_000.0)
    assert eth.target_qty == pytest.approx(100_000.0 / 3_500_000.0)
    assert btc.scope_key_hashes
    assert eth.scope_key_hashes


def test_pair_aware_hold_missing_own_pair_previous_exposure_fails_closed() -> None:
    config = PortfolioAllocatorConfig(target_exposure_krw=100_000.0)
    allocation_input = PortfolioAllocationInput(
        preference_set=SignalAggregator().aggregate((_preference("KRW-BTC", "HOLD", instance="btc-hold"),)),
        allocator_config=config,
        previous_target_exposure_by_pair={"KRW-ETH": 7_000.0},
        reference_price_by_pair={"KRW-BTC": 21_000_000.0},
    )
    decision = PortfolioAllocator(config).allocate(allocation_input)
    target = decision.target_for_pair("KRW-BTC")
    assert target is not None
    assert target.authoritative is False
    assert target.fail_closed_reason == "hold_missing_previous_target_exposure"


def test_execution_plan_batch_hash_stability_and_dict_authority_rejection() -> None:
    order_rule_snapshot = {"pair": "KRW-BTC", "min_qty": 0.0001, "min_notional_krw": 5000.0}
    pair_plan = PairExecutionPlan(
        pair="KRW-BTC",
        scope_key_hash=_scope().scope_key_hash(),
        scope_key_hashes=(_scope().scope_key_hash(),),
        portfolio_target_hash="sha256:" + "a" * 64,
        execution_submit_plan_hash="sha256:" + "b" * 64,
        idempotency_key="idem-btc",
        submit_authority_policy_hash="sha256:" + "c" * 64,
        pre_submit_risk_decision_hash="sha256:" + "d" * 64,
        pre_submit_risk_required=True,
        pre_submit_risk_proof_status="allow",
        order_rule_snapshot_hash="sha256:" + "9" * 64,
        order_rule_signature="sha256:" + "8" * 64,
        order_rule_snapshot=order_rule_snapshot,
    )
    batch = ExecutionPlanBatch(
        runtime_strategy_set_manifest_hash="sha256:" + "e" * 64,
        allocation_decision_hash="sha256:" + "f" * 64,
        pair_plans=(pair_plan,),
        batch_risk_decision_evidence={"status": "ALLOW"},
        budget_lock_hash="sha256:" + "1" * 64,
    )
    assert batch.content_hash() == ExecutionPlanBatch(
        runtime_strategy_set_manifest_hash="sha256:" + "e" * 64,
        allocation_decision_hash="sha256:" + "f" * 64,
        pair_plans=(pair_plan,),
        batch_risk_decision_evidence={"status": "ALLOW"},
        budget_lock_hash="sha256:" + "1" * 64,
    ).content_hash()
    assert batch.as_dict()["pair_plans"][0]["pair"] == "KRW-BTC"
    assert batch.as_dict()["pair_plans"][0]["scope_key_hashes"] == [_scope().scope_key_hash()]
    assert batch.as_dict()["pair_plans"][0]["order_rule_snapshot_hash"] == "sha256:" + "9" * 64
    with pytest.raises(TypeError, match="dict_only_execution_batch_not_authority"):
        reject_dict_only_batch_authority(batch.as_dict())


def test_pair_execution_plan_scope_and_order_rule_evidence_are_hash_bound() -> None:
    scope_hashes = (_scope(strategy_instance_id="s1").scope_key_hash(), _scope(strategy_instance_id="s2").scope_key_hash())
    order_rule_snapshot = {"pair": "KRW-BTC", "min_qty": 0.0001, "min_notional_krw": 5000.0}
    pair_plan = PairExecutionPlan(
        pair="KRW-BTC",
        scope_key_hashes=scope_hashes,
        portfolio_target_hash="sha256:" + "a" * 64,
        execution_submit_plan_hash="sha256:" + "b" * 64,
        idempotency_key="idem-btc",
        submit_authority_policy_hash="sha256:" + "c" * 64,
        pre_submit_risk_decision_hash="sha256:" + "d" * 64,
        pre_submit_risk_required=True,
        pre_submit_risk_proof_status="allow",
        order_rule_snapshot_hash="sha256:" + "9" * 64,
        order_rule_signature="sha256:" + "8" * 64,
        order_rule_snapshot=order_rule_snapshot,
        submit_expected=True,
        lock_evidence_hash="sha256:" + "e" * 64,
        lock_type="quote_budget",
        lock_status="active",
    )
    changed_order_rules = PairExecutionPlan(
        **{
            **pair_plan.as_dict(),
            "order_rule_snapshot_hash": "sha256:" + "7" * 64,
        }
    )
    assert pair_plan.as_dict()["scope_key_hashes"] == sorted(scope_hashes)
    assert verify_pair_plan_replay_complete(pair_plan.as_dict())["status"] == "pass"
    missing_scope = dict(pair_plan.as_dict())
    missing_scope["scope_key_hashes"] = []
    assert verify_pair_plan_replay_complete(missing_scope)["status"] == "fail"
    assert "scope_key_hashes" in verify_pair_plan_replay_complete(missing_scope)["missing_fields"]
    assert pair_plan.content_hash() != changed_order_rules.content_hash()


def test_pair_execution_plan_rejects_submit_expected_missing_order_rule_evidence() -> None:
    with pytest.raises(ValueError, match="pair_execution_plan_order_rule_evidence_missing"):
        PairExecutionPlan(
            pair="KRW-BTC",
            scope_key_hashes=(_scope().scope_key_hash(),),
            portfolio_target_hash="sha256:" + "a" * 64,
            execution_submit_plan_hash="sha256:" + "b" * 64,
            idempotency_key="idem-btc",
            submit_authority_policy_hash="sha256:" + "c" * 64,
            pre_submit_risk_decision_hash="sha256:" + "d" * 64,
            pre_submit_risk_required=True,
            pre_submit_risk_proof_status="allow",
            submit_expected=True,
        )


def test_pre_submit_risk_finalization_artifact_detects_mismatch() -> None:
    payload = {
        "execution_plan_batch_hash": "sha256:" + "1" * 64,
        "execution_plan_batch_id": "batch-1",
        "pair_execution_plan_hash": "sha256:" + "2" * 64,
        "pair_execution_plan_pair": "KRW-BTC",
        "submit_plan_hash": "sha256:" + "3" * 64,
        "content_hash": "sha256:" + "4" * 64,
        "pre_submit_risk_decision_hash": "sha256:" + "5" * 64,
        "pre_submit_risk_policy_hash": "sha256:" + "6" * 64,
        "pre_submit_risk_input_hash": "sha256:" + "7" * 64,
        "pre_submit_risk_evidence_hash": "sha256:" + "8" * 64,
        "pre_submit_risk_plan_hash": "sha256:" + "3" * 64,
    }
    artifact = build_pre_submit_risk_finalization_artifact(payload)
    final_payload = {
        **payload,
        "pre_submit_risk_finalization_artifact": artifact,
        "pre_submit_risk_finalization_hash": artifact["pre_submit_risk_finalization_hash"],
    }
    assert verify_pre_submit_risk_finalization_artifact(final_payload)["status"] == "pass"
    tampered = {**final_payload, "pre_submit_risk_decision_hash": "sha256:" + "9" * 64}
    mismatch = verify_pre_submit_risk_finalization_artifact(tampered)
    assert mismatch["status"] == "fail"
    assert "pre_submit_risk_decision_hash" in mismatch["mismatch_reason"]


def _submit_plan(*, idempotency_key: str = "idem-btc") -> ExecutionSubmitPlan:
    return ExecutionSubmitPlan(
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
        idempotency_key=idempotency_key,
        pair="KRW-BTC",
        portfolio_target_hash="sha256:" + "a" * 64,
        submit_authority_policy_hash="sha256:" + "c" * 64,
    )


def _summary(plan: ExecutionSubmitPlan) -> ExecutionDecisionSummary:
    return ExecutionDecisionSummary(
        raw_signal=plan.side,
        final_signal=plan.side,
        final_action=plan.final_action,
        submit_expected=True,
        pre_submit_proof_status="passed",
        block_reason="none",
        strategy_sell_candidate=None,
        residual_sell_candidate=None,
        target_exposure_krw=plan.target_exposure_krw,
        current_effective_exposure_krw=plan.current_effective_exposure_krw,
        tracked_residual_exposure_krw=None,
        buy_delta_krw=plan.delta_krw,
        residual_live_sell_mode="off",
        residual_buy_sizing_mode="target",
        residual_submit_plan=None,
        buy_submit_plan=None,
        target_shadow_decision=None,
        target_submit_plan=plan,
    )


def _batch_for_plan(plan: ExecutionSubmitPlan) -> ExecutionPlanBatch:
    order_rule_snapshot = {"pair": "KRW-BTC", "min_qty": 0.0001, "min_notional_krw": 5000.0}
    pair_plan = PairExecutionPlan(
        pair="KRW-BTC",
        scope_key_hashes=(_scope().scope_key_hash(),),
        portfolio_target_hash=str(plan.portfolio_target_hash),
        execution_submit_plan_hash=plan.content_hash(),
        idempotency_key=str(plan.idempotency_key),
        submit_authority_policy_hash=str(plan.submit_authority_policy_hash),
        pre_submit_risk_decision_hash="sha256:" + "d" * 64,
        pre_submit_risk_required=True,
        pre_submit_risk_proof_status="allow",
        order_rule_snapshot_hash="sha256:" + "9" * 64,
        order_rule_signature="sha256:" + "8" * 64,
        order_rule_snapshot=order_rule_snapshot,
        lock_evidence_hash="sha256:" + "e" * 64,
        lock_type="quote_budget",
        lock_status="active",
        submit_expected=True,
    )
    return ExecutionPlanBatch(
        runtime_strategy_set_manifest_hash="sha256:" + "f" * 64,
        allocation_decision_hash="sha256:" + "1" * 64,
        pair_plans=(pair_plan,),
        batch_risk_decision_evidence={"status": "ALLOW"},
        budget_lock_hash="sha256:" + "2" * 64,
    )


def test_execution_coordinator_requires_batch_selected_pair_plan_for_runtime_bundle() -> None:
    plan = _submit_plan()
    summary = _summary(plan)
    coordinator = ExecutionCoordinator("target_delta")
    missing = coordinator.execute_cycle(
        candle_ts=1,
        decision_id=1,
        signal="BUY",
        decision_context={"runtime_pair": "KRW-BTC"},
        execution_plan_bundle=SimpleNamespace(),
        execution_decision_summary=summary,
        submit_invoker=lambda: {"submitted": True},
    )
    assert missing.planning_status == "execution_plan_batch_missing"

    batch = _batch_for_plan(plan)
    valid = coordinator.execute_cycle(
        candle_ts=1,
        decision_id=1,
        signal="BUY",
        decision_context={
            "runtime_pair": "KRW-BTC",
            "execution_plan_batch_hash": batch.content_hash(),
            "pair_execution_plan_hash": batch.pair_plans[0].content_hash(),
        },
        execution_plan_bundle=SimpleNamespace(execution_plan_batch=batch),
        execution_decision_summary=summary,
        submit_invoker=lambda: {"submitted": True},
    )
    assert valid.planning_status == "submitted"
    assert valid.submitted is True


def test_decision_coordinator_records_execution_plan_batch_in_normal_cycle() -> None:
    conn = SimpleNamespace(close=lambda: None, commit=lambda: None)
    plan = _submit_plan()
    batch = _batch_for_plan(plan)
    planning_bundle = SimpleNamespace(
        summary=_summary(plan),
        execution_plan_batch=batch,
        persistence_context={
            "portfolio_allocation_decision": {"allocation_decision_hash": "sha256:" + "1" * 64},
            "execution_decision": {},
            "ts": 123,
            "last_close": 10.0,
            "execution_plan_batch_hash": batch.content_hash(),
            "execution_plan_batch_id": batch.batch_id,
        },
    )
    typed_bundle = SimpleNamespace(
        candle_ts=123,
        market_price=10.0,
        strategy_set=SimpleNamespace(
            multi_strategy_enabled=False,
            market_scope=SimpleNamespace(pair="KRW-BTC", interval="1m"),
        ),
        results=(
            SimpleNamespace(
                decision=SimpleNamespace(
                    strategy_name="safe_hold",
                    final_signal="HOLD",
                    final_reason="unit",
                )
            ),
        ),
    )
    calls: list[str] = []
    coordinator = DecisionCoordinator(
        db_factory=lambda: conn,
        decision_gateway_factory=lambda: SimpleNamespace(decide_bundle=lambda *_args, **_kwargs: typed_bundle),
        planner_factory=lambda **_kwargs: SimpleNamespace(
            plan_runtime_strategy_results=lambda *_args, **_kwargs: planning_bundle
        ),
        record_runtime_strategy_decision_bundle_fn=lambda *_args, **_kwargs: {
            calls.append("bundle") or "runtime_strategy_decision_bundle_id": 1,
            "runtime_strategy_decision_bundle_hash": "sha256:" + "3" * 64,
        },
        record_portfolio_allocation_decision_fn=lambda *_args, **_kwargs: {
            calls.append("allocation") or "portfolio_allocation_decision_id": 2,
            "allocation_decision_hash": "sha256:" + "1" * 64,
            "portfolio_target_id": 3,
            "portfolio_target_hash": str(plan.portfolio_target_hash),
        },
        record_execution_plan_batch_fn=lambda *_args, **_kwargs: {
            calls.append("batch") or "execution_plan_batch_hash": batch.content_hash(),
            "execution_plan_batch_id": batch.batch_id,
        },
        record_execution_plan_fn=lambda *_args, **_kwargs: {
            calls.append("execution") or "execution_plan_id": 4,
            "execution_plan_bundle_hash": "sha256:" + "4" * 64,
            "execution_submit_plan_hash": plan.content_hash(),
        },
        record_strategy_decision_fn=lambda *_args, **_kwargs: 5,
        target_position_state_persister=lambda *_args, **_kwargs: False,
    )

    result = coordinator.decide_cycle(
        runtime_strategy_set=typed_bundle.strategy_set,
        candle_ts=123,
        updated_ts=456,
    )

    assert calls == ["bundle", "allocation", "batch", "execution"]
    assert result.execution_plan_batch_hash == batch.content_hash()
    assert result.execution_plan_batch_id == batch.batch_id
    assert result.as_dict()["execution_plan_batch_hash"] == batch.content_hash()


def test_virtual_target_state_is_independent_and_not_live_submit_authority() -> None:
    conn = _memory_conn()
    try:
        first = StrategyVirtualTargetState(
            strategy_instance_id="s1",
            strategy_name="sma_with_filter",
            pair="KRW-BTC",
            interval="1m",
            scope_key_hash=_scope(strategy_instance_id="s1").scope_key_hash(),
            runtime_contract_hash="sha256:" + "1" * 64,
            virtual_target_exposure_krw=10_000.0,
            virtual_target_qty=0.001,
            lifecycle_state="virtual_open",
            last_signal="BUY",
            updated_ts=1,
        )
        second = StrategyVirtualTargetState(
            strategy_instance_id="s2",
            strategy_name="safe_hold",
            pair="KRW-BTC",
            interval="1m",
            scope_key_hash=_scope(strategy_instance_id="s2").scope_key_hash(),
            runtime_contract_hash="sha256:" + "1" * 64,
            virtual_target_exposure_krw=0.0,
            virtual_target_qty=0.0,
            lifecycle_state="virtual_flat",
            last_signal="HOLD",
            updated_ts=2,
        )
        upsert_strategy_virtual_target_state(conn, first)
        upsert_strategy_virtual_target_state(conn, second)
        loaded_first = load_strategy_virtual_target_state(
            conn,
            strategy_instance_id="s1",
            pair="KRW-BTC",
            interval="1m",
            scope_key_hash=first.scope_key_hash,
        )
        loaded_second = load_strategy_virtual_target_state(
            conn,
            strategy_instance_id="s2",
            pair="KRW-BTC",
            interval="1m",
            scope_key_hash=second.scope_key_hash,
        )
        assert loaded_first is not None and loaded_first.virtual_target_exposure_krw == pytest.approx(10_000.0)
        assert loaded_second is not None and loaded_second.virtual_target_exposure_krw == pytest.approx(0.0)
        assert loaded_first.strategy_name == "sma_with_filter"
        with pytest.raises(TypeError, match="virtual_target_state_not_live_submit_authority"):
            assert_not_live_submit_authority(first)
        with pytest.raises(TypeError, match="virtual_target_state_not_live_submit_authority"):
            ExecutionTargetPlanningInput(portfolio_target=first)  # type: ignore[arg-type]
    finally:
        conn.close()


def test_virtual_target_state_evolves_without_actual_submit_authority() -> None:
    scope = _scope(strategy_instance_id="s1")
    bought = evolve_strategy_virtual_target_state(
        previous=None,
        strategy_instance_id="s1",
        strategy_name="sma_with_filter",
        pair="KRW-BTC",
        interval="1m",
        scope_key_hash=scope.scope_key_hash(),
        runtime_contract_hash=scope.runtime_contract_hash,
        signal="BUY",
        target_exposure_krw=50_000.0,
        reference_price=25_000_000.0,
        updated_ts=10,
    )
    held = evolve_strategy_virtual_target_state(
        previous=bought,
        strategy_instance_id="s1",
        strategy_name="sma_with_filter",
        pair="KRW-BTC",
        interval="1m",
        scope_key_hash=scope.scope_key_hash(),
        runtime_contract_hash=scope.runtime_contract_hash,
        signal="HOLD",
        target_exposure_krw=None,
        reference_price=25_000_000.0,
        updated_ts=11,
    )
    sold = evolve_strategy_virtual_target_state(
        previous=held,
        strategy_instance_id="s1",
        strategy_name="sma_with_filter",
        pair="KRW-BTC",
        interval="1m",
        scope_key_hash=scope.scope_key_hash(),
        runtime_contract_hash=scope.runtime_contract_hash,
        signal="SELL",
        target_exposure_krw=None,
        reference_price=25_000_000.0,
        updated_ts=12,
    )
    assert bought.virtual_target_exposure_krw == pytest.approx(50_000.0)
    assert held.virtual_target_exposure_krw == pytest.approx(50_000.0)
    assert sold.virtual_target_exposure_krw == pytest.approx(0.0)
    assert bought.as_dict()["live_submit_authority"] is False
    with pytest.raises(TypeError, match="virtual_target_state_not_live_submit_authority"):
        assert_not_live_submit_authority(bought.as_dict())


def test_multi_asset_ledger_stores_balances_positions_and_blocks_portfolio_projection_authority() -> None:
    conn = _memory_conn()
    try:
        upsert_account_balance(conn, currency="KRW", available=1000.0, locked=50.0, updated_ts=1)
        upsert_pair_position(
            conn,
            pair="KRW-BTC",
            base_currency="BTC",
            quote_currency="KRW",
            available_qty=0.1,
            locked_qty=0.02,
            updated_ts=1,
        )
        upsert_pair_position(
            conn,
            pair="KRW-ETH",
            base_currency="ETH",
            quote_currency="KRW",
            available_qty=2.0,
            locked_qty=0.3,
            updated_ts=1,
        )
        rows = conn.execute("SELECT pair, total_qty FROM pair_positions ORDER BY pair").fetchall()
        assert [(row["pair"], row["total_qty"]) for row in rows] == [
            ("KRW-BTC", pytest.approx(0.12)),
            ("KRW-ETH", pytest.approx(2.3)),
        ]
        status = multi_asset_ledger_authority_status(conn)
        assert status["status"] == "present"
        assert status["authority_verification_status"] == "present_unverified"
        assert status["authority_verified"] is False
        assert status["reconcile_status"] == "not_multi_pair_verified"
        assert status["portfolio_id_1_multi_pair_live_authority"] is False
        assert status["live_multi_pair_enablement"] == "fail_closed_until_scoped_batch_ledger_authority_verified"
    finally:
        conn.close()


class _FakeSpec:
    strategy_name = "fake"
    strategy_instance_id = "fake-1"
    pair = "KRW-BTC"
    interval = "1m"


class _FakeStrategySet:
    active_strategies = (_FakeSpec(),)

    class market_scope:
        pair = "KRW-BTC"
        interval = "1m"


class _FakeResolver:
    def resolve_for_strategy_set(self, strategy_set: object) -> RuntimeStrategyDataRequirements:
        return RuntimeStrategyDataRequirements(
            required=(
                DataCapabilityRequirement(
                    name="candles",
                    required=True,
                    lookback_rows=1,
                    closed_candle_required=True,
                ),
            ),
            optional=(),
            per_strategy={"fake-1": {"strategy_name": "fake", "required": ["candles"], "optional": []}},
        )


def test_runtime_data_preflight_and_snapshot_are_scope_aware() -> None:
    conn = _memory_conn()
    try:
        conn.execute(
            "INSERT INTO candles(pair, interval, ts, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("KRW-BTC", "1m", 1000, 1.0, 2.0, 0.5, 1.5, 10.0),
        )
        provider = SQLiteRuntimeDataProvider(conn, resolver=_FakeResolver())
        report = provider.preflight(_FakeStrategySet(), through_ts_ms=1000)
        assert report.ok
        assert "fake-1:KRW-BTC:1m" in report.as_dict()["coverage_by_scope"]
        assert report.as_dict()["decision_clock_policy"] == "single_interval_same_closed_candle_fail_closed_v1"
        request = type(
            "Request",
            (),
            {
                "pair": "KRW-BTC",
                "interval": "1m",
                "through_ts_ms": 1000,
                "runtime_scope_key": _scope(strategy_instance_id="fake-1"),
            },
        )()
        snapshot = provider.snapshot(request, _FakeResolver().resolve_for_strategy_set(_FakeStrategySet()))
        assert snapshot is not None
        payload = snapshot.as_dict()
        assert payload["scope_key_hash"] == _scope(strategy_instance_id="fake-1").scope_key_hash()
        assert payload["source_schema_hash_by_scope"]
        assert payload["freshness_by_scope"][payload["scope_key_hash"]]["decision_clock_policy"] == (
            "single_interval_same_closed_candle_fail_closed_v1"
        )
    finally:
        conn.close()


def test_decision_clock_policy_fails_closed_for_mixed_intervals() -> None:
    policy = DecisionClockPolicy()

    single = policy.evaluate_intervals(("1m", "1m"))
    mixed = policy.evaluate_intervals(("1m", "5m"))

    assert single["status"] == "PASS"
    assert mixed["status"] == "FAIL"
    assert mixed["reason"] == "single_interval_runtime_unsupported"


def test_runtime_data_snapshot_scope_mismatch_fails_closed() -> None:
    scope = _scope(strategy_instance_id="fake-1")
    snapshot = RuntimeFeatureSnapshot(
        {
            "scope_key_hash": scope.scope_key_hash(),
            "preflight_scope_id": "fake-1:KRW-BTC:1m",
        }
    )
    request = SimpleNamespace(
        strategy_instance_id="fake-1",
        pair="KRW-BTC",
        interval="1m",
    )
    matching_report = RuntimeDataAvailabilityReport(
        {
            "coverage_by_scope": {scope.scope_key_hash(): {}},
            "selected_candle_by_scope": {scope.scope_key_hash(): {}},
            "source_schema_hash_by_scope": {scope.scope_key_hash(): "sha256:" + "1" * 64},
            "freshness_by_scope": {scope.scope_key_hash(): {"status": "PASS"}},
        }
    )
    _validate_feature_snapshot_scope_preflight(snapshot, matching_report, request=request)

    mismatched_report = RuntimeDataAvailabilityReport(
        {
            "coverage_by_scope": {"other-scope": {}},
            "selected_candle_by_scope": {"other-scope": {}},
            "source_schema_hash_by_scope": {"other-scope": "sha256:" + "1" * 64},
            "freshness_by_scope": {"other-scope": {"status": "PASS"}},
        }
    )
    with pytest.raises(RuntimeError, match="runtime_data_snapshot_preflight_scope_mismatch"):
        _validate_feature_snapshot_scope_preflight(snapshot, mismatched_report, request=request)
