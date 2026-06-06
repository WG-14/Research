from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from bithumb_bot.canonical_decision import sha256_prefixed
from bithumb_bot.db_core import (
    create_or_get_budget_lock,
    create_or_get_order_lock,
    ensure_schema,
    upsert_account_balance,
    upsert_pair_position,
)
from bithumb_bot.execution_plan_batch import ExecutionPlanBatch
from bithumb_bot.execution_service import ExecutionSubmitPlan, _execution_batch_payload_extra
from bithumb_bot.multi_pair_runtime import (
    MultiPairFailClosed,
    MultiPairRuntimeAuthority,
    PairRuntimeInputs,
    apply_pair_submit_result,
    build_multi_pair_runtime,
    replay_pair_submit_status,
    validate_execution_batch_for_runtime_scope,
    validate_multi_pair_replay_boundaries,
    verify_multi_asset_ledger_authority,
)
from bithumb_bot.portfolio_allocation import PortfolioAllocatorConfig
from bithumb_bot.runtime_scope import RuntimeScopeKey
from bithumb_bot.strategy_preference import StrategyPreference
from bithumb_bot.virtual_target_state import StrategyVirtualTargetState


def _memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _scope(pair: str, *, instance: str, interval: str = "1m") -> RuntimeScopeKey:
    return RuntimeScopeKey(
        pair=pair,
        interval=interval,
        strategy_instance_id=instance,
        strategy_name="sma_with_filter",
        runtime_contract_hash=sha256_prefixed({"runtime": pair}),
        approved_profile_hash=sha256_prefixed({"profile": pair}),
        strategy_parameters_hash=sha256_prefixed({"params": pair}),
    )


def _preference(scope: RuntimeScopeKey, signal: str) -> StrategyPreference:
    return StrategyPreference(
        strategy_instance_id=scope.strategy_instance_id,
        strategy_name=scope.strategy_name,
        pair=scope.pair,
        signal_direction=signal,
        reason="test",
        desired_exposure_krw=100_000.0,
        scope_key_hash=scope.scope_key_hash(),
        runtime_scope_key=scope.as_dict(),
    )


def _submit_plan(pair: str, scope_hash: str, *, side: str = "HOLD") -> ExecutionSubmitPlan:
    return ExecutionSubmitPlan(
        side=side,
        source="target_delta",
        authority="ExecutionSubmitPlan",
        final_action="HOLD" if side == "HOLD" else f"SUBMIT_{side}",
        qty=None if side == "HOLD" else 0.01,
        notional_krw=None if side == "HOLD" else 10_000.0,
        target_exposure_krw=42_000.0,
        current_effective_exposure_krw=42_000.0,
        delta_krw=0.0 if side == "HOLD" else 10_000.0,
        submit_expected=side != "HOLD",
        pre_submit_proof_status="not_required",
        block_reason="hold" if side == "HOLD" else "",
        idempotency_key=f"idem:{pair}:{scope_hash}",
        pair=pair,
        scope_key_hash=scope_hash,
        portfolio_target_hash=sha256_prefixed({"portfolio_target": pair}),
        submit_authority_policy_hash=sha256_prefixed({"submit_policy": pair}),
    )


def _pair_input(
    pair: str,
    signal: str,
    *,
    previous: float = 42_000.0,
    price: float = 21_000_000.0,
    interval: str = "1m",
) -> PairRuntimeInputs:
    scope = _scope(pair, instance=f"{pair}:sma", interval=interval)
    scope_hash = scope.scope_key_hash()
    return PairRuntimeInputs(
        pair=pair,
        interval=interval,
        scope_key=scope,
        data_preflight={
            "pair": pair,
            "interval": interval,
            "scope_key_hash": scope_hash,
            "coverage": "pass",
            "source_schema_hash": sha256_prefixed({"schema": pair}),
            "freshness": "pass",
            "decision_clock": "closed_candle",
        },
        selected_candle={
            "pair": pair,
            "interval": interval,
            "scope_key_hash": scope_hash,
            "closed_ts": 1_700_000_000_000,
            "close": price,
        },
        feature_snapshot={
            "pair": pair,
            "interval": interval,
            "scope_key_hash": scope_hash,
            "feature_hash": sha256_prefixed({"feature": pair}),
        },
        strategy_preference=_preference(scope, signal),
        decision_artifact={
            "pair": pair,
            "interval": interval,
            "scope_key_hash": scope_hash,
            "strategy_result_hash": sha256_prefixed({"decision": pair, "signal": signal}),
        },
        previous_target_exposure_krw=previous,
        reference_price=price,
        submit_plan=_submit_plan(pair, scope_hash),
        recovery_evidence={"pair": pair, "recovery": "not_started"},
    )


