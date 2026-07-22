from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.derivatives.common import (
    AvailabilityTimes,
    DerivativeResearchError,
    QualityDecision,
    QualityResult,
    RunType,
)
from market_research.research.derivatives.futures import (
    AdjustmentDirection,
    CompositeOperator,
    ContractChainSnapshot,
    ContractQuote,
    ContinuousAdjustment,
    ContinuousFuturesPoint,
    ContinuousFuturesPolicy,
    ExpiryPolicy,
    FuturesContract,
    FuturesCostPolicy,
    FuturesLedger,
    FuturesLifecycleEvent,
    FuturesOrderIntent,
    FuturesRiskSummary,
    FuturesRobustnessSummary,
    FuturesRoot,
    FuturesSimulator,
    FuturesSpreadOrder,
    FuturesStressCase,
    FuturesStressKind,
    FuturesStressResult,
    LifecycleEventType,
    MarginCallAction,
    MarginSimulationPolicy,
    MarketState,
    OrderSide,
    PhysicalDeliveryAction,
    ProspectiveFuturesEvidence,
    RollDecision,
    RollPolicy,
    RollTrigger,
    SessionType,
    SettlementPolicy,
    SettlementType,
    SpreadLeg,
    attribute_roll_return,
    build_continuous_point,
    compute_basis_feature,
    compute_curve_feature,
    decide_roll,
    select_chain_as_of,
    summarize_futures_risk,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _availability(at: str) -> AvailabilityTimes:
    return AvailabilityTimes(
        event_at=at,
        published_at=at,
        provider_received_at=at,
        system_received_at=at,
        processed_at=at,
    )


def _delayed_availability(event_at: str) -> AvailabilityTimes:
    return AvailabilityTimes(
        event_at=event_at,
        published_at="2026-03-11T16:01:00Z",
        provider_received_at="2026-03-11T16:02:00Z",
        system_received_at="2026-03-11T16:03:00Z",
        processed_at="2026-03-11T16:04:00Z",
    )


def _contract(
    contract_id: str,
    *,
    last_trade: str,
    final_settlement: str | None = None,
    expiration: str | None = None,
    settlement_type: SettlementType = SettlementType.CASH_SETTLED,
    first_notice: str | None = None,
    multiplier: Decimal = Decimal("50"),
    tick: Decimal = Decimal("0.25"),
) -> FuturesContract:
    final = final_settlement or last_trade
    expiry = expiration or final
    notice = first_notice
    if settlement_type is SettlementType.PHYSICAL_SETTLED and notice is None:
        notice = "2026-03-15"
    return FuturesContract(
        contract_id=contract_id,
        root_id="FUT.ROOT",
        listing_date="2026-01-01",
        first_trade_date="2026-01-02",
        last_trade_date=last_trade,
        first_notice_date=notice,
        final_settlement_date=final,
        expiration_date=expiry,
        contract_multiplier=multiplier,
        tick_size=tick,
        settlement_type=settlement_type,
        spec_effective_at="2026-01-01T00:00:00Z",
        spec_version="spec.v1",
        availability=_availability("2026-01-01T00:00:00Z"),
    )


def _quote(
    contract_id: str,
    at: str,
    price: str,
    *,
    settlement: str | None = None,
    volume: str = "100",
    open_interest: str = "1000",
    session: SessionType = SessionType.DAY,
    session_sequence: int = 1,
    trading_date: str | None = None,
    market_state: MarketState = MarketState.OPEN,
    limit_up: str | None = None,
    limit_down: str | None = None,
) -> ContractQuote:
    close = Decimal(price)
    settle = close if settlement is None else Decimal(settlement)
    low = min(close, settle) - Decimal("1")
    high = max(close, settle) + Decimal("1")
    return ContractQuote(
        quote_id=f"quote.{contract_id}.{at.replace(':', '').replace('-', '')}",
        contract_id=contract_id,
        root_id="FUT.ROOT",
        observed_at=at,
        trading_date=trading_date or at[:10],
        session=session,
        session_sequence=session_sequence,
        open_price=close,
        high_price=high,
        low_price=low,
        close_price=close,
        settlement_price=settle,
        volume=Decimal(volume),
        open_interest=Decimal(open_interest),
        availability=_availability(at),
        source_hash=HASH_A,
        market_state=market_state,
        limit_up_price=None if limit_up is None else Decimal(limit_up),
        limit_down_price=None if limit_down is None else Decimal(limit_down),
    )


def _chain(
    snapshot_id: str,
    at: str,
    contracts: tuple[FuturesContract, ...],
    quotes: tuple[ContractQuote, ...],
) -> ContractChainSnapshot:
    return ContractChainSnapshot(
        snapshot_id=snapshot_id,
        root_id="FUT.ROOT",
        observed_at=at,
        availability=_availability(at),
        contracts=contracts,
        quotes=quotes,
        lifecycle_events=(),
        quality_results=(
            QualityResult(
                check_id="futures.chain.complete",
                check_version="v1",
                decision=QualityDecision.PASS,
            ),
        ),
        source_manifest_hashes=(HASH_A,),
    )


def _policies(
    *,
    margin_action: MarginCallAction = MarginCallAction.BLOCK_NEW_TRADES,
    commission: str = "1",
    roll_ticks: str = "1",
    legging_ticks: str = "0.5",
) -> tuple[
    SettlementPolicy,
    MarginSimulationPolicy,
    ExpiryPolicy,
    FuturesCostPolicy,
]:
    return (
        SettlementPolicy(
            policy_id="settlement.daily",
            policy_version="v1",
            settlement_price_field="settlement_price",
            daily_mark_to_market=True,
            realize_variation_margin_daily=True,
            collateral_annual_rate=Decimal("0.03"),
        ),
        MarginSimulationPolicy(
            policy_id="margin.research",
            policy_version="v1",
            initial_margin_per_contract=Decimal("1000"),
            maintenance_margin_per_contract=Decimal("800"),
            collateral_fraction=Decimal("1"),
            margin_call_action=margin_action,
        ),
        ExpiryPolicy(
            policy_id="expiry.safe",
            policy_version="v1",
            exit_days_before_first_notice=0,
            exit_days_before_last_trade=0,
            physical_delivery_action=PhysicalDeliveryAction.FAIL_RESEARCH,
        ),
        FuturesCostPolicy(
            policy_id="cost.futures",
            policy_version="v1",
            commission_per_contract=Decimal(commission),
            execution_slippage_ticks=Decimal("0"),
            roll_slippage_ticks=Decimal(roll_ticks),
            spread_legging_ticks=Decimal(legging_ticks),
        ),
    )


def _simulator(
    contracts: tuple[FuturesContract, ...],
    *,
    margin_action: MarginCallAction = MarginCallAction.BLOCK_NEW_TRADES,
    commission: str = "1",
) -> FuturesSimulator:
    settlement, margin, expiry, costs = _policies(
        margin_action=margin_action, commission=commission
    )
    return FuturesSimulator(
        simulator_id="futures.sim",
        simulator_version="v1",
        contracts=contracts,
        settlement_policy=settlement,
        margin_policy=margin,
        expiry_policy=expiry,
        cost_policy=costs,
    )


def _market_fixture() -> tuple[
    FuturesContract,
    FuturesContract,
    ContractChainSnapshot,
    ContractChainSnapshot,
]:
    near = _contract("FUT.202603", last_trade="2026-03-20")
    deferred = _contract("FUT.202606", last_trade="2026-06-20")
    first_at = "2026-03-10T16:00:00Z"
    second_at = "2026-03-11T16:00:00Z"
    first = _chain(
        "chain.20260310",
        first_at,
        (near, deferred),
        (
            _quote(near.contract_id, first_at, "100", volume="100"),
            _quote(deferred.contract_id, first_at, "102", volume="80"),
        ),
    )
    second = _chain(
        "chain.20260311",
        second_at,
        (near, deferred),
        (
            _quote(near.contract_id, second_at, "101", volume="90"),
            _quote(deferred.contract_id, second_at, "103", volume="120"),
        ),
    )
    return near, deferred, first, second


def _volume_roll_policy() -> RollPolicy:
    return RollPolicy(
        policy_id="roll.volume",
        policy_version="v1",
        trigger=RollTrigger.VOLUME_CROSSOVER,
        crossover_ratio=Decimal("1"),
        consecutive_observations=1,
    )


def test_first_class_root_contract_dates_specs_and_exact_decimal() -> None:
    root = FuturesRoot(
        root_id="FUT.ROOT",
        symbol="FUT",
        exchange_id="XKRX",
        underlying_id="INDEX.K200",
        quote_currency="KRW",
        calendar_id="XKRX.FUT",
        settlement_type=SettlementType.CASH_SETTLED,
        root_version="v1",
    )
    contract = _contract("FUT.202603", last_trade="2026-03-20")

    assert root.root_id != contract.contract_id
    assert contract.listing_date == "2026-01-01"
    assert contract.first_trade_date == "2026-01-02"
    assert contract.last_trade_date == "2026-03-20"
    assert contract.first_notice_date is None
    assert contract.final_settlement_date == "2026-03-20"
    assert contract.expiration_date == "2026-03-20"
    assert contract.contract_multiplier == Decimal("50")
    assert contract.tick_size == Decimal("0.25")
    assert root.content_hash.startswith("sha256:")
    assert replace(contract).content_hash == contract.content_hash

    with pytest.raises(
        DerivativeResearchError,
        match="contract_multiplier_must_be_decimal_text_or_integer",
    ):
        replace(contract, contract_multiplier=50.0)  # type: ignore[arg-type]
    with pytest.raises(
        DerivativeResearchError,
        match="physical_futures_first_notice_date_required",
    ):
        replace(
            contract,
            settlement_type=SettlementType.PHYSICAL_SETTLED,
            first_notice_date=None,
        )


def test_chain_selection_and_volume_roll_are_strictly_point_in_time() -> None:
    near, _, first, second = _market_fixture()
    policy = _volume_roll_policy()

    selected = select_chain_as_of((first, second), first.observed_at)
    early = decide_roll(
        (first, second),
        policy,
        as_of=first.observed_at,
        current_contract_id=near.contract_id,
        decision_id="decision.early",
    )
    later = decide_roll(
        (first, second),
        policy,
        as_of=second.observed_at,
        current_contract_id=near.contract_id,
        decision_id="decision.later",
    )

    assert selected.content_hash == first.content_hash
    assert not early.should_roll
    assert early.to_contract_id == near.contract_id
    assert later.should_roll
    assert later.to_contract_id == "FUT.202606"
    assert second.quotes[1].content_hash not in early.input_quote_hashes
    assert later.chain_snapshot_hash == second.content_hash
    first.admit(RunType.CONFIRMATORY)

    with pytest.raises(
        DerivativeResearchError, match="contract_chain_not_available_as_of"
    ):
        select_chain_as_of((second,), first.observed_at)
    with pytest.raises(
        DerivativeResearchError, match="future_roll_observations_must_be_forbidden"
    ):
        replace(policy, forbid_future_observations=False)


def test_contract_chain_structurally_binds_quote_and_lifecycle_sources() -> None:
    near, _deferred, chain, _later = _market_fixture()

    with pytest.raises(
        DerivativeResearchError, match="contract_chain_quote_source_unbound"
    ):
        replace(
            chain,
            quotes=(
                replace(chain.quotes[0], source_hash=HASH_B),
                chain.quotes[1],
            ),
        )

    unbound_lifecycle = FuturesLifecycleEvent(
        event_id="lifecycle.unbound.source",
        contract_id=near.contract_id,
        event_type=LifecycleEventType.LISTED,
        event_at=chain.observed_at,
        availability=_availability(chain.observed_at),
        source_hash=HASH_B,
    )
    with pytest.raises(
        DerivativeResearchError, match="contract_chain_lifecycle_source_unbound"
    ):
        replace(chain, lifecycle_events=(unbound_lifecycle,))


def test_contract_chain_rejects_lifecycle_unknown_at_snapshot_time() -> None:
    near, _deferred, chain, _later = _market_fixture()
    future_at = "2026-03-11T16:00:00Z"
    future_lifecycle = FuturesLifecycleEvent(
        event_id="lifecycle.future.knowledge",
        contract_id=near.contract_id,
        event_type=LifecycleEventType.FINAL_SETTLEMENT,
        event_at=future_at,
        availability=_availability(future_at),
        source_hash=HASH_A,
    )

    with pytest.raises(
        DerivativeResearchError,
        match="contract_chain_lifecycle_not_known_at_snapshot",
    ):
        replace(chain, lifecycle_events=(future_lifecycle,))


def test_continuous_series_is_append_only_signal_mapping_not_an_instrument() -> None:
    near, deferred, first, second = _market_fixture()
    roll_policy = _volume_roll_policy()
    continuous_policy = ContinuousFuturesPolicy(
        series_id="continuous.FUT",
        root_id="FUT.ROOT",
        policy_version="v1",
        roll_policy_hash=roll_policy.content_hash,
        adjustment=ContinuousAdjustment.DIFFERENCE,
        adjustment_direction=AdjustmentDirection.FORWARD,
    )
    first_point, first_decision = build_continuous_point(
        (first, second),
        roll_policy,
        continuous_policy,
        as_of=first.observed_at,
        current_contract_id=near.contract_id,
        point_id="point.1",
        decision_id="point.decision.1",
        prospective=True,
    )
    second_point, second_decision = build_continuous_point(
        (first, second),
        roll_policy,
        continuous_policy,
        as_of=second.observed_at,
        current_contract_id=near.contract_id,
        point_id="point.2",
        decision_id="point.decision.2",
        previous_point=first_point,
        prospective=True,
    )

    assert first_point.source_contract_id == near.contract_id
    assert second_point.source_contract_id == deferred.contract_id
    assert second_point.source_price == Decimal("103")
    assert second_point.roll_gap == Decimal("2")
    assert second_point.additive_adjustment == Decimal("-2")
    assert second_point.continuous_price == Decimal("101")
    assert second_point.previous_point_hash == first_point.content_hash
    assert first_decision.content_hash != second_decision.content_hash

    simulator = _simulator((near, deferred))
    with pytest.raises(
        DerivativeResearchError, match="continuous_futures_not_executable"
    ):
        simulator.execute_continuous(
            FuturesLedger.open("ledger.signal", Decimal("100000")), second_point
        )

    backward = replace(
        continuous_policy,
        adjustment_direction=AdjustmentDirection.BACKWARD,
    )
    with pytest.raises(
        DerivativeResearchError,
        match="backward_adjustment_would_rewrite_history",
    ):
        build_continuous_point(
            (first, second),
            roll_policy,
            backward,
            as_of=first.observed_at,
            current_contract_id=near.contract_id,
            point_id="point.bad",
            decision_id="point.decision.bad",
            prospective=True,
        )


def test_basis_curve_and_roll_attribution_keep_distinct_returns() -> None:
    near, deferred, first, second = _market_fixture()
    policy = _volume_roll_policy()
    continuous_policy = ContinuousFuturesPolicy(
        series_id="continuous.FUT",
        root_id="FUT.ROOT",
        policy_version="v1",
        roll_policy_hash=policy.content_hash,
        adjustment=ContinuousAdjustment.DIFFERENCE,
        adjustment_direction=AdjustmentDirection.FORWARD,
    )
    previous, _ = build_continuous_point(
        (first, second),
        policy,
        continuous_policy,
        as_of=first.observed_at,
        current_contract_id=near.contract_id,
        point_id="feature.point.1",
        decision_id="feature.decision.1",
    )
    current, _ = build_continuous_point(
        (first, second),
        policy,
        continuous_policy,
        as_of=second.observed_at,
        current_contract_id=near.contract_id,
        point_id="feature.point.2",
        decision_id="feature.decision.2",
        previous_point=previous,
    )
    near_quote = second.quote_for(near.contract_id, second.observed_at)
    deferred_quote = second.quote_for(deferred.contract_id, second.observed_at)
    basis = compute_basis_feature(
        feature_id="basis.1",
        feature_version="v1",
        as_of=second.observed_at,
        spot_price=Decimal("100"),
        spot_availability=_availability(second.observed_at),
        futures_quote=near_quote,
        contract=near,
    )
    curve = compute_curve_feature(
        feature_id="curve.1",
        feature_version="v1",
        as_of=second.observed_at,
        near_quote=near_quote,
        deferred_quote=deferred_quote,
        near_contract=near,
        deferred_contract=deferred,
    )
    attribution = attribute_roll_return(
        feature_id="roll.attribute.1",
        feature_version="v1",
        previous_point=previous,
        current_point=current,
        old_contract_quote_at_roll=near_quote,
        settlement_return=Decimal("0.009"),
        execution_return=Decimal("0.008"),
    )

    assert basis.basis == Decimal("1")
    assert basis.basis_ratio == Decimal("0.01")
    assert curve.calendar_spread == Decimal("2")
    assert curve.annualized_slope > 0
    assert attribution.continuous_return == Decimal("0.01")
    assert attribution.contract_price_return == Decimal("0.01")
    assert attribution.roll_return == 0
    assert attribution.settlement_return != attribution.execution_return

    skewed_spot = _availability("2026-03-11T15:59:59Z")
    with pytest.raises(DerivativeResearchError, match="basis_inputs_not_time_aligned"):
        compute_basis_feature(
            feature_id="basis.bad",
            feature_version="v1",
            as_of=second.observed_at,
            spot_price=Decimal("100"),
            spot_availability=skewed_spot,
            futures_quote=near_quote,
            contract=near,
        )


def test_simulator_applies_tick_multiplier_long_short_and_daily_settlement() -> None:
    contract = _contract("FUT.202603", last_trade="2026-03-20")
    simulator = _simulator((contract,))
    ledger = FuturesLedger.open("ledger.pnl", Decimal("100000"))
    open_quote = _quote(contract.contract_id, "2026-03-10T16:00:00Z", "100.12")
    opened = simulator.execute(
        ledger,
        FuturesOrderIntent(
            intent_id="intent.long",
            contract_id=contract.contract_id,
            side=OrderSide.BUY,
            quantity=2,
            decision_at=open_quote.observed_at,
        ),
        open_quote,
        fill_id="fill.long",
        step_id="step.long",
    )
    assert opened.fills[0].fill_price == Decimal("100.25")
    assert opened.fills[0].slippage_cost == Decimal("13")
    assert opened.ledger.position_for(contract.contract_id).quantity == 2  # type: ignore[union-attr]
    assert opened.ledger.cash_balance == Decimal("99985")

    settle_quote = _quote(
        contract.contract_id,
        "2026-03-11T16:00:00Z",
        "101.25",
        settlement="101.25",
    )
    settle_quote = replace(
        settle_quote,
        availability=_delayed_availability(settle_quote.observed_at),
    )
    settled = simulator.settle_daily(
        opened.ledger,
        settle_quote,
        event_id="settle.1",
        step_id="step.settle.1",
    )
    assert settled.settlement_events[0].variation_margin == Decimal("100")
    assert settled.ledger.cash_balance == Decimal("100085")
    assert settled.ledger.cumulative_variation_margin == Decimal("100")

    flip_quote = _quote(
        contract.contract_id,
        "2026-03-11T16:05:00Z",
        "101.25",
    )
    flipped = simulator.execute(
        settled.ledger,
        FuturesOrderIntent(
            intent_id="intent.flip",
            contract_id=contract.contract_id,
            side=OrderSide.SELL,
            quantity=3,
            decision_at=flip_quote.observed_at,
        ),
        flip_quote,
        fill_id="fill.flip",
        step_id="step.flip",
    )
    position = flipped.ledger.position_for(contract.contract_id)
    assert position is not None and position.quantity == -1
    assert flipped.fills[0].multiplier == Decimal("50")


@pytest.mark.parametrize(
    ("action", "positions_left", "blocked", "failed"),
    [
        (MarginCallAction.REDUCE_POSITION, 1, True, False),
        (MarginCallAction.VIRTUAL_MARGIN_CALL, 1, True, False),
        (MarginCallAction.BLOCK_NEW_TRADES, 1, True, False),
        (MarginCallAction.FAIL_RESEARCH, 1, False, True),
    ],
)
def test_daily_loss_triggers_each_typed_margin_shortfall_policy(
    action: MarginCallAction,
    positions_left: int,
    blocked: bool,
    failed: bool,
) -> None:
    contract = _contract("FUT.202603", last_trade="2026-03-20")
    simulator = _simulator((contract,), margin_action=action, commission="0")
    ledger = FuturesLedger.open(f"ledger.margin.{action.value}", Decimal("2000"))
    open_quote = _quote(contract.contract_id, "2026-03-10T16:00:00Z", "100")
    opened = simulator.execute(
        ledger,
        FuturesOrderIntent(
            intent_id=f"intent.margin.{action.value}",
            contract_id=contract.contract_id,
            side=OrderSide.BUY,
            quantity=1,
            decision_at=open_quote.observed_at,
        ),
        open_quote,
        fill_id=f"fill.margin.{action.value}",
        step_id=f"step.margin.open.{action.value}",
    )
    loss_quote = _quote(
        contract.contract_id,
        "2026-03-11T16:00:00Z",
        "70",
        settlement="70",
    )
    result = simulator.settle_daily(
        opened.ledger,
        loss_quote,
        event_id=f"settle.margin.{action.value}",
        step_id=f"step.margin.{action.value}",
    )

    assert result.settlement_events[0].variation_margin == Decimal("-1500")
    assert result.margin_call is not None
    assert result.margin_call.action is action
    assert len(result.ledger.positions) == positions_left
    assert result.ledger.blocked_new_trades is blocked
    assert result.ledger.failed is failed
    assert result.ledger.margin_call_count == 1


def test_scale_in_uses_weighted_settlement_basis_for_new_contracts() -> None:
    contract = _contract("FUT.202603", last_trade="2026-03-20")
    simulator = _simulator((contract,), commission="0")
    ledger = FuturesLedger.open("ledger.scale-in", Decimal("100000"))
    first_quote = _quote(contract.contract_id, "2026-03-10T16:00:00Z", "100")
    first = simulator.execute(
        ledger,
        FuturesOrderIntent(
            intent_id="intent.scale-in.first",
            contract_id=contract.contract_id,
            side=OrderSide.BUY,
            quantity=1,
            decision_at=first_quote.observed_at,
        ),
        first_quote,
        fill_id="fill.scale-in.first",
        step_id="step.scale-in.first",
    )
    second_quote = _quote(contract.contract_id, "2026-03-11T15:00:00Z", "110")
    second = simulator.execute(
        first.ledger,
        FuturesOrderIntent(
            intent_id="intent.scale-in.second",
            contract_id=contract.contract_id,
            side=OrderSide.BUY,
            quantity=1,
            decision_at=second_quote.observed_at,
        ),
        second_quote,
        fill_id="fill.scale-in.second",
        step_id="step.scale-in.second",
    )
    position = second.ledger.position_for(contract.contract_id)
    assert position is not None
    assert position.last_settlement_price == Decimal("105")

    settlement_quote = _quote(
        contract.contract_id,
        "2026-03-11T16:00:00Z",
        "111",
        settlement="111",
        session_sequence=2,
    )
    settled = simulator.settle_daily(
        second.ledger,
        settlement_quote,
        event_id="settle.scale-in",
        step_id="step.scale-in.settle",
    )

    # (111-100)*1 + (111-110)*1, multiplied by 50.
    assert settled.settlement_events[0].variation_margin == Decimal("600")


def test_reduce_position_margin_action_requires_an_explicit_costed_fill() -> None:
    contract = _contract("FUT.202603", last_trade="2026-03-20")
    simulator = _simulator(
        (contract,),
        margin_action=MarginCallAction.REDUCE_POSITION,
        commission="2",
    )
    ledger = FuturesLedger.open("ledger.explicit-reduction", Decimal("2000"))
    open_quote = _quote(contract.contract_id, "2026-03-10T16:00:00Z", "100")
    opened = simulator.execute(
        ledger,
        FuturesOrderIntent(
            intent_id="intent.explicit-reduction.open",
            contract_id=contract.contract_id,
            side=OrderSide.BUY,
            quantity=1,
            decision_at=open_quote.observed_at,
        ),
        open_quote,
        fill_id="fill.explicit-reduction.open",
        step_id="step.explicit-reduction.open",
    )
    loss_quote = _quote(
        contract.contract_id,
        "2026-03-11T16:00:00Z",
        "70",
        settlement="70",
    )
    called = simulator.settle_daily(
        opened.ledger,
        loss_quote,
        event_id="settle.explicit-reduction",
        step_id="step.explicit-reduction.margin",
    )

    assert called.ledger.position_for(contract.contract_id) is not None
    assert called.ledger.blocked_new_trades is True
    close_quote = _quote(
        contract.contract_id,
        "2026-03-12T16:00:00Z",
        "69",
    )
    reduced = simulator.execute(
        called.ledger,
        FuturesOrderIntent(
            intent_id="intent.explicit-reduction.close",
            contract_id=contract.contract_id,
            side=OrderSide.SELL,
            quantity=1,
            decision_at=close_quote.observed_at,
        ),
        close_quote,
        fill_id="fill.explicit-reduction.close",
        step_id="step.explicit-reduction.close",
    )

    assert reduced.ledger.positions == ()
    assert reduced.fills[0].commission == Decimal("2")
    assert reduced.fills[0].realized_trade_pnl == Decimal("-50")


def test_roll_is_two_actual_contract_fills_with_two_costs_and_roll_yield() -> None:
    near, deferred, first, second = _market_fixture()
    simulator = _simulator((near, deferred))
    ledger = FuturesLedger.open("ledger.roll", Decimal("100000"))
    first_near = first.quote_for(near.contract_id, first.observed_at)
    opened = simulator.execute(
        ledger,
        FuturesOrderIntent(
            intent_id="intent.roll.seed",
            contract_id=near.contract_id,
            side=OrderSide.BUY,
            quantity=1,
            decision_at=first.observed_at,
        ),
        first_near,
        fill_id="fill.roll.seed",
        step_id="step.roll.seed",
    )
    decision = decide_roll(
        (first, second),
        _volume_roll_policy(),
        as_of=second.observed_at,
        current_contract_id=near.contract_id,
        decision_id="decision.execute.roll",
    )
    old_quote = second.quote_for(near.contract_id, second.observed_at)
    new_quote = second.quote_for(deferred.contract_id, second.observed_at)
    rolled = simulator.roll(
        opened.ledger,
        decision,
        old_quote,
        new_quote,
        execution_id="roll.execution.1",
        step_id="step.roll.1",
    )

    assert len(rolled.fills) == 2
    assert rolled.fills[0].contract_id == near.contract_id
    assert rolled.fills[1].contract_id == deferred.contract_id
    assert all(fill.is_roll_leg for fill in rolled.fills)
    assert rolled.roll_execution is not None
    assert rolled.roll_execution.close_cost > 0
    assert rolled.roll_execution.open_cost > 0
    assert rolled.roll_execution.total_roll_cost == sum(
        (fill.total_cost for fill in rolled.fills), Decimal("0")
    )
    assert rolled.roll_execution.price_gap == Decimal("2")
    assert rolled.roll_execution.roll_yield == Decimal("-100")
    assert rolled.ledger.position_for(near.contract_id) is None
    assert rolled.ledger.position_for(deferred.contract_id) is not None

    future_bound = replace(
        decision,
        input_quote_hashes=(old_quote.content_hash, HASH_B),
    )
    with pytest.raises(DerivativeResearchError, match="quote_not_in_pit_decision"):
        simulator.roll(
            opened.ledger,
            future_bound,
            old_quote,
            new_quote,
            execution_id="roll.execution.bad",
            step_id="step.roll.bad",
        )


def test_expiration_uses_settlement_price_and_never_models_physical_delivery() -> None:
    cash_contract = _contract("FUT.CASH", last_trade="2026-03-20")
    physical_contract = _contract(
        "FUT.PHYSICAL",
        last_trade="2026-03-20",
        final_settlement="2026-03-25",
        expiration="2026-03-25",
        settlement_type=SettlementType.PHYSICAL_SETTLED,
        first_notice="2026-03-15",
    )
    simulator = _simulator((cash_contract, physical_contract), commission="0")
    cash_ledger = FuturesLedger.open("ledger.expiry.cash", Decimal("100000"))
    cash_open_quote = _quote(cash_contract.contract_id, "2026-03-19T16:00:00Z", "100")
    cash_opened = simulator.execute(
        cash_ledger,
        FuturesOrderIntent(
            intent_id="intent.expiry.cash",
            contract_id=cash_contract.contract_id,
            side=OrderSide.BUY,
            quantity=1,
            decision_at=cash_open_quote.observed_at,
        ),
        cash_open_quote,
        fill_id="fill.expiry.cash",
        step_id="step.expiry.cash.open",
    )
    final_quote = _quote(
        cash_contract.contract_id,
        "2026-03-20T16:00:00Z",
        "104",
        settlement="105",
    )
    expired = simulator.handle_expiration(
        cash_opened.ledger,
        final_quote,
        event_id="expiry.cash",
        step_id="step.expiry.cash",
    )
    assert expired.ledger.positions == ()
    assert expired.settlement_events[0].variation_margin == Decimal("250")
    assert expired.diagnostics == ("CASH_SETTLED_AT_FINAL_SETTLEMENT_PRICE",)

    physical_ledger = FuturesLedger.open("ledger.expiry.physical", Decimal("100000"))
    physical_open_quote = _quote(
        physical_contract.contract_id, "2026-03-10T16:00:00Z", "100"
    )
    physical_opened = simulator.execute(
        physical_ledger,
        FuturesOrderIntent(
            intent_id="intent.expiry.physical",
            contract_id=physical_contract.contract_id,
            side=OrderSide.BUY,
            quantity=1,
            decision_at=physical_open_quote.observed_at,
        ),
        physical_open_quote,
        fill_id="fill.expiry.physical",
        step_id="step.expiry.physical.open",
    )
    physical_final = _quote(
        physical_contract.contract_id, "2026-03-25T16:00:00Z", "105"
    )
    physical_result = simulator.handle_expiration(
        physical_opened.ledger,
        physical_final,
        event_id="expiry.physical",
        step_id="step.expiry.physical",
    )
    assert physical_result.ledger.failed
    assert physical_result.diagnostics == ("PHYSICAL_DELIVERY_NOT_SIMULATED",)

    cutoff_quote = _quote(physical_contract.contract_id, "2026-03-15T00:00:00Z", "101")
    with pytest.raises(DerivativeResearchError, match="open_after_expiry_exit_cutoff"):
        simulator.execute(
            FuturesLedger.open("ledger.cutoff", Decimal("100000")),
            FuturesOrderIntent(
                intent_id="intent.cutoff",
                contract_id=physical_contract.contract_id,
                side=OrderSide.BUY,
                quantity=1,
                decision_at=cutoff_quote.observed_at,
            ),
            cutoff_quote,
            fill_id="fill.cutoff",
            step_id="step.cutoff",
        )


def test_price_limits_halts_and_night_day_session_order_block_false_fills() -> None:
    contract = _contract("FUT.202603", last_trade="2026-03-20")
    simulator = _simulator((contract,))
    ledger = FuturesLedger.open("ledger.session", Decimal("100000"))
    limit_quote = _quote(
        contract.contract_id,
        "2026-03-10T00:00:00Z",
        "101",
        market_state=MarketState.LIMIT_UP,
        limit_up="101",
    )
    with pytest.raises(DerivativeResearchError, match="limit_up_buy_unavailable"):
        simulator.execute(
            ledger,
            FuturesOrderIntent(
                intent_id="intent.limit",
                contract_id=contract.contract_id,
                side=OrderSide.BUY,
                quantity=1,
                decision_at=limit_quote.observed_at,
            ),
            limit_quote,
            fill_id="fill.limit",
            step_id="step.limit",
        )

    halted = replace(limit_quote, market_state=MarketState.HALTED)
    with pytest.raises(DerivativeResearchError, match="futures_market_halted"):
        simulator.execute(
            ledger,
            FuturesOrderIntent(
                intent_id="intent.halt",
                contract_id=contract.contract_id,
                side=OrderSide.SELL,
                quantity=1,
                decision_at=halted.observed_at,
            ),
            halted,
            fill_id="fill.halt",
            step_id="step.halt",
        )

    night = _quote(
        contract.contract_id,
        "2026-03-10T01:00:00Z",
        "100",
        session=SessionType.NIGHT,
        session_sequence=0,
        trading_date="2026-03-10",
    )
    opened = simulator.execute(
        ledger,
        FuturesOrderIntent(
            intent_id="intent.night",
            contract_id=contract.contract_id,
            side=OrderSide.BUY,
            quantity=1,
            decision_at=night.observed_at,
        ),
        night,
        fill_id="fill.night",
        step_id="step.night",
    )
    day = _quote(
        contract.contract_id,
        "2026-03-10T06:00:00Z",
        "100.25",
        session=SessionType.DAY,
        session_sequence=1,
        trading_date="2026-03-10",
    )
    day_step = simulator.execute(
        opened.ledger,
        FuturesOrderIntent(
            intent_id="intent.day",
            contract_id=contract.contract_id,
            side=OrderSide.BUY,
            quantity=1,
            decision_at=day.observed_at,
        ),
        day,
        fill_id="fill.day",
        step_id="step.day",
    )
    reversed_night = _quote(
        contract.contract_id,
        "2026-03-10T07:00:00Z",
        "100.5",
        session=SessionType.NIGHT,
        session_sequence=0,
        trading_date="2026-03-10",
    )
    with pytest.raises(DerivativeResearchError, match="session_order_reversed"):
        simulator.execute(
            day_step.ledger,
            FuturesOrderIntent(
                intent_id="intent.reversed",
                contract_id=contract.contract_id,
                side=OrderSide.SELL,
                quantity=1,
                decision_at=reversed_night.observed_at,
            ),
            reversed_night,
            fill_id="fill.reversed",
            step_id="step.reversed",
        )


def test_calendar_spread_records_each_leg_cost_and_legging_basis_risk() -> None:
    near, deferred, first, _ = _market_fixture()
    simulator = _simulator((near, deferred))
    order = FuturesSpreadOrder(
        order_id="spread.order.1",
        spread_id="spread.calendar.1",
        legs=(
            SpreadLeg(contract_id=near.contract_id, ratio=1),
            SpreadLeg(contract_id=deferred.contract_id, ratio=-1),
        ),
        units=1,
        decision_at=first.observed_at,
        simultaneous_fill=False,
    )
    step, execution = simulator.execute_spread(
        FuturesLedger.open("ledger.spread", Decimal("100000")),
        order,
        first.quotes,
        execution_id="spread.execution.1",
        step_id="step.spread.1",
    )

    assert len(step.fills) == 2
    assert {item.quantity for item in step.ledger.positions} == {-1, 1}
    assert execution.basis_risk_flag
    assert not execution.simultaneous_fill
    assert execution.legging_cost == Decimal("12.5")
    assert step.diagnostics == ("SPREAD_BASIS_RISK",)

    simultaneous = replace(order, order_id="spread.order.2", simultaneous_fill=True)
    _, simultaneous_execution = simulator.execute_spread(
        FuturesLedger.open("ledger.spread.simultaneous", Decimal("100000")),
        simultaneous,
        first.quotes,
        execution_id="spread.execution.2",
        step_id="step.spread.2",
    )
    assert simultaneous_execution.legging_cost == 0
    assert not simultaneous_execution.basis_risk_flag


def test_risk_and_robustness_require_all_twelve_futures_stress_dimensions() -> None:
    near, deferred, first, _ = _market_fixture()
    simulator = _simulator((near, deferred))
    ledger = FuturesLedger.open("ledger.risk", Decimal("100000"))
    quote = first.quote_for(near.contract_id, first.observed_at)
    opened = simulator.execute(
        ledger,
        FuturesOrderIntent(
            intent_id="intent.risk",
            contract_id=near.contract_id,
            side=OrderSide.BUY,
            quantity=2,
            decision_at=quote.observed_at,
        ),
        quote,
        fill_id="fill.risk",
        step_id="step.risk",
    )
    risk = summarize_futures_risk(
        summary_id="risk.summary",
        summary_version="v1",
        simulator=simulator,
        ledger=opened.ledger,
        marks=(quote,),
        capital_usage_days=Decimal("12.5"),
    )
    assert isinstance(risk, FuturesRiskSummary)
    assert risk.gross_notional == Decimal("10000")
    assert risk.initial_margin_requirement == Decimal("2000")
    assert risk.leverage > 0

    cases = tuple(
        FuturesStressCase(
            case_id=f"stress.{kind.value}",
            case_version="v1",
            kind=kind,
            scalar=Decimal("1.5"),
            baseline_policy_hashes=(simulator.content_hash,),
        )
        for kind in FuturesStressKind
    )
    results = tuple(
        FuturesStressResult(
            result_id=f"result.{case.kind.value}",
            case_hash=case.content_hash,
            baseline_equity=Decimal("100000"),
            stressed_equity=Decimal("99000"),
            blocked_exit_count=(
                1 if case.kind is FuturesStressKind.PRICE_LIMIT_NO_EXIT else 0
            ),
            margin_call_count=(
                1 if case.kind is FuturesStressKind.MARGIN_INCREASE else 0
            ),
            diagnostics=(case.kind.value,),
        )
        for case in cases
    )
    robustness = FuturesRobustnessSummary(
        summary_id="robustness.summary",
        summary_version="v1",
        risk_summary_hash=risk.content_hash,
        cases=cases,
        results=results,
    )
    robustness.require_complete_s5_coverage()
    assert not robustness.missing_kinds
    assert len(robustness.covered_kinds) == 12

    incomplete = replace(
        robustness,
        summary_id="robustness.incomplete",
        cases=cases[:-1],
        results=results[:-1],
    )
    with pytest.raises(
        DerivativeResearchError, match="futures_robustness_coverage_missing"
    ):
        incomplete.require_complete_s5_coverage()


def test_prospective_evidence_freezes_chain_roll_settlement_margin_and_curve_drift() -> (
    None
):
    near, deferred, first, second = _market_fixture()
    roll_policy = _volume_roll_policy()
    continuous_policy = ContinuousFuturesPolicy(
        series_id="continuous.FUT",
        root_id="FUT.ROOT",
        policy_version="v1",
        roll_policy_hash=roll_policy.content_hash,
        adjustment=ContinuousAdjustment.DIFFERENCE,
        adjustment_direction=AdjustmentDirection.FORWARD,
    )
    previous, _ = build_continuous_point(
        (first, second),
        roll_policy,
        continuous_policy,
        as_of=first.observed_at,
        current_contract_id=near.contract_id,
        point_id="prospective.point.1",
        decision_id="prospective.decision.1",
        prospective=True,
    )
    point, decision = build_continuous_point(
        (first, second),
        roll_policy,
        continuous_policy,
        as_of=second.observed_at,
        current_contract_id=near.contract_id,
        point_id="prospective.point.2",
        decision_id="prospective.decision.2",
        previous_point=previous,
        prospective=True,
    )
    curve = compute_curve_feature(
        feature_id="prospective.curve",
        feature_version="v1",
        as_of=second.observed_at,
        near_quote=second.quote_for(near.contract_id, second.observed_at),
        deferred_quote=second.quote_for(deferred.contract_id, second.observed_at),
        near_contract=near,
        deferred_contract=deferred,
    )
    simulator = _simulator((near, deferred))
    first_quote = first.quote_for(near.contract_id, first.observed_at)
    opened = simulator.execute(
        FuturesLedger.open("ledger.prospective", Decimal("100000")),
        FuturesOrderIntent(
            intent_id="intent.prospective.seed",
            contract_id=near.contract_id,
            side=OrderSide.BUY,
            quantity=1,
            decision_at=first.observed_at,
            signal_series_id=previous.series_id,
            signal_point_hash=previous.content_hash,
        ),
        first_quote,
        fill_id="fill.prospective.seed",
        step_id="step.prospective.seed",
    )
    rolled = simulator.roll(
        opened.ledger,
        decision,
        second.quote_for(near.contract_id, second.observed_at),
        second.quote_for(deferred.contract_id, second.observed_at),
        execution_id="prospective.roll.execution",
        step_id="prospective.roll.step",
    )
    assert rolled.roll_execution is not None
    evidence = ProspectiveFuturesEvidence.capture(
        observation_id="prospective.observation.1",
        evidence_version="v1",
        frozen_experiment_hash=HASH_A,
        as_of=second.observed_at,
        snapshots=(first, second),
        continuous_point=point,
        roll_decision=decision,
        margin_policy=simulator.margin_policy,
        settlement_policy=simulator.settlement_policy,
        curve_feature=curve,
        historical_curve_mean=Decimal("0.05"),
        historical_curve_std=Decimal("0.02"),
        roll_execution=rolled.roll_execution,
    )

    assert evidence.chain_snapshot_hash == second.content_hash
    assert evidence.selected_contract_id == deferred.contract_id
    assert evidence.settlement_price == Decimal("103")
    assert len(evidence.roll_fill_hashes) == 2
    assert evidence.roll_cost == rolled.roll_execution.total_roll_cost
    assert evidence.margin_policy_hash == simulator.margin_policy.content_hash
    assert evidence.curve_drift_zscore == (
        curve.annualized_slope - Decimal("0.05")
    ) / Decimal("0.02")
    assert evidence.content_hash.startswith("sha256:")

    future_point = replace(
        point,
        observed_at="2026-03-12T16:00:00Z",
    )
    with pytest.raises(
        DerivativeResearchError, match="prospective_continuous_time_mismatch"
    ):
        ProspectiveFuturesEvidence.capture(
            observation_id="prospective.observation.bad",
            evidence_version="v1",
            frozen_experiment_hash=HASH_A,
            as_of=second.observed_at,
            snapshots=(first, second),
            continuous_point=future_point,
            roll_decision=decision,
            margin_policy=simulator.margin_policy,
            settlement_policy=simulator.settlement_policy,
            curve_feature=curve,
            historical_curve_mean=Decimal("0.05"),
            historical_curve_std=Decimal("0.02"),
        )


def test_policy_invariants_reject_ambiguous_or_incomplete_futures_semantics() -> None:
    with pytest.raises(DerivativeResearchError, match="days_before_required"):
        RollPolicy(
            policy_id="roll.bad",
            policy_version="v1",
            trigger=RollTrigger.COMPOSITE,
            composite_operator=CompositeOperator.ALL,
        )
    with pytest.raises(
        DerivativeResearchError, match="continuous_futures_must_be_signal_only"
    ):
        ContinuousFuturesPolicy(
            series_id="continuous.bad",
            root_id="FUT.ROOT",
            policy_version="v1",
            roll_policy_hash=HASH_A,
            adjustment=ContinuousAdjustment.UNADJUSTED,
            adjustment_direction=AdjustmentDirection.NONE,
            signal_only=False,
        )
    with pytest.raises(
        DerivativeResearchError, match="maintenance_margin_exceeds_initial"
    ):
        MarginSimulationPolicy(
            policy_id="margin.bad",
            policy_version="v1",
            initial_margin_per_contract=Decimal("500"),
            maintenance_margin_per_contract=Decimal("800"),
            collateral_fraction=Decimal("1"),
            margin_call_action=MarginCallAction.FAIL_RESEARCH,
        )
    with pytest.raises(
        DerivativeResearchError, match="settlement_price_field_must_be_explicit"
    ):
        SettlementPolicy(
            policy_id="settlement.bad",
            policy_version="v1",
            settlement_price_field="close_price",
            daily_mark_to_market=True,
            realize_variation_margin_daily=True,
        )


def test_runtime_type_guard_rejects_continuous_point_on_execution_path() -> None:
    contract = _contract("FUT.202603", last_trade="2026-03-20")
    simulator = _simulator((contract,))
    point = ContinuousFuturesPoint(
        point_id="guard.point",
        series_id="continuous.FUT",
        root_id="FUT.ROOT",
        observed_at="2026-03-10T16:00:00Z",
        source_contract_id=contract.contract_id,
        source_quote_hash=HASH_A,
        source_price=Decimal("100"),
        continuous_price=Decimal("100"),
        additive_adjustment=Decimal("0"),
        multiplicative_adjustment=Decimal("1"),
        roll_gap=Decimal("0"),
        policy_hash=HASH_A,
        roll_decision_hash=HASH_A,
        chain_snapshot_hash=HASH_A,
        previous_point_hash=None,
    )
    intent = FuturesOrderIntent(
        intent_id="guard.intent",
        contract_id=contract.contract_id,
        side=OrderSide.BUY,
        quantity=1,
        decision_at=point.observed_at,
    )
    with pytest.raises(
        DerivativeResearchError, match="continuous_futures_not_executable"
    ):
        simulator.execute(
            FuturesLedger.open("guard.ledger", Decimal("100000")),
            intent,
            point,  # type: ignore[arg-type]
            fill_id="guard.fill",
            step_id="guard.step",
        )


def test_roll_decision_cannot_claim_roll_without_contract_transition() -> None:
    with pytest.raises(
        DerivativeResearchError, match="roll_decision_contract_transition_invalid"
    ):
        RollDecision(
            decision_id="decision.invalid",
            decision_at="2026-03-10T16:00:00Z",
            root_id="FUT.ROOT",
            from_contract_id="FUT.202603",
            to_contract_id="FUT.202603",
            should_roll=True,
            reason="VOLUME_CROSSOVER",
            policy_hash=HASH_A,
            chain_snapshot_hash=HASH_A,
            input_quote_hashes=(HASH_A,),
        )
