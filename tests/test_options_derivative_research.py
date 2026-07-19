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
from market_research.research.derivatives.options import (
    BlackScholesModel,
    EarlyExerciseDecision,
    ExerciseStyle,
    FillStatus,
    IVFailure,
    LifecycleEventType,
    MoneynessMethod,
    MultiLegExecutionPolicy,
    MultiLegOrder,
    MultiLegState,
    OptionChainSnapshot,
    OptionContract,
    OptionFeatureSnapshot,
    OptionLeg,
    OptionPosition,
    OptionProspectiveObservation,
    OptionProspectiveProtocol,
    OptionStressScenario,
    OptionType,
    PositionSide,
    ProspectiveStatus,
    QuoteState,
    SettlementType,
    SurfacePoint,
    TransactionSide,
    ValuationInputSnapshot,
    VolatilitySurface,
    analyze_option_payoff,
    evaluate_early_exercise,
    evaluate_option_chain_quality,
    evaluate_option_prospective,
    evaluate_volatility_surface_quality,
    execute_multi_leg_order,
    liquidity_features,
    mark_option_position,
    net_option_greeks,
    option_capital_requirement,
    option_chain_as_of,
    option_moneyness,
    position_from_fill,
    put_call_parity_residual,
    simulate_option_fill,
    simulate_option_lifecycle,
    stress_option_portfolio,
    unwind_multi_leg_execution,
    volatility_skew,
    volatility_term_structure,
    OptionQuote,
)


NOW = "2026-01-02T12:00:10+00:00"
EXPIRY = "2026-07-02T00:00:00+00:00"


def _hash(token: str) -> str:
    return "sha256:" + token * 64


def _availability(
    event_at: str = "2026-01-02T12:00:00+00:00",
    processed_at: str = "2026-01-02T12:00:04+00:00",
) -> AvailabilityTimes:
    return AvailabilityTimes(
        event_at=event_at,
        published_at="2026-01-02T12:00:01+00:00",
        provider_received_at="2026-01-02T12:00:02+00:00",
        system_received_at="2026-01-02T12:00:03+00:00",
        processed_at=processed_at,
    )


def _contract(
    contract_id: str = "opt_call_100_jul",
    *,
    option_type: OptionType = OptionType.CALL,
    strike: str = "100",
    expiration_at: str = EXPIRY,
    exercise_style: ExerciseStyle = ExerciseStyle.EUROPEAN,
    settlement_type: SettlementType = SettlementType.CASH,
    listing_at: str = "2026-01-01T00:00:00+00:00",
) -> OptionContract:
    return OptionContract(
        contract_id=contract_id,
        underlying_id="asset_xyz",
        option_type=option_type,
        strike=Decimal(strike),
        expiration_at=expiration_at,
        exercise_style=exercise_style,
        settlement_type=settlement_type,
        multiplier=Decimal("100"),
        currency="USD",
        exchange="exchange_x",
        listing_at=listing_at,
        last_trade_at=expiration_at,
        settlement_at=(
            "2026-07-02T01:00:00+00:00"
            if expiration_at == EXPIRY
            else "2027-01-02T01:00:00+00:00"
        ),
        price_tick=Decimal("0.01"),
        deliverable_asset_id=(
            "asset_xyz" if settlement_type is SettlementType.PHYSICAL else None
        ),
    )


def _quote(
    contract: OptionContract,
    *,
    bid: str | None = "5.8",
    ask: str | None = "6.0",
    bid_size: str = "10",
    ask_size: str = "10",
    as_of: str = NOW,
    availability: AvailabilityTimes | None = None,
    stale_after_seconds: int = 60,
) -> OptionQuote:
    return OptionQuote(
        quote_id=f"quote.{contract.contract_id}",
        contract_id=contract.contract_id,
        availability=availability or _availability(),
        as_of=as_of,
        bid=Decimal(bid) if bid is not None else None,
        ask=Decimal(ask) if ask is not None else None,
        last=Decimal("5.9"),
        bid_size=Decimal(bid_size),
        ask_size=Decimal(ask_size),
        volume=100,
        open_interest=500,
        stale_after_seconds=stale_after_seconds,
    )


