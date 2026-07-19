from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.derivatives.common import (
    AvailabilityTimes,
    DerivativeResearchError,
)
from market_research.research.derivatives.futures import (
    AdjustmentDirection,
    ContractQuote,
    ContinuousAdjustment,
    ContinuousFuturesPoint,
    ContinuousFuturesPolicy,
    ExpiryPolicy,
    FuturesContract,
    FuturesCostPolicy,
    FuturesLedger,
    FuturesOrderIntent,
    FuturesSimulator,
    FuturesStressCase,
    FuturesStressExecution,
    FuturesStressInputs,
    FuturesStressKind,
    MarginCallAction,
    MarginSimulationPolicy,
    MarketState,
    OrderSide,
    PhysicalDeliveryAction,
    RollExecution,
    RollPolicy,
    RollTrigger,
    SessionType,
    SettlementPolicy,
    SettlementType,
    SpreadExecution,
    run_futures_stress_case,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
HASH_D = "sha256:" + "d" * 64


def _availability(at: str) -> AvailabilityTimes:
    return AvailabilityTimes(
        event_at=at,
        published_at=at,
        provider_received_at=at,
        system_received_at=at,
        processed_at=at,
    )


def _contract(contract_id: str, *, last_trade: str) -> FuturesContract:
    return FuturesContract(
        contract_id=contract_id,
        root_id="FUT.ROOT",
        listing_date="2026-01-01",
        first_trade_date="2026-01-02",
        last_trade_date=last_trade,
        first_notice_date=None,
        final_settlement_date=last_trade,
        expiration_date=last_trade,
        contract_multiplier=Decimal("50"),
        tick_size=Decimal("0.25"),
        settlement_type=SettlementType.CASH_SETTLED,
        spec_effective_at="2026-01-01T00:00:00Z",
        spec_version="spec.v1",
        availability=_availability("2026-01-01T00:00:00Z"),
    )


def _quote(
    contract_id: str,
    *,
    price: str,
    limit_down: str,
    limit_up: str,
    bid: str,
    ask: str,
    volume: str = "100",
) -> ContractQuote:
    at = "2026-03-18T16:00:00Z"
    close = Decimal(price)
    return ContractQuote(
        quote_id=f"quote.{contract_id}",
        contract_id=contract_id,
        root_id="FUT.ROOT",
        observed_at=at,
        trading_date="2026-03-18",
        session=SessionType.NIGHT,
        session_sequence=1,
        open_price=close,
        high_price=close + Decimal("2"),
        low_price=close - Decimal("2"),
        close_price=close,
        settlement_price=close,
        volume=Decimal(volume),
        open_interest=Decimal("1000"),
        availability=_availability(at),
        source_hash=HASH_A,
        market_state=MarketState.OPEN,
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
        limit_up_price=Decimal(limit_up),
        limit_down_price=Decimal(limit_down),
    )


def _fixture() -> tuple[
    FuturesStressInputs,
    RollPolicy,
    ContinuousFuturesPolicy,
]:
    near = _contract("FUT.202603", last_trade="2026-03-20")
    deferred = _contract("FUT.202606", last_trade="2026-06-20")
    near_quote = _quote(
        near.contract_id,
        price="100",
        limit_down="90",
        limit_up="110",
        bid="99.5",
        ask="100.5",
    )
    deferred_quote = _quote(
        deferred.contract_id,
        price="102",
        limit_down="92",
        limit_up="112",
        bid="101.5",
        ask="102.5",
    )
    simulator = FuturesSimulator(
        simulator_id="futures.stress.simulator",
        simulator_version="v1",
        contracts=(near, deferred),
        settlement_policy=SettlementPolicy(
            policy_id="settlement.daily",
            policy_version="v1",
            settlement_price_field="settlement_price",
            daily_mark_to_market=True,
            realize_variation_margin_daily=True,
        ),
        margin_policy=MarginSimulationPolicy(
            policy_id="margin.research",
            policy_version="v1",
            initial_margin_per_contract=Decimal("1000"),
            maintenance_margin_per_contract=Decimal("800"),
            collateral_fraction=Decimal("1"),
            margin_call_action=MarginCallAction.BLOCK_NEW_TRADES,
        ),
        expiry_policy=ExpiryPolicy(
            policy_id="expiry.safe",
            policy_version="v1",
            exit_days_before_first_notice=1,
            exit_days_before_last_trade=1,
            physical_delivery_action=PhysicalDeliveryAction.FAIL_RESEARCH,
        ),
        cost_policy=FuturesCostPolicy(
            policy_id="cost.stress",
            policy_version="v1",
            commission_per_contract=Decimal("1"),
            execution_slippage_ticks=Decimal("1"),
            roll_slippage_ticks=Decimal("1"),
            spread_legging_ticks=Decimal("2"),
        ),
    )
    opened = simulator.execute(
        FuturesLedger.open("ledger.stress", Decimal("3000")),
        FuturesOrderIntent(
            intent_id="intent.open",
            contract_id=near.contract_id,
            side=OrderSide.BUY,
            quantity=2,
            decision_at=near_quote.observed_at,
        ),
        near_quote,
        fill_id="fill.open",
        step_id="step.open",
    )
    roll_policy = RollPolicy(
        policy_id="roll.days",
        policy_version="v1",
        trigger=RollTrigger.DAYS_BEFORE_LAST_TRADE,
        days_before_last_trade=1,
    )
    continuous_policy = ContinuousFuturesPolicy(
        series_id="continuous.FUT",
        root_id="FUT.ROOT",
        policy_version="v1",
        roll_policy_hash=roll_policy.content_hash,
        adjustment=ContinuousAdjustment.DIFFERENCE,
        adjustment_direction=AdjustmentDirection.FORWARD,
    )
    point = ContinuousFuturesPoint(
        point_id="continuous.point",
        series_id=continuous_policy.series_id,
        root_id="FUT.ROOT",
        observed_at=near_quote.observed_at,
        source_contract_id=near.contract_id,
        source_quote_hash=near_quote.content_hash,
        source_price=near_quote.close_price,
        continuous_price=Decimal("102"),
        additive_adjustment=Decimal("2"),
        multiplicative_adjustment=Decimal("1.02"),
        roll_gap=Decimal("2"),
        policy_hash=continuous_policy.content_hash,
        roll_decision_hash=HASH_B,
        chain_snapshot_hash=HASH_C,
        previous_point_hash=None,
    )
    roll_execution = RollExecution(
        execution_id="roll.execution",
        decision_hash=HASH_B,
        executed_at=near_quote.observed_at,
        from_contract_id=near.contract_id,
        to_contract_id=deferred.contract_id,
        close_fill_hash=HASH_C,
        open_fill_hash=HASH_D,
        close_cost=Decimal("5"),
        open_cost=Decimal("7"),
        price_gap=Decimal("2"),
        roll_yield=Decimal("-200"),
    )
    spread_execution = SpreadExecution(
        execution_id="spread.execution",
        spread_order_hash=HASH_B,
        fill_hashes=(HASH_C, HASH_D),
        simultaneous_fill=False,
        legging_cost=Decimal("20"),
        basis_risk_flag=True,
    )
    return (
        FuturesStressInputs(
            input_id="stress.inputs",
            input_version="v1",
            simulator=simulator,
            ledger=opened.ledger,
            marks=(near_quote, deferred_quote),
            as_of=near_quote.observed_at,
            selected_contract_id=near.contract_id,
            alternate_contract_id=deferred.contract_id,
            roll_policy=roll_policy,
            continuous_policy=continuous_policy,
            continuous_point=point,
            roll_executions=(roll_execution,),
            spread_executions=(spread_execution,),
        ),
        roll_policy,
        continuous_policy,
    )


def _case(
    kind: FuturesStressKind,
    inputs: FuturesStressInputs,
    roll_policy: RollPolicy,
    continuous_policy: ContinuousFuturesPolicy,
) -> FuturesStressCase:
    hashes = [inputs.simulator.content_hash]
    if kind is FuturesStressKind.ROLL_POLICY:
        hashes.append(roll_policy.content_hash)
    elif kind is FuturesStressKind.CONTINUOUS_ADJUSTMENT:
        hashes.append(continuous_policy.content_hash)
    elif kind in {
        FuturesStressKind.ROLL_COST,
        FuturesStressKind.HIGH_VOL_LOW_LIQUIDITY,
        FuturesStressKind.NIGHT_SESSION,
        FuturesStressKind.SPREAD_LEGGING,
    }:
        hashes.append(inputs.simulator.cost_policy.content_hash)
    elif kind is FuturesStressKind.NEAR_EXPIRY_EXCLUSION:
        hashes.extend(
            (
                inputs.simulator.expiry_policy.content_hash,
                inputs.simulator.cost_policy.content_hash,
            )
        )
    elif kind is FuturesStressKind.MARGIN_INCREASE:
        hashes.append(inputs.simulator.margin_policy.content_hash)
    return FuturesStressCase(
        case_id=f"stress.{kind.value}",
        case_version="v1",
        kind=kind,
        scalar=Decimal("2"),
        baseline_policy_hashes=tuple(hashes),
    )


def test_executor_runs_all_twelve_declared_stress_kinds_deterministically() -> None:
    inputs, roll_policy, continuous_policy = _fixture()
    executions: list[FuturesStressExecution] = []

    for kind in FuturesStressKind:
        case = _case(kind, inputs, roll_policy, continuous_policy)
        first = run_futures_stress_case(
            case,
            inputs,
            execution_id=f"execution.{kind.value}",
        )
        repeated = run_futures_stress_case(
            case,
            inputs,
            execution_id=f"execution.{kind.value}",
        )
        assert first.content_hash == repeated.content_hash
        assert first.result.content_hash == repeated.result.content_hash
        assert first.case_hash == case.content_hash
        assert first.input_hash == inputs.content_hash
        assert first.result.baseline_equity == Decimal("2948.00")
        assert first.scenario_hash in first.evidence_hashes
        executions.append(first)

    assert {item.result.diagnostics[1] for item in executions} == {
        item.value for item in FuturesStressKind
    }
    by_kind = {
        item.result.diagnostics[1]: item.result for item in executions
    }
    assert by_kind[FuturesStressKind.MARGIN_INCREASE.value].margin_call_count == 1
    assert (
        by_kind[FuturesStressKind.PRICE_LIMIT_NO_EXIT.value].blocked_exit_count
        == 1
    )
    assert (
        by_kind[FuturesStressKind.NEAR_EXPIRY_EXCLUSION.value].stressed_equity
        < by_kind[FuturesStressKind.NEAR_EXPIRY_EXCLUSION.value].baseline_equity
    )
    assert all(
        result.stressed_equity <= result.baseline_equity
        for result in by_kind.values()
    )


def test_stress_results_are_bound_to_case_inputs_and_scenario_hashes() -> None:
    inputs, roll_policy, continuous_policy = _fixture()
    case = _case(
        FuturesStressKind.CURVE_REGIME,
        inputs,
        roll_policy,
        continuous_policy,
    )
    execution = run_futures_stress_case(
        case, inputs, execution_id="execution.curve"
    )
    changed_case = replace(case, scalar=Decimal("3"))
    changed = run_futures_stress_case(
        changed_case, inputs, execution_id="execution.curve.changed"
    )

    assert changed.case_hash != execution.case_hash
    assert changed.scenario_hash != execution.scenario_hash
    assert changed.result.stressed_equity < execution.result.stressed_equity
    with pytest.raises(
        DerivativeResearchError,
        match="futures_stress_execution_result_case_mismatch",
    ):
        replace(execution, case_hash=changed_case.content_hash)


def test_executor_fails_closed_on_policy_pit_and_kind_specific_evidence() -> None:
    inputs, roll_policy, continuous_policy = _fixture()
    roll_case = _case(
        FuturesStressKind.ROLL_POLICY,
        inputs,
        roll_policy,
        continuous_policy,
    )
    unbound = replace(
        roll_case,
        baseline_policy_hashes=(inputs.simulator.content_hash,),
    )
    with pytest.raises(
        DerivativeResearchError,
        match="futures_stress_required_policy_hash_missing",
    ):
        run_futures_stress_case(
            unbound, inputs, execution_id="execution.unbound"
        )

    with pytest.raises(
        DerivativeResearchError,
        match="futures_stress_mark_not_available_as_of",
    ):
        replace(inputs, as_of="2026-03-18T15:59:59Z")

    illiquid_case = _case(
        FuturesStressKind.HIGH_VOL_LOW_LIQUIDITY,
        inputs,
        roll_policy,
        continuous_policy,
    )
    near_mark, deferred_mark = inputs.marks
    no_book = replace(near_mark, bid_price=None, ask_price=None)
    missing_book = replace(
        inputs,
        marks=(no_book, deferred_mark),
        continuous_point=None,
    )
    with pytest.raises(
        DerivativeResearchError,
        match="futures_stress_bid_ask_required",
    ):
        run_futures_stress_case(
            illiquid_case,
            missing_book,
            execution_id="execution.no.book",
        )

    low_scalar = replace(illiquid_case, scalar=Decimal("0.5"))
    with pytest.raises(
        DerivativeResearchError,
        match="futures_stress_scalar_below_baseline",
    ):
        run_futures_stress_case(
            low_scalar, inputs, execution_id="execution.low.scalar"
        )


def test_price_limit_direction_and_spread_execution_are_not_inferred() -> None:
    inputs, roll_policy, continuous_policy = _fixture()
    price_case = _case(
        FuturesStressKind.PRICE_LIMIT_NO_EXIT,
        inputs,
        roll_policy,
        continuous_policy,
    )
    near_mark, deferred_mark = inputs.marks
    no_adverse_limit = replace(near_mark, limit_down_price=None)
    missing_limit = replace(
        inputs,
        marks=(no_adverse_limit, deferred_mark),
        continuous_point=None,
    )
    with pytest.raises(
        DerivativeResearchError,
        match="futures_stress_adverse_price_limit_required",
    ):
        run_futures_stress_case(
            price_case,
            missing_limit,
            execution_id="execution.no.limit",
        )

    spread_case = _case(
        FuturesStressKind.SPREAD_LEGGING,
        inputs,
        roll_policy,
        continuous_policy,
    )
    without_spread = replace(inputs, spread_executions=())
    with pytest.raises(
        DerivativeResearchError,
        match="futures_stress_spread_execution_evidence_required",
    ):
        run_futures_stress_case(
            spread_case,
            without_spread,
            execution_id="execution.no.spread",
        )
