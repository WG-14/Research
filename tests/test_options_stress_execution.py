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
    ExerciseStyle,
    MultiLegExecutionPolicy,
    MultiLegOrder,
    MultiLegState,
    OptionChainSnapshot,
    OptionContract,
    OptionLeg,
    OptionPosition,
    OptionQuote,
    OptionRobustnessDimension,
    OptionRobustnessInput,
    OptionRobustnessPolicy,
    OptionType,
    PositionSide,
    SettlementType,
    SurfacePoint,
    TransactionSide,
    ValuationInputSnapshot,
    VolatilitySurface,
    evaluate_early_exercise,
    execute_multi_leg_order,
    mark_option_position,
    position_from_fill,
    run_option_robustness_suite,
    simulate_option_fill,
    simulate_option_lifecycle,
    standard_option_robustness_cases,
)
from tests.test_options_derivative_research import _settlement_input


NOW = "2026-01-02T12:00:30+00:00"
NEAR_EXPIRY = "2026-07-02T00:00:00+00:00"
FAR_EXPIRY = "2027-01-02T00:00:00+00:00"


def _hash(token: str) -> str:
    return "sha256:" + token * 64


def _availability(*, older: bool = False) -> AvailabilityTimes:
    if older:
        return AvailabilityTimes(
            event_at="2026-01-02T12:00:00+00:00",
            published_at="2026-01-02T12:00:01+00:00",
            provider_received_at="2026-01-02T12:00:02+00:00",
            system_received_at="2026-01-02T12:00:03+00:00",
            processed_at="2026-01-02T12:00:04+00:00",
        )
    return AvailabilityTimes(
        event_at="2026-01-02T12:00:20+00:00",
        published_at="2026-01-02T12:00:21+00:00",
        provider_received_at="2026-01-02T12:00:22+00:00",
        system_received_at="2026-01-02T12:00:23+00:00",
        processed_at="2026-01-02T12:00:24+00:00",
    )


def _contract(
    contract_id: str,
    *,
    strike: str,
    expiration_at: str,
    exercise_style: ExerciseStyle = ExerciseStyle.EUROPEAN,
) -> OptionContract:
    return OptionContract(
        contract_id=contract_id,
        underlying_id="asset_xyz",
        option_type=OptionType.CALL,
        strike=Decimal(strike),
        expiration_at=expiration_at,
        exercise_style=exercise_style,
        settlement_type=SettlementType.CASH,
        multiplier=Decimal("100"),
        currency="USD",
        exchange="exchange_x",
        listing_at="2025-12-01T00:00:00+00:00",
        last_trade_at=expiration_at,
        settlement_at=(
            "2026-07-02T01:00:00+00:00"
            if expiration_at == NEAR_EXPIRY
            else "2027-01-02T01:00:00+00:00"
        ),
        price_tick=Decimal("0.01"),
    )


def _quote(
    contract: OptionContract,
    *,
    bid: str,
    ask: str,
    volume: int,
    open_interest: int,
    older: bool = False,
    ask_size: str = "10",
) -> OptionQuote:
    return OptionQuote(
        quote_id=f"quote.{contract.contract_id}",
        contract_id=contract.contract_id,
        availability=_availability(older=older),
        as_of=NOW,
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=(Decimal(bid) + Decimal(ask)) / Decimal("2"),
        bid_size=Decimal("10"),
        ask_size=Decimal(ask_size),
        volume=volume,
        open_interest=open_interest,
        stale_after_seconds=60,
        max_spread_ratio=Decimal("0.50"),
    )