def _inputs(
    contract: OptionContract | None = None,
    quote: OptionQuote | None = None,
    *,
    valuation_at: str = NOW,
) -> ValuationInputSnapshot:
    selected_contract = contract or _contract()
    selected_quote = quote or _quote(selected_contract)
    availability = _availability()
    return ValuationInputSnapshot(
        valuation_input_id=f"valuation.{selected_contract.contract_id}",
        contract=selected_contract,
        quote=selected_quote,
        valuation_at=valuation_at,
        spot_price=Decimal("100"),
        risk_free_rate=Decimal("0"),
        dividend_yield=Decimal("0"),
        forward_price=Decimal("100"),
        spot_availability=availability,
        rate_availability=availability,
        dividend_availability=availability,
        forward_availability=availability,
        source_manifest_hashes=(_hash("1"),),
    )


def _position(
    contract: OptionContract,
    *,
    side: PositionSide = PositionSide.LONG,
    quantity: str = "1",
    entry_price: str = "5",
) -> OptionPosition:
    return OptionPosition(
        position_id=f"position.{contract.contract_id}.{side.value.lower()}",
        contract=contract,
        side=side,
        quantity=Decimal(quantity),
        entry_price=Decimal(entry_price),
        entry_fee=Decimal("1"),
        opened_at=NOW,
        source_fill_hash=_hash("2"),
    )


def test_contract_series_identity_and_lifecycle_time_boundaries() -> None:
    contract = _contract()

    assert contract.is_tradeable_at(contract.last_trade_at)
    assert contract.series_key[2] == "100"
    assert contract.content_hash.startswith("sha256:")
    with pytest.raises(DerivativeResearchError, match="time_order_invalid"):
        replace(contract, listing_at=contract.expiration_at)
    with pytest.raises(DerivativeResearchError, match="deliverable_asset_required"):
        replace(contract, settlement_type=SettlementType.PHYSICAL)
    with pytest.raises(DerivativeResearchError, match="must_be_decimal"):
        replace(contract, strike=100.0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("bid", "ask", "as_of", "sizes", "expected"),
    [
        (None, None, NOW, ("0", "0"), QuoteState.NO_QUOTE),
        ("0", "1", NOW, ("1", "1"), QuoteState.ZERO_BID),
        ("6", "5", NOW, ("1", "1"), QuoteState.CROSSED),
        (
            "5.8",
            "6",
            "2026-01-02T12:02:00+00:00",
            ("1", "1"),
            QuoteState.STALE,
        ),
        ("1", "2", NOW, ("1", "1"), QuoteState.ILLIQUID),
        ("5.8", "6", NOW, ("1", "1"), QuoteState.NORMAL),
    ],
)
def test_quote_states_are_distinct_and_fail_closed(
    bid: str | None,
    ask: str | None,
    as_of: str,
    sizes: tuple[str, str],
    expected: QuoteState,
) -> None:
    quote = _quote(
        _contract(),
        bid=bid,
        ask=ask,
        as_of=as_of,
        bid_size=sizes[0],
        ask_size=sizes[1],
    )

    assert quote.state is expected
    if expected is QuoteState.NORMAL:
        assert quote.executable_price(TransactionSide.BUY) == Decimal("6")
    else:
        with pytest.raises(DerivativeResearchError, match="not_executable"):
            quote.executable_price(TransactionSide.BUY)


def test_chain_snapshot_is_point_in_time_and_quality_gates_confirmation() -> None:
    current = _contract()
    future = _contract(
        "opt_future_listing",
        strike="110",
        listing_at="2026-06-01T00:00:00+00:00",
    )
    snapshot = option_chain_as_of(
        chain_snapshot_id="chain.xyz.now",
        underlying_id="asset_xyz",
        as_of=NOW,
        underlying_price=Decimal("100"),
        contracts=(current, future),
        quotes=(_quote(current),),
        source_manifest_hashes=(_hash("3"),),
    )

    assert tuple(item.contract_id for item in snapshot.contracts) == (
        current.contract_id,
    )
    assert snapshot.contract(current.contract_id) is current
    with pytest.raises(DerivativeResearchError, match="contract_missing"):
        snapshot.contract("not_here")
    failed = replace(
        snapshot,
        quality_results=(
            QualityResult(
                "option.chain_integrity", "1", QualityDecision.FAILED
            ),
        ),
    )
    failed.admit(RunType.EXPLORATORY)
    with pytest.raises(DerivativeResearchError, match="quality_blocked"):
        failed.admit(RunType.CONFIRMATORY)