def _verified_authority() -> MultiPairRuntimeAuthority:
    return MultiPairRuntimeAuthority(
        enabled=True,
        shard_authority_verified=True,
        batch_risk_authority_verified=True,
        ledger_authority_verified=True,
        reconcile_authority_verified=True,
    )


def _ledger_conn() -> sqlite3.Connection:
    conn = _memory_conn()
    for currency in ("KRW", "BTC", "ETH"):
        upsert_account_balance(
            conn,
            currency=currency,
            available=1_000_000.0,
            locked=0.0,
            updated_ts=1_700_000_000,
            evidence_hash=sha256_prefixed({"balance": currency}),
        )
    upsert_pair_position(
        conn,
        pair="KRW-BTC",
        base_currency="BTC",
        quote_currency="KRW",
        available_qty=0.01,
        locked_qty=0.0,
        updated_ts=1_700_000_000,
        evidence_hash=sha256_prefixed({"position": "KRW-BTC"}),
    )
    upsert_pair_position(
        conn,
        pair="KRW-ETH",
        base_currency="ETH",
        quote_currency="KRW",
        available_qty=0.2,
        locked_qty=0.0,
        updated_ts=1_700_000_000,
        evidence_hash=sha256_prefixed({"position": "KRW-ETH"}),
    )
    create_or_get_budget_lock(
        conn,
        currency="KRW",
        pair="KRW-BTC",
        amount=10_000.0,
        reason="test",
        created_ts=1_700_000_000,
        idempotency_key="budget:btc",
        evidence={"pair": "KRW-BTC"},
    )
    create_or_get_order_lock(
        conn,
        pair="KRW-ETH",
        currency="ETH",
        amount=0.1,
        reason="test",
        created_ts=1_700_000_000,
        idempotency_key="order:eth",
        evidence={"pair": "KRW-ETH"},
    )
    return conn


def _result():
    return build_multi_pair_runtime(
        inputs=(
            _pair_input("KRW-BTC", "HOLD", previous=42_000.0, price=21_000_000.0),
            _pair_input("KRW-ETH", "HOLD", previous=7_000.0, price=3_500_000.0),
        ),
        authority=_verified_authority(),
        allocator_config=PortfolioAllocatorConfig(target_exposure_krw=100_000.0),
        manifest_hash=sha256_prefixed({"manifest": "multi-pair"}),
        conn=_ledger_conn(),
    )


def test_multi_pair_gate_off_fails_closed_with_structured_reason() -> None:
    with pytest.raises(MultiPairFailClosed) as exc:
        build_multi_pair_runtime(
            inputs=(_pair_input("KRW-BTC", "HOLD"), _pair_input("KRW-ETH", "HOLD")),
            authority=MultiPairRuntimeAuthority(enabled=False),
            allocator_config=PortfolioAllocatorConfig(target_exposure_krw=100_000.0),
            manifest_hash=sha256_prefixed({"manifest": "gate-off"}),
            conn=_ledger_conn(),
        )
    evidence = exc.value.as_dict()
    assert evidence["blocked_layer"] == "runtime_scope_validation"
    assert evidence["unsupported_reason"] == "multi_pair_runtime_unsupported"
    assert evidence["required_migration"] == "RuntimeScopeV2_multi_pair_authority"
    assert evidence["missing_authority"] == "live_multi_pair_enablement_gate"