def _valuation(contract: OptionContract, quote: OptionQuote) -> ValuationInputSnapshot:
    availability = quote.availability
    return ValuationInputSnapshot(
        valuation_input_id=f"valuation.{contract.contract_id}",
        contract=contract,
        quote=quote,
        valuation_at=NOW,
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


def _fill_and_position(
    contract: OptionContract,
    quote: OptionQuote,
    *,
    side: PositionSide,
) -> tuple[object, OptionPosition]:
    fill = simulate_option_fill(
        fill_id=f"fill.{contract.contract_id}.{side.value.lower()}",
        contract=contract,
        quote=quote,
        side=(
            TransactionSide.BUY if side is PositionSide.LONG else TransactionSide.SELL
        ),
        quantity=Decimal("1"),
        filled_at=NOW,
    )
    return fill, position_from_fill(
        fill,
        position_id=f"position.{contract.contract_id}.{side.value.lower()}",
    )


def _robustness_input() -> OptionRobustnessInput:
    euro_long = _contract("euro_call_100_near", strike="100", expiration_at=NEAR_EXPIRY)
    euro_short = _contract("euro_call_110_far", strike="110", expiration_at=FAR_EXPIRY)
    american_long = _contract(
        "american_call_80_near",
        strike="80",
        expiration_at=NEAR_EXPIRY,
        exercise_style=ExerciseStyle.AMERICAN,
    )
    american_short = _contract(
        "american_call_90_near",
        strike="90",
        expiration_at=NEAR_EXPIRY,
        exercise_style=ExerciseStyle.AMERICAN,
    )
    euro_long_quote = _quote(
        euro_long,
        bid="5.5",
        ask="5.7",
        volume=120,
        open_interest=600,
        ask_size="1",
    )
    euro_short_quote = _quote(
        euro_short,
        bid="3.0",
        ask="3.2",
        volume=1200,
        open_interest=6000,
        older=True,
    )
    american_long_quote = _quote(
        american_long,
        bid="20.1",
        ask="20.5",
        volume=200,
        open_interest=800,
    )
    american_short_quote = _quote(
        american_short,
        bid="10.2",
        ask="10.6",
        volume=200,
        open_interest=800,
    )
    contracts = (
        euro_long,
        euro_short,
        american_long,
        american_short,
    )
    quotes = (
        euro_long_quote,
        euro_short_quote,
        american_long_quote,
        american_short_quote,
    )
    quality = (
        QualityResult(
            check_id="option.robustness_fixture",
            check_version="1",
            decision=QualityDecision.PASS,
        ),
    )
    chain = OptionChainSnapshot(
        chain_snapshot_id="chain.options.robustness",
        underlying_id="asset_xyz",
        knowledge_time=NOW,
        underlying_price=Decimal("100"),
        contracts=contracts,
        quotes=quotes,
        source_manifest_hashes=(_hash("1"),),
        quality_results=quality,
    )
    fill_position_rows = (
        _fill_and_position(euro_long, euro_long_quote, side=PositionSide.LONG),
        _fill_and_position(euro_short, euro_short_quote, side=PositionSide.SHORT),
        _fill_and_position(american_long, american_long_quote, side=PositionSide.LONG),
        _fill_and_position(
            american_short, american_short_quote, side=PositionSide.SHORT
        ),
    )
    fills = tuple(row[0] for row in fill_position_rows)
    positions = tuple(row[1] for row in fill_position_rows)
    priced_positions = positions[:2]
    valuations = (
        _valuation(euro_long, euro_long_quote),
        _valuation(euro_short, euro_short_quote),
    )
    reference_model = BlackScholesModel(model_version="iv_reference_v1")
    comparison_model = BlackScholesModel(model_version="iv_conservative_v1")
    base_iv = tuple(reference_model.implied_volatility(item) for item in valuations)
    comparison_iv = tuple(
        comparison_model.implied_volatility(item, item.quote.ask) for item in valuations
    )
    assert all(item.success and item.volatility is not None for item in base_iv)
    assert all(item.success and item.volatility is not None for item in comparison_iv)
    greeks = tuple(
        reference_model.greeks(item, iv.volatility)
        for item, iv in zip(valuations, base_iv, strict=True)
        if iv.volatility is not None
    )
    base_surface = VolatilitySurface(
        surface_id="surface.base.robustness",
        as_of=NOW,
        underlying_id="asset_xyz",
        points=tuple(
            SurfacePoint(
                contract_id=item.contract.contract_id,
                expiration_at=item.contract.expiration_at,
                strike=item.contract.strike,
                implied_volatility=iv.volatility,
                valuation_input_hash=item.content_hash,
                iv_result_hash=iv.content_hash,
                model_version=iv.model_version,
            )
            for item, iv in zip(valuations, base_iv, strict=True)
            if iv.volatility is not None
        ),
        interpolation_version="linear_variance_v1",
        source_chain_hash=chain.content_hash,
        quality_results=quality,
    )
    comparison_surface = VolatilitySurface(
        surface_id="surface.comparison.robustness",
        as_of=NOW,
        underlying_id="asset_xyz",
        points=tuple(
            SurfacePoint(
                contract_id=item.contract.contract_id,
                expiration_at=item.contract.expiration_at,
                strike=item.contract.strike,
                implied_volatility=iv.volatility,
                valuation_input_hash=item.content_hash,
                iv_result_hash=iv.content_hash,
                model_version=iv.model_version,
            )
            for item, iv in zip(valuations, comparison_iv, strict=True)
            if iv.volatility is not None
        ),
        interpolation_version="monotone_variance_v2",
        source_chain_hash=chain.content_hash,
        quality_results=quality,
    )
    marks = tuple(
        mark_option_position(
            position,
            quote=value_input.quote,
            theoretical_price=reference_model.price(value_input, iv.volatility),
            theoretical_input_hash=value_input.content_hash,
            marked_at=NOW,
        )
        for position, value_input, iv in zip(
            priced_positions, valuations, base_iv, strict=True
        )
        if iv.volatility is not None
    )
    lifecycle_events = []
    for position in positions[2:]:
        decision = evaluate_early_exercise(
            position.contract,
            evaluated_at=NOW,
            spot_price=Decimal("100"),
            continuation_value=Decimal("1"),
        )
        lifecycle_events.append(
            simulate_option_lifecycle(
                position,
                event_id=f"lifecycle.{position.position_id}",
                event_at=NOW,
                settlement_input=_settlement_input(
                    position.contract, "100", settlement_at=NOW
                ),
                early_exercise_decision=decision,
            )
        )
    multileg_order = MultiLegOrder(
        group_id="multileg.robustness.partial",
        legs=(
            OptionLeg(
                leg_id="leg.long",
                contract=euro_long,
                side=PositionSide.LONG,
                quantity=Decimal("2"),
            ),
            OptionLeg(
                leg_id="leg.short",
                contract=euro_short,
                side=PositionSide.SHORT,
                quantity=Decimal("2"),
            ),
        ),
        policy=MultiLegExecutionPolicy.SEQUENTIAL,
        requested_at=NOW,
        maximum_leg_time_skew_seconds=0,
        allow_partial=True,
        execution_policy_hash=_hash("2"),
    )
    multileg_result = execute_multi_leg_order(
        multileg_order,
        quotes={item.contract_id: item for item in quotes},
        fill_times={"leg.long": NOW, "leg.short": NOW},
    )
    assert multileg_result.state is MultiLegState.PARTIAL
    return OptionRobustnessInput(
        robustness_input_id="option.robustness.input.v1",
        run_type=RunType.ROBUSTNESS,
        chain_snapshot=chain,
        positions=positions,
        priced_position_ids=tuple(item.position_id for item in priced_positions),
        valuation_inputs=valuations,
        base_iv_results=base_iv,
        comparison_iv_results=comparison_iv,
        greeks=greeks,
        base_surface=base_surface,
        comparison_surface=comparison_surface,
        fills=fills,  # type: ignore[arg-type]
        marks=marks,
        lifecycle_events=tuple(lifecycle_events),
        multileg_orders=(multileg_order,),
        multileg_results=(multileg_result,),
        payoff_spots=(
            Decimal("0"),
            Decimal("50"),
            Decimal("100"),
            Decimal("200"),
            Decimal("500"),
        ),
        definition_hashes=(
            _hash("a"),
            _hash("b"),
            _hash("c"),
            _hash("d"),
            _hash("e"),
        ),
    )


def test_complete_option_robustness_suite_executes_all_twenty_dimensions() -> None:
    inputs = _robustness_input()
    policy = OptionRobustnessPolicy(policy_id="options.s5.v1")
    cases = standard_option_robustness_cases(policy)

    executions, summary = run_option_robustness_suite(
        suite_id="options.s5.complete",
        inputs=inputs,
        cases=cases,
    )
    repeated, repeated_summary = run_option_robustness_suite(
        suite_id="options.s5.complete",
        inputs=inputs,
        cases=tuple(reversed(cases)),
    )

    assert len(executions) == 20
    assert {item.dimension for item in executions} == set(OptionRobustnessDimension)
    assert summary.content_hash == repeated_summary.content_hash
    assert tuple(item.content_hash for item in executions) == tuple(
        item.content_hash for item in repeated
    )
    assert all(item.input_hash == inputs.content_hash for item in executions)
    assert all(item.evidence_hashes == inputs.evidence_hashes for item in executions)


def test_required_option_stress_variants_and_tail_evidence_are_materialized() -> None:
    inputs = _robustness_input()
    executions, _summary = run_option_robustness_suite(
        suite_id="options.s5.metrics",
        inputs=inputs,
        cases=standard_option_robustness_cases(
            OptionRobustnessPolicy(policy_id="options.s5.metrics.v1")
        ),
    )
    by_dimension = {item.dimension: item for item in executions}
    spread_ids = {
        item.metric_id
        for item in by_dimension[OptionRobustnessDimension.BID_ASK_COST].metrics
    }
    selection_ids = {
        item.metric_id
        for item in by_dimension[OptionRobustnessDimension.CHAIN_SELECTION].metrics
    }

    assert spread_ids == {"spread.1", "spread.1.5", "spread.2"}
    assert selection_ids == {
        "selection.fixed_strike.count",
        "selection.delta.count",
        "selection.moneyness.count",
        "selection.expiry.count",
    }
    assert by_dimension[
        OptionRobustnessDimension.PAYOFF_TAIL_RISK
    ].derived_artifact_hashes
    assert any(
        item.metric_id == "rare_loss.total" and item.value > 0
        for item in by_dimension[OptionRobustnessDimension.SHORT_RARE_LOSS].metrics
    )
    assert any(
        item.metric_id == "lifecycle.assignment.count" and item.value == 1
        for item in by_dimension[OptionRobustnessDimension.EXERCISE_ASSIGNMENT].metrics
    )


def test_robustness_input_fails_closed_on_surface_and_execution_mismatch() -> None:
    inputs = _robustness_input()

    with pytest.raises(DerivativeResearchError, match="surface_chain_mismatch"):
        replace(
            inputs,
            comparison_surface=replace(
                inputs.comparison_surface,
                source_chain_hash=_hash("f"),
            ),
        )
    with pytest.raises(DerivativeResearchError, match="position_fill_missing"):
        replace(inputs, fills=inputs.fills[:-1])


def test_suite_rejects_missing_or_duplicate_dimensions() -> None:
    inputs = _robustness_input()
    cases = standard_option_robustness_cases(
        OptionRobustnessPolicy(policy_id="options.s5.coverage.v1")
    )

    with pytest.raises(DerivativeResearchError, match="full_case_matrix_required"):
        run_option_robustness_suite(
            suite_id="options.s5.incomplete",
            inputs=inputs,
            cases=cases[:-1],
        )
    with pytest.raises(DerivativeResearchError, match="full_case_matrix_required"):
        run_option_robustness_suite(
            suite_id="options.s5.duplicate",
            inputs=inputs,
            cases=(*cases[:-1], cases[0]),
        )