def test_chain_no_arbitrage_checks_detect_bounds_monotonicity_and_crossed_quotes() -> None:
    call_90 = _contract("call_90", strike="90")
    call_100 = _contract("call_100", strike="100")
    call_110 = _contract("call_110", strike="110")
    snapshot = OptionChainSnapshot(
        chain_snapshot_id="chain.quality.bad",
        underlying_id="asset_xyz",
        knowledge_time=NOW,
        underlying_price=Decimal("100"),
        contracts=(call_90, call_100, call_110),
        quotes=(
            _quote(call_90, bid="10", ask="11"),
            _quote(call_100, bid="20", ask="21"),
            _quote(call_110, bid="6", ask="5"),
        ),
        source_manifest_hashes=(_hash("4"),),
    )

    results = {item.check_id: item for item in evaluate_option_chain_quality(snapshot)}
    assert results["option.quote_state"].decision is QualityDecision.FAILED
    assert results["option.strike_monotonicity"].decision is QualityDecision.FAILED


def test_valuation_inputs_require_pit_alignment_and_carry_consistency() -> None:
    inputs = _inputs()

    assert inputs.time_to_expiry_years > Decimal("0")
    with pytest.raises(DerivativeResearchError, match="forward_inconsistent"):
        replace(inputs, forward_price=Decimal("150"))
    late = AvailabilityTimes(
        event_at="2026-01-02T12:02:00+00:00",
        published_at="2026-01-02T12:02:01+00:00",
        provider_received_at="2026-01-02T12:02:02+00:00",
        system_received_at="2026-01-02T12:02:03+00:00",
        processed_at="2026-01-02T12:02:04+00:00",
    )
    with pytest.raises(DerivativeResearchError, match="not_time_aligned"):
        replace(
            inputs,
            valuation_at="2026-01-02T12:05:00+00:00",
            rate_availability=late,
        )


def test_black_scholes_iv_greeks_and_structured_failures() -> None:
    inputs = _inputs()
    model = BlackScholesModel()
    iv = model.implied_volatility(inputs)

    assert iv.success
    assert iv.failure is IVFailure.NONE
    assert Decimal("0.1") < iv.volatility < Decimal("0.4")  # type: ignore[operator]
    greeks = model.greeks(inputs, iv.volatility)  # type: ignore[arg-type]
    assert Decimal("0") < greeks.delta < Decimal("1")
    assert greeks.gamma > 0
    outside = model.implied_volatility(inputs, Decimal("101"))
    assert outside.failure is IVFailure.OUTSIDE_ARBITRAGE_BOUNDS

    zero_quote = _quote(inputs.contract, bid="0", ask="1")
    zero_result = model.implied_volatility(_inputs(inputs.contract, zero_quote))
    assert not zero_result.success
    assert zero_result.failure is IVFailure.ZERO_BID


def _surface(*, far_vol: str = "0.24") -> VolatilitySurface:
    near = EXPIRY
    far = "2027-01-02T00:00:00+00:00"
    points = (
        SurfacePoint("near_90", near, Decimal("90"), Decimal("0.25"), _hash("1"), _hash("2"), "bs_v1"),
        SurfacePoint("near_110", near, Decimal("110"), Decimal("0.21"), _hash("3"), _hash("4"), "bs_v1"),
        SurfacePoint("far_90", far, Decimal("90"), Decimal(far_vol), _hash("5"), _hash("6"), "bs_v1"),
        SurfacePoint("far_110", far, Decimal("110"), Decimal(far_vol), _hash("7"), _hash("8"), "bs_v1"),
    )
    return VolatilitySurface(
        surface_id="surface.xyz.v1",
        as_of=NOW,
        underlying_id="asset_xyz",
        points=points,
        interpolation_version="total_variance_v1",
        source_chain_hash=_hash("9"),
    )