def test_multi_pair_gate_on_missing_authority_fails_closed_with_specific_authority() -> None:
    with pytest.raises(MultiPairFailClosed) as exc:
        build_multi_pair_runtime(
            inputs=(_pair_input("KRW-BTC", "HOLD"), _pair_input("KRW-ETH", "HOLD")),
            authority=MultiPairRuntimeAuthority(
                enabled=True,
                shard_authority_verified=True,
                batch_risk_authority_verified=False,
                ledger_authority_verified=True,
                reconcile_authority_verified=True,
            ),
            allocator_config=PortfolioAllocatorConfig(target_exposure_krw=100_000.0),
            manifest_hash=sha256_prefixed({"manifest": "missing-authority"}),
            conn=_ledger_conn(),
        )
    evidence = exc.value.as_dict()
    assert evidence["blocked_layer"] == "runtime_authority_validation"
    assert evidence["missing_authority"] == "batch_risk_authority"


def test_multi_pair_complete_authority_creates_pair_scoped_shards_and_artifacts() -> None:
    result = _result()
    assert result.authority.as_dict()["authority_verified"] is True
    assert {shard.pair for shard in result.shards} == {"KRW-BTC", "KRW-ETH"}
    btc = next(shard for shard in result.shards if shard.pair == "KRW-BTC")
    eth = next(shard for shard in result.shards if shard.pair == "KRW-ETH")
    assert btc.selected_candle["pair"] == "KRW-BTC"
    assert eth.selected_candle["pair"] == "KRW-ETH"
    assert btc.feature_snapshot["feature_hash"] != eth.feature_snapshot["feature_hash"]
    assert btc.allocation_target["target_exposure_krw"] == pytest.approx(42_000.0)
    assert eth.allocation_target["target_exposure_krw"] == pytest.approx(7_000.0)
    assert btc.execution_pair_plan.pair == "KRW-BTC"
    assert eth.execution_pair_plan.pair == "KRW-ETH"
    assert result.execution_plan_batch.as_dict()["batch_risk_decision_evidence"]["risk_scope"] == "multi_pair_portfolio"


def test_pair_data_preflight_and_hold_allocation_do_not_borrow_other_pair_state() -> None:
    result = _result()
    by_pair = {shard.pair: shard for shard in result.shards}
    assert by_pair["KRW-BTC"].data_preflight["source_schema_hash"] != by_pair["KRW-ETH"].data_preflight["source_schema_hash"]
    assert by_pair["KRW-BTC"].allocation_target["target_exposure_krw"] == pytest.approx(42_000.0)
    assert by_pair["KRW-ETH"].allocation_target["target_exposure_krw"] == pytest.approx(7_000.0)
    with pytest.raises(MultiPairFailClosed) as exc:
        build_multi_pair_runtime(
            inputs=(
                _pair_input("KRW-BTC", "HOLD", previous=42_000.0),
                _pair_input("KRW-ETH", "HOLD", previous=None),  # type: ignore[arg-type]
            ),
            authority=_verified_authority(),
            allocator_config=PortfolioAllocatorConfig(target_exposure_krw=100_000.0),
            manifest_hash=sha256_prefixed({"manifest": "missing-previous"}),
            conn=_ledger_conn(),
        )
    assert exc.value.as_dict()["pair"] == "KRW-ETH"
    assert exc.value.as_dict()["fail_closed_reason"] == "multi_pair_previous_target_missing"


