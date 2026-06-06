from __future__ import annotations

import sqlite3

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
    reject_dict_only_batch_authority,
)
from bithumb_bot.portfolio_allocation import (
    PortfolioAllocationInput,
    PortfolioAllocator,
    PortfolioAllocatorConfig,
    SignalAggregator,
)
from bithumb_bot.runtime_data_provider import (
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
from bithumb_bot.virtual_target_state import (
    StrategyVirtualTargetState,
    assert_not_live_submit_authority,
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
    pair_plan = PairExecutionPlan(
        pair="KRW-BTC",
        scope_key_hash=_scope().scope_key_hash(),
        portfolio_target_hash="sha256:" + "a" * 64,
        execution_submit_plan_hash="sha256:" + "b" * 64,
        idempotency_key="idem-btc",
        submit_authority_policy_hash="sha256:" + "c" * 64,
        pre_submit_risk_decision_hash="sha256:" + "d" * 64,
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
    with pytest.raises(TypeError, match="dict_only_execution_batch_not_authority"):
        reject_dict_only_batch_authority(batch.as_dict())


def test_virtual_target_state_is_independent_and_not_live_submit_authority() -> None:
    conn = _memory_conn()
    try:
        first = StrategyVirtualTargetState(
            strategy_instance_id="s1",
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
        with pytest.raises(TypeError, match="virtual_target_state_not_live_submit_authority"):
            assert_not_live_submit_authority(first)
    finally:
        conn.close()


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