def test_surface_features_and_calendar_arbitrage_are_versioned() -> None:
    surface = _surface()
    middle = surface.interpolate(expiration_at=EXPIRY, strike=Decimal("100"))

    assert middle == Decimal("0.23")
    assert volatility_skew(
        surface,
        expiration_at=EXPIRY,
        lower_strike=Decimal("90"),
        upper_strike=Decimal("110"),
    ) == Decimal("-0.002")
    assert volatility_term_structure(
        surface,
        strike=Decimal("100"),
        near_expiration_at=EXPIRY,
        far_expiration_at="2027-01-02T00:00:00+00:00",
    ) > 0
    assert option_moneyness(
        _contract(),
        spot_price=Decimal("100"),
        forward_price=Decimal("100"),
        method=MoneynessMethod.LOG_FORWARD_MONEYNESS,
    ) == 0
    with pytest.raises(DerivativeResearchError, match="extrapolation_forbidden"):
        surface.interpolate(expiration_at=EXPIRY, strike=Decimal("120"))

    bad = _surface(far_vol="0.05")
    quality = evaluate_volatility_surface_quality(bad)
    assert quality[0].decision is QualityDecision.FAILED


def test_liquidity_parity_and_feature_evidence_are_explicit() -> None:
    quote = _quote(_contract())
    liquidity = liquidity_features(quote)
    residual = put_call_parity_residual(
        call_price=Decimal("6"),
        put_price=Decimal("6"),
        spot_price=Decimal("100"),
        strike=Decimal("100"),
        risk_free_rate=Decimal("0"),
        dividend_yield=Decimal("0"),
        valuation_at=NOW,
        expiration_at=EXPIRY,
    )
    feature = OptionFeatureSnapshot(
        feature_snapshot_id="feature.call100.v1",
        contract_id=quote.contract_id,
        feature_at=NOW,
        moneyness_method=MoneynessMethod.STRIKE_OVER_FORWARD,
        moneyness=Decimal("1"),
        skew=Decimal("-0.002"),
        term_slope=Decimal("0.01"),
        parity_residual=residual,
        liquidity_state=quote.state,
        spread_ratio=liquidity["spread_ratio"],  # type: ignore[arg-type]
        definition_hashes=(_hash("a"),),
        source_hashes=(quote.content_hash,),
    )

    assert residual == 0
    assert feature.content_hash.startswith("sha256:")


def test_single_leg_partial_fill_market_vs_theoretical_mark() -> None:
    contract = _contract()
    quote = _quote(contract, ask_size="2")
    fill = simulate_option_fill(
        fill_id="fill.call100.partial",
        contract=contract,
        quote=quote,
        side=TransactionSide.BUY,
        quantity=Decimal("3"),
        filled_at=NOW,
        fee_per_contract=Decimal("1"),
        allow_partial=True,
    )
    position = position_from_fill(fill, position_id="position.call100")
    mark = mark_option_position(
        position,
        quote=quote,
        theoretical_price=Decimal("6.5"),
        theoretical_input_hash=_hash("b"),
        marked_at=NOW,
    )

    assert fill.status is FillStatus.PARTIAL
    assert fill.filled_quantity == 2
    assert fill.cash_flow == Decimal("-1202")
    assert mark.liquidation_price == quote.bid
    assert mark.theoretical_pnl > mark.liquidation_pnl  # type: ignore[operator]
    no_fill = simulate_option_fill(
        fill_id="fill.call100.none",
        contract=contract,
        quote=_quote(contract, bid=None, ask=None, bid_size="0", ask_size="0"),
        side=TransactionSide.BUY,
        quantity=Decimal("1"),
        filled_at=NOW,
    )
    assert no_fill.status is FillStatus.FAILED