def test_scope_mismatch_and_missing_replay_layer_fail_at_detecting_layer() -> None:
    item = _pair_input("KRW-BTC", "HOLD")
    bad = PairRuntimeInputs(
        pair=item.pair,
        interval=item.interval,
        scope_key=item.scope_key,
        data_preflight={**dict(item.data_preflight), "scope_key_hash": "sha256:" + "0" * 64},
        selected_candle=item.selected_candle,
        feature_snapshot=item.feature_snapshot,
        strategy_preference=item.strategy_preference,
        decision_artifact=item.decision_artifact,
        previous_target_exposure_krw=item.previous_target_exposure_krw,
        reference_price=item.reference_price,
    )
    with pytest.raises(MultiPairFailClosed) as exc:
        build_multi_pair_runtime(
            inputs=(bad, _pair_input("KRW-ETH", "HOLD")),
            authority=_verified_authority(),
            allocator_config=PortfolioAllocatorConfig(target_exposure_krw=100_000.0),
            manifest_hash=sha256_prefixed({"manifest": "scope-mismatch"}),
            conn=_ledger_conn(),
        )
    assert exc.value.as_dict()["blocked_layer"] == "data_preflight"
    assert exc.value.as_dict()["replay_layer"] == "data_preflight"
    result = _result()
    broken_chain = result.shards[0].replay_hash_chain.__class__(
        **{**result.shards[0].replay_hash_chain.as_dict(), "portfolio_target_hash": ""}
    )
    broken_shard = result.shards[0].__class__(
        pair=result.shards[0].pair,
        interval=result.shards[0].interval,
        scope_key=result.shards[0].scope_key,
        data_preflight=result.shards[0].data_preflight,
        selected_candle=result.shards[0].selected_candle,
        feature_snapshot=result.shards[0].feature_snapshot,
        decision_artifact=result.shards[0].decision_artifact,
        allocation_target=result.shards[0].allocation_target,
        execution_pair_plan=result.shards[0].execution_pair_plan,
        reconcile_status=result.shards[0].reconcile_status,
        replay_hash_chain=broken_chain,
    )
    replay = validate_multi_pair_replay_boundaries(
        result.__class__(
            authority=result.authority,
            shards=(broken_shard, result.shards[1]),
            allocation_decision=result.allocation_decision,
            execution_plan_batch=result.execution_plan_batch,
            ledger_authority=result.ledger_authority,
            observability=result.observability,
        )
    )
    assert replay["status"] == "fail"
    assert replay["failing_layer"] == "replay_hash_chain"
    assert replay["failures"][0]["missing_layers"] == ["portfolio_target_hash"]


def test_virtual_target_state_cannot_be_live_submit_authority_and_interval_mismatch_blocks() -> None:
    item = _pair_input("KRW-BTC", "HOLD")
    virtual = StrategyVirtualTargetState(
        strategy_instance_id="btc",
        strategy_name="sma_with_filter",
        pair="KRW-BTC",
        interval="1m",
        scope_key_hash=item.scope_key.scope_key_hash(),
        runtime_contract_hash=item.scope_key.runtime_contract_hash,
        virtual_target_exposure_krw=100_000.0,
        virtual_target_qty=0.01,
        lifecycle_state="virtual_open",
        last_signal="BUY",
        updated_ts=1,
    )
    with pytest.raises(MultiPairFailClosed) as exc:
        build_multi_pair_runtime(
            inputs=(
                PairRuntimeInputs(
                    pair=item.pair,
                    interval=item.interval,
                    scope_key=item.scope_key,
                    data_preflight=item.data_preflight,
                    selected_candle=item.selected_candle,
                    feature_snapshot=item.feature_snapshot,
                    strategy_preference=item.strategy_preference,
                    decision_artifact={**dict(item.decision_artifact), "virtual_target_state": virtual},
                    previous_target_exposure_krw=item.previous_target_exposure_krw,
                    reference_price=item.reference_price,
                ),
                _pair_input("KRW-ETH", "HOLD"),
            ),
            authority=_verified_authority(),
            allocator_config=PortfolioAllocatorConfig(target_exposure_krw=100_000.0),
            manifest_hash=sha256_prefixed({"manifest": "virtual"}),
            conn=_ledger_conn(),
        )
    assert str(exc.value) == "virtual_target_state_not_live_submit_authority"
    with pytest.raises(MultiPairFailClosed) as interval_exc:
        build_multi_pair_runtime(
            inputs=(_pair_input("KRW-BTC", "HOLD", interval="1m"), _pair_input("KRW-ETH", "HOLD", interval="5m")),
            authority=_verified_authority(),
            allocator_config=PortfolioAllocatorConfig(target_exposure_krw=100_000.0),
            manifest_hash=sha256_prefixed({"manifest": "interval"}),
            conn=_ledger_conn(),
        )
    assert interval_exc.value.as_dict()["blocked_layer"] == "decision_clock_preflight"


def test_multi_pair_execution_batch_validation_and_execution_payload_extra() -> None:
    result = _result()
    batch = result.execution_plan_batch
    assert len(batch.pair_plans) == 2
    validation = validate_execution_batch_for_runtime_scope(
        batch,
        multi_pair_enabled=True,
        expected_pairs=("KRW-BTC", "KRW-ETH"),
    )
    assert validation["status"] == "pass"
    assert all(plan.idempotency_key and plan.lock_evidence_hash for plan in batch.pair_plans)
    assert set(batch.as_dict()["batch_risk_decision_evidence"]["pair_plan_hashes"]) == {
        plan.content_hash() for plan in batch.pair_plans
    }
    payload = _execution_batch_payload_extra(SimpleNamespace(execution_plan_bundle=SimpleNamespace(execution_plan_batch=batch)))
    assert payload["runtime_scope_mode"] == "multi_pair_portfolio"
    assert payload["primary_submit_plan_compatibility_authority"] is False
    assert payload["execution_plan_batch_pair_count"] == 2
    assert len(payload["execution_plan_batch_pair_plans"]) == 2