def test_early_exercise_expiry_assignment_and_physical_delivery() -> None:
    american = _contract(
        "american_call",
        exercise_style=ExerciseStyle.AMERICAN,
    )
    decision = evaluate_early_exercise(
        american,
        evaluated_at="2026-06-01T00:00:00+00:00",
        spot_price=Decimal("120"),
        continuation_value=Decimal("19"),
    )
    event = simulate_option_lifecycle(
        _position(american),
        event_id="event.american.exercise",
        event_at=decision.evaluated_at,
        settlement_spot=Decimal("120"),
        early_exercise_decision=decision,
    )

    assert decision.exercise
    assert event.event_type is LifecycleEventType.EXERCISE
    assert event.cash_delta == Decimal("2000")

    physical = _contract(
        "physical_call",
        settlement_type=SettlementType.PHYSICAL,
    )
    assignment = simulate_option_lifecycle(
        _position(physical, side=PositionSide.SHORT),
        event_id="event.physical.assignment",
        event_at=physical.expiration_at,
        settlement_spot=Decimal("110"),
    )
    assert assignment.event_type is LifecycleEventType.EXPIRY
    assert assignment.deliverable_quantity_delta == Decimal("-100")
    assert assignment.cash_delta == Decimal("10000")

    european = _contract()
    rejected = evaluate_early_exercise(
        european,
        evaluated_at="2026-06-01T00:00:00+00:00",
        spot_price=Decimal("120"),
        continuation_value=Decimal("0"),
    )
    assert rejected == EarlyExerciseDecision(
        contract_id=european.contract_id,
        evaluated_at="2026-06-01T00:00:00+00:00",
        permitted=False,
        exercise=False,
        intrinsic_value=Decimal("20"),
        continuation_value=Decimal("0"),
        transaction_cost=Decimal("0"),
        reason="european_before_expiry",
    )


def _multi_leg_order(
    call: OptionContract,
    put: OptionContract,
    *,
    policy: MultiLegExecutionPolicy,
    quantity: str = "1",
    allow_partial: bool = False,
    maximum_skew: int = 1,
) -> MultiLegOrder:
    return MultiLegOrder(
        group_id=f"group.{policy.value.lower()}.{quantity.replace('.', '_')}",
        legs=(
            OptionLeg("call_leg", call, PositionSide.LONG, Decimal(quantity)),
            OptionLeg("put_leg", put, PositionSide.SHORT, Decimal(quantity)),
        ),
        policy=policy,
        requested_at=NOW,
        maximum_leg_time_skew_seconds=maximum_skew,
        allow_partial=allow_partial,
        execution_policy_hash=_hash("c"),
    )


def test_multi_leg_simultaneous_atomicity_and_sequential_legging() -> None:
    call = _contract("multi_call")
    put = _contract("multi_put", option_type=OptionType.PUT)
    quotes = {call.contract_id: _quote(call), put.contract_id: _quote(put)}
    simultaneous = _multi_leg_order(
        call, put, policy=MultiLegExecutionPolicy.SIMULTANEOUS
    )
    filled = execute_multi_leg_order(
        simultaneous,
        quotes=quotes,
        fill_times={"call_leg": NOW, "put_leg": NOW},
    )
    failed = execute_multi_leg_order(
        simultaneous,
        quotes=quotes,
        fill_times={
            "call_leg": NOW,
            "put_leg": "2026-01-02T12:00:12+00:00",
        },
    )

    assert filled.state is MultiLegState.FILLED
    assert len(filled.committed_fills) == 2
    assert failed.state is MultiLegState.FAILED
    assert not failed.committed_fills

    partial_quotes = {
        call.contract_id: _quote(call, ask_size="1"),
        put.contract_id: _quote(put),
    }
    sequential = _multi_leg_order(
        call,
        put,
        policy=MultiLegExecutionPolicy.SEQUENTIAL,
        quantity="2",
        allow_partial=True,
    )
    partial = execute_multi_leg_order(
        sequential,
        quotes=partial_quotes,
        fill_times={"call_leg": NOW, "put_leg": NOW},
    )
    assert partial.state is MultiLegState.PARTIAL
    assert partial.legging_exposure_contract_ids

    unwound = unwind_multi_leg_execution(
        filled,
        unwind_group_id="group.unwind",
        quotes=quotes,
        filled_at=NOW,
    )
    assert unwound.state is MultiLegState.UNWOUND