def test_single_pair_batch_size_one_remains_valid_and_malformed_multipair_rejected() -> None:
    result = _result()
    single = ExecutionPlanBatch(
        runtime_strategy_set_manifest_hash=result.execution_plan_batch.runtime_strategy_set_manifest_hash,
        allocation_decision_hash=result.execution_plan_batch.allocation_decision_hash,
        pair_plans=(result.execution_plan_batch.pair_plans[0],),
        batch_risk_decision_evidence={"status": "ALLOW"},
        budget_lock_hash=sha256_prefixed({"lock": "single"}),
    )
    assert validate_execution_batch_for_runtime_scope(
        single,
        multi_pair_enabled=False,
        expected_pairs=("KRW-BTC",),
    )["status"] == "pass"
    assert validate_execution_batch_for_runtime_scope(
        single,
        multi_pair_enabled=True,
        expected_pairs=("KRW-BTC", "KRW-ETH"),
    )["reason"] == "multi_pair_batch_pair_set_mismatch"


def test_partial_submit_failure_is_pair_isolated_and_replayable() -> None:
    result = _result()
    btc = next(shard for shard in result.shards if shard.pair == "KRW-BTC")
    eth = next(shard for shard in result.shards if shard.pair == "KRW-ETH")
    btc_success = apply_pair_submit_result(
        btc,
        submit_status="success",
        broker_response={"exchange_order_id": "btc-order"},
        recovery_status="reconciled",
    )
    eth_failure = apply_pair_submit_result(
        eth,
        submit_status="broker_error",
        broker_response={"error": "eth rejected"},
        recovery_status="reconcile_required",
    )
    replay = replay_pair_submit_status((btc_success, eth_failure))
    assert replay["batch_status"] == "partial_failure"
    assert replay["succeeded_pairs"] == ["KRW-BTC"]
    assert replay["failed_pairs"] == ["KRW-ETH"]
    assert btc_success.allocation_target == btc.allocation_target
    assert btc_success.execution_pair_plan.lock_evidence_hash == btc.execution_pair_plan.lock_evidence_hash


def test_multi_asset_ledger_authority_verifies_required_balances_positions_locks_and_rejects_portfolio_projection() -> None:
    verified = verify_multi_asset_ledger_authority(
        _ledger_conn(),
        required_pairs=("KRW-BTC", "KRW-ETH"),
    )
    assert verified["authority_verified"] is True
    assert verified["required_currencies"] == ["BTC", "ETH", "KRW"]
    assert verified["portfolio_id_1_multi_pair_live_authority"] is False
    assert verified["live_multi_pair_enablement"] == "enabled_authority_verified"
    missing = verify_multi_asset_ledger_authority(
        _memory_conn(),
        required_pairs=("KRW-BTC", "KRW-ETH"),
    )
    assert missing["authority_verified"] is False
    assert missing["portfolio_id_1_multi_pair_live_authority"] is False
    assert missing["live_multi_pair_enablement"] == "fail_closed_until_scoped_batch_ledger_authority_verified"


def test_operator_observability_exposes_pair_scope_batch_lock_reason_and_replay_layer() -> None:
    result = _result()
    obs = result.observability
    assert obs["runtime_scope_mode"] == "multi_pair_portfolio"
    assert obs["execution_plan_batch_hash"] == result.execution_plan_batch.content_hash()
    assert {entry["pair"] for entry in obs["pairs"]} == {"KRW-BTC", "KRW-ETH"}
    for entry in obs["pairs"]:
        assert entry["scope_key_hash"].startswith("sha256:")
        assert entry["pair_execution_plan_hash"].startswith("sha256:")
        assert entry["lock_status"] == "verified"
        assert entry["fail_closed_reason"] == ""
        assert entry["replay_layer"] == "replay_hash_chain"