def test_net_greeks_payoff_expiry_mismatch_and_tail_capital() -> None:
    call = _contract("risk_call")
    far_call = _contract(
        "risk_far_call",
        strike="110",
        expiration_at="2027-01-02T00:00:00+00:00",
    )
    long = _position(call)
    short = _position(far_call, side=PositionSide.SHORT)
    model = BlackScholesModel()
    call_greeks = model.greeks(_inputs(call, _quote(call)), Decimal("0.2"))
    far_greeks = model.greeks(
        _inputs(far_call, _quote(far_call)), Decimal("0.2")
    )
    net = net_option_greeks(
        (long, short),
        {call.contract_id: call_greeks, far_call.contract_id: far_greeks},
    )
    payoff = analyze_option_payoff(
        (short,), scenario_spots=(Decimal("0"), Decimal("110"), Decimal("300"))
    )
    capital = option_capital_requirement(
        (short,), reference_spot=Decimal("100")
    )

    assert net.expiry_mismatch
    assert payoff.unbounded_loss
    assert payoff.maximum_loss is None
    assert capital.unbounded_tail
    assert capital.stressed_capital > 0


def test_option_stress_combines_spot_vol_rate_time_and_liquidity() -> None:
    contract = _contract("stress_call")
    position = _position(contract)
    inputs = _inputs(contract, _quote(contract))
    scenario = OptionStressScenario(
        scenario_id="stress.down_vol_liquidity",
        spot_shock_ratio=Decimal("-0.2"),
        volatility_shock=Decimal("0.1"),
        rate_shock=Decimal("0.01"),
        dividend_yield_shock=Decimal("0"),
        liquidity_spread_multiplier=Decimal("3"),
        days_forward=30,
        scenario_policy_hash=_hash("d"),
    )
    result = stress_option_portfolio(
        (position,),
        inputs_by_contract={contract.contract_id: inputs},
        volatility_by_contract={contract.contract_id: Decimal("0.2")},
        scenario=scenario,
    )

    assert result.scenario_hash == scenario.content_hash
    assert result.total_stressed_liquidation_value < result.total_stressed_value
    assert result.total_profit_loss_change < 0
    with pytest.raises(DerivativeResearchError, match="shock_invalid"):
        replace(scenario, spot_shock_ratio=Decimal("-1"))


def _protocol(*, minimum: int = 2) -> OptionProspectiveProtocol:
    return OptionProspectiveProtocol(
        protocol_id="prospective.option_iv.v1",
        frozen_at="2026-01-01T00:00:00+00:00",
        evaluation_start="2026-01-03T00:00:00+00:00",
        evaluation_end="2026-01-04T00:00:00+00:00",
        minimum_observations=minimum,
        maximum_mean_absolute_error=Decimal("1"),
        maximum_invalid_quote_fraction=Decimal("0.25"),
        model_version="black_scholes_european_v1",
        dataset_snapshot_hash=_hash("e"),
        surface_definition_hash=_hash("f"),
        acceptance_policy_hash=_hash("0"),
    )


def _observation(observation_id: str, observed: str) -> OptionProspectiveObservation:
    return OptionProspectiveObservation(
        observation_id=observation_id,
        contract_id="opt_call_100_jul",
        prediction_made_at="2026-01-02T12:00:00+00:00",
        observed_at="2026-01-03T12:00:00+00:00",
        predicted_price=Decimal("6"),
        observed_price=Decimal(observed),
        quote_state=QuoteState.NORMAL,
        valuation_input_hash=_hash("1"),
        model_result_hash=_hash("2"),
        quote_hash=_hash("3"),
    )


def test_prospective_evidence_is_frozen_before_observation_and_version_bound() -> None:
    protocol = _protocol()
    observations = (_observation("observation.one", "6.2"), _observation("observation.two", "5.8"))
    evaluation = evaluate_option_prospective(
        protocol,
        observations,
        evaluated_at="2026-01-04T00:00:01+00:00",
    )

    assert evaluation.status is ProspectiveStatus.CONFIRMED
    assert evaluation.mean_absolute_error == Decimal("0.2")
    assert evaluation.protocol_hash == protocol.content_hash
    inconclusive = evaluate_option_prospective(
        _protocol(minimum=3),
        observations,
        evaluated_at="2026-01-04T00:00:01+00:00",
    )
    assert inconclusive.status is ProspectiveStatus.INCONCLUSIVE
    with pytest.raises(DerivativeResearchError, match="before_window_end"):
        evaluate_option_prospective(
            protocol,
            observations,
            evaluated_at="2026-01-03T23:59:59+00:00",
        )
    with pytest.raises(DerivativeResearchError, match="not_prospective"):
        replace(
            observations[0],
            prediction_made_at=observations[0].observed_at,
        )
