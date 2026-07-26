from __future__ import annotations

import ast
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from market_research.research.derivatives.common import AvailabilityTimes
from market_research.research.derivatives.options import (
    ExerciseStyle,
    OptionContract,
    OptionQuote,
    OptionType,
    SettlementType,
    ValuationInputSnapshot,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.market_state import (
    MarketDataQuality,
    ObservationMetadata,
    OptionContractQuote,
    OptionRight as MarketStateOptionRight,
    QuoteCondition,
)
from market_research.research.multi_asset.option_analytics import (
    AnalyticsComparisonAction,
    AnalyticsComparisonStatus,
    AnalyticsVolatilitySource,
    AmericanCrrBinomialModel,
    AmericanRichardsonBinomialModel,
    AsianArithmeticMonteCarloModel,
    AuthoritativeOptionAnalyticsFactory,
    DayCountConvention,
    DiscreteDividend,
    EuropeanBlackScholesModel,
    ForwardInput,
    ForwardMethod,
    FuturesBlack76Model,
    OptionAnalyticsError,
    OptionModelInput,
    OptionModelKind,
    OptionModelRegistry,
    OptionQuoteQualityCandidate,
    OptionQuoteQualityContext,
    ProviderOptionQuoteRow,
    QuoteQualityAction,
    QuoteQualityDisposition,
    RejectedSurfacePoint,
    SupplierAnalyticsObservation,
    SurfaceCalibrationPolicy,
    SurfaceCoordinate,
    SurfaceExtrapolation,
    SurfaceObservation,
    calibrate_volatility_surface,
    default_analytics_comparison_policy,
    default_option_quote_quality_policy,
    default_option_model_registry,
    estimate_forward,
    screen_option_quote_quality,
    standard_provider_adapters,
    validate_option_model_conformance,
)
from market_research.research.multi_asset.option_pricing import (
    BlackScholesOptionAnalyticsFactory,
)


NOW = "2026-01-02T12:00:10+00:00"
EXPIRY = "2026-07-02T00:00:00+00:00"
FAR_EXPIRY = "2027-01-02T00:00:00+00:00"


def _hash(token: str) -> str:
    return sha256_prefixed(token, label="option_analytics_test")


def _provider_row(
    *,
    provider_id: str,
    record_id: str,
    contract_id: str,
    observed_at: str,
    published_at: str,
    available_at: str,
    bid: str | None,
    ask: str | None,
) -> ProviderOptionQuoteRow:
    return ProviderOptionQuoteRow(
        provider_id=provider_id,
        provider_record_id=record_id,
        contract_id=contract_id,
        observed_at=observed_at,
        published_at=published_at,
        available_at=available_at,
        bid=None if bid is None else Decimal(bid),
        ask=None if ask is None else Decimal(ask),
        bid_size=Decimal("10"),
        ask_size=Decimal("12"),
        volume=100,
        open_interest=500,
        source_artifact_hash=_hash(f"artifact:{record_id}"),
        provider_schema_hash=_hash(f"schema:{provider_id}"),
        supplier_implied_volatility=Decimal("0.25"),
        supplier_delta=Decimal("0.5"),
    )


def test_two_provider_conventions_normalize_to_same_economics_and_keep_raw() -> None:
    unit_adapter, contract_adapter = standard_provider_adapters()
    unit_row = _provider_row(
        provider_id="provider_unit_utc",
        record_id="unit.1",
        contract_id="option.call.100",
        observed_at="2026-01-02T12:00:00+00:00",
        published_at="2026-01-02T12:00:01+00:00",
        available_at="2026-01-02T12:00:02+00:00",
        bid="5.8",
        ask="6.0",
    )
    contract_row = _provider_row(
        provider_id="provider_contract_local",
        record_id="contract.1",
        contract_id="option.call.100",
        observed_at="2026-01-02T07:00:00",
        published_at="2026-01-02T07:00:01",
        available_at="2026-01-02T07:00:02",
        bid="580",
        ask="600",
    )

    unit = unit_adapter.normalize(unit_row, contract_multiplier=Decimal("100"))
    contract = contract_adapter.normalize(
        contract_row,
        contract_multiplier=Decimal("100"),
    )

    assert (unit.bid, unit.ask) == (contract.bid, contract.ask)
    assert unit.observed_at_utc == contract.observed_at_utc
    assert unit.raw_row is unit_row
    assert contract.raw_row is contract_row
    assert unit.raw_row_hash == unit_row.raw_payload_hash
    assert contract.raw_row_hash == contract_row.raw_payload_hash
    assert unit.raw_row_hash != contract.raw_row_hash
    assert unit.convention_hash != contract.convention_hash
    assert unit.transformation_hash != contract.transformation_hash

    zero_row = replace(
        contract_row, provider_record_id="contract.zero", bid=Decimal("0")
    )
    zero = contract_adapter.normalize(zero_row, contract_multiplier=Decimal("100"))
    assert zero.bid is None
    with pytest.raises(OptionAnalyticsError, match="provider_mismatch"):
        unit_adapter.normalize(contract_row, contract_multiplier=Decimal("100"))


def _quality_candidate(
    *,
    contract_id: str,
    strike: str,
    bid: str,
    ask: str,
    expiry_at: str = EXPIRY,
    observed_at: str = "2026-01-02T12:00:00+00:00",
    exercise_style: str = "EUROPEAN",
    settlement_style: str = "CASH",
    quote_underlying_id: str = "underlying.asset.xyz",
) -> OptionQuoteQualityCandidate:
    adapter = standard_provider_adapters()[0]
    row = _provider_row(
        provider_id="provider_unit_utc",
        record_id=f"row.{contract_id}",
        contract_id=contract_id,
        observed_at=observed_at,
        published_at="2026-01-02T12:00:01+00:00",
        available_at="2026-01-02T12:00:02+00:00",
        bid=bid,
        ask=ask,
    )
    quote = adapter.normalize(row, contract_multiplier=Decimal("100"))
    context = OptionQuoteQualityContext(
        contract_id=contract_id,
        underlying_id="underlying.asset.xyz",
        quote_underlying_id=quote_underlying_id,
        option_type=OptionType.CALL,
        exercise_style=exercise_style,
        settlement_style=settlement_style,
        expiry_at=expiry_at,
        strike=Decimal(strike),
        spot_price=Decimal("100"),
        discount_factor=Decimal("0.99"),
        decision_at=NOW,
        underlying_observed_at="2026-01-02T12:00:00+00:00",
        rate_observed_at="2026-01-02T12:00:00+00:00",
        dividend_observed_at="2026-01-02T12:00:00+00:00",
        market_state_hash=_hash("market-state"),
        underlying_hash=_hash("underlying"),
        rate_hash=_hash("rate"),
        dividend_hash=_hash("dividend"),
    )
    return OptionQuoteQualityCandidate(quote=quote, context=context)


def test_quality_pipeline_retains_modified_excluded_and_arbitrage_candidates() -> None:
    policy = replace(
        default_option_quote_quality_policy(),
        crossed_market_action=QuoteQualityAction.REPAIR,
    )
    candidates = (
        _quality_candidate(
            contract_id="call.90.near",
            strike="90",
            bid="11.9",
            ask="12.1",
        ),
        _quality_candidate(
            contract_id="call.100.near",
            strike="100",
            bid="12.9",
            ask="13.1",
        ),
        _quality_candidate(
            contract_id="call.110.near",
            strike="110",
            bid="2.9",
            ask="3.1",
        ),
        _quality_candidate(
            contract_id="call.100.far",
            strike="100",
            bid="4.9",
            ask="5.1",
            expiry_at=FAR_EXPIRY,
        ),
        _quality_candidate(
            contract_id="call.crossed",
            strike="120",
            bid="6.0",
            ask="5.8",
        ),
        _quality_candidate(
            contract_id="call.bad-underlying",
            strike="130",
            bid="1.0",
            ask="1.1",
            quote_underlying_id="underlying.other",
        ),
    )

    chain = screen_option_quote_quality(
        chain_id="quality.chain.1",
        candidates=candidates,
        policy=policy,
    )
    by_id = {item.contract_id: item for item in chain.records}

    crossed = by_id["call.crossed"]
    assert crossed.disposition is QuoteQualityDisposition.MODIFIED
    assert (crossed.corrected_bid, crossed.corrected_ask) == (
        Decimal("5.8"),
        Decimal("6.0"),
    )
    assert "CROSSED_MARKET_REPAIRED_BY_SWAP" in crossed.reasons
    assert by_id["call.bad-underlying"].disposition is QuoteQualityDisposition.EXCLUDED
    assert "UNDERLYING_MISMATCH" in by_id["call.bad-underlying"].reasons
    all_reasons = {reason for item in chain.records for reason in item.reasons}
    assert {
        "VERTICAL_ARBITRAGE_CANDIDATE",
        "BUTTERFLY_ARBITRAGE_CANDIDATE",
        "CALENDAR_ARBITRAGE_CANDIDATE",
    } <= all_reasons
    assert chain.modified_records
    assert chain.excluded_records
    assert all(
        record.raw_quote.content_hash == record.identity_payload()["raw_quote_hash"]
        for record in chain.records
    )


def test_quality_pipeline_rejects_stale_style_and_intrinsic_failures() -> None:
    stale = _quality_candidate(
        contract_id="call.stale",
        strike="90",
        bid="1.0",
        ask="1.1",
        observed_at="2026-01-02T11:58:00+00:00",
        exercise_style="BERMUDAN",
    )
    chain = screen_option_quote_quality(
        chain_id="quality.chain.negative",
        candidates=(stale,),
        policy=default_option_quote_quality_policy(),
    )
    record = chain.records[0]
    assert record.disposition is QuoteQualityDisposition.EXCLUDED
    assert {
        "STALE_QUOTE",
        "EXERCISE_STYLE_UNSUPPORTED",
        "INTRINSIC_LOWER_BOUND_VIOLATION",
    } <= set(record.reasons)


def test_forward_authority_supports_spot_futures_dividends_and_day_counts() -> None:
    dividend = DiscreteDividend(
        ex_at="2026-03-02T00:00:00+00:00",
        amount=Decimal("1.50"),
        source_hash=_hash("dividend-source"),
    )
    inputs = ForwardInput(
        valuation_at=NOW,
        expiry_at=EXPIRY,
        spot_price=Decimal("100"),
        futures_price=Decimal("103"),
        risk_free_rate=Decimal("0.04"),
        dividend_yield=Decimal("0.01"),
        borrow_rate=Decimal("0.005"),
        day_count=DayCountConvention.ACT_365_25,
        settlement_convention="T_PLUS_1_CASH",
        discrete_dividends=(dividend,),
        market_state_hash=_hash("forward-market-state"),
        policy_hash=_hash("forward-policy"),
        input_hashes=(_hash("spot"), _hash("curve"), _hash("borrow")),
    )

    spot = estimate_forward(inputs, method=ForwardMethod.SPOT_CARRY)
    future = estimate_forward(inputs, method=ForwardMethod.FUTURES_PRICE)

    assert spot.discounted_dividends > 0
    assert spot.prepaid_spot is not None and spot.prepaid_spot < Decimal("100")
    assert future.value == Decimal("103")
    assert future.prepaid_spot is None
    assert spot.market_state_hash == inputs.market_state_hash
    assert spot.policy_hash == inputs.policy_hash
    assert spot.assumptions_hash != future.assumptions_hash

    with pytest.raises(OptionAnalyticsError, match="outside_horizon"):
        replace(
            inputs,
            discrete_dividends=(replace(dividend, ex_at="2028-01-01T00:00:00+00:00"),),
        )


def _surface_observation(
    *,
    contract_id: str,
    expiry_at: str,
    time_years: str,
    strike: str,
    volatility: str,
) -> SurfaceObservation:
    return SurfaceObservation(
        contract_id=contract_id,
        option_type=OptionType.CALL,
        expiry_at=expiry_at,
        time_years=Decimal(time_years),
        strike=Decimal(strike),
        spot=Decimal("100"),
        forward=Decimal("100"),
        discount_factor=Decimal("0.99"),
        raw_implied_volatility=Decimal(volatility),
        bid_implied_volatility=Decimal(volatility) - Decimal("0.01"),
        ask_implied_volatility=Decimal(volatility) + Decimal("0.01"),
        delta=Decimal("0.5"),
        liquidity_weight=Decimal("1"),
        normalized_quote_hash=_hash(f"normalized:{contract_id}"),
        own_analytics_hash=_hash(f"analytics:{contract_id}"),
    )


def _surface_inputs() -> tuple[SurfaceObservation, ...]:
    return (
        _surface_observation(
            contract_id="surface.90.near",
            expiry_at=EXPIRY,
            time_years="0.5",
            strike="90",
            volatility="0.20",
        ),
        _surface_observation(
            contract_id="surface.100.near",
            expiry_at=EXPIRY,
            time_years="0.5",
            strike="100",
            volatility="1.00",
        ),
        _surface_observation(
            contract_id="surface.110.near",
            expiry_at=EXPIRY,
            time_years="0.5",
            strike="110",
            volatility="0.20",
        ),
        _surface_observation(
            contract_id="surface.90.far",
            expiry_at=FAR_EXPIRY,
            time_years="1",
            strike="90",
            volatility="0.10",
        ),
        _surface_observation(
            contract_id="surface.100.far",
            expiry_at=FAR_EXPIRY,
            time_years="1",
            strike="100",
            volatility="0.10",
        ),
        _surface_observation(
            contract_id="surface.110.far",
            expiry_at=FAR_EXPIRY,
            time_years="1",
            strike="110",
            volatility="0.10",
        ),
    )


def test_surface_calibration_repairs_static_arbitrage_and_is_deterministic() -> None:
    policy = SurfaceCalibrationPolicy(
        policy_id="surface.repair",
        policy_version="v1",
        coordinate=SurfaceCoordinate.LOG_FORWARD_MONEYNESS,
        extrapolation=SurfaceExtrapolation.REJECT,
        maximum_repair_price_residual=Decimal("100"),
    )
    rejected = RejectedSurfacePoint(
        contract_id="surface.rejected",
        source_quote_hash=_hash("surface-rejected-quote"),
        quality_record_hash=_hash("surface-rejected-quality"),
        rejection_reasons=("STALE_QUOTE",),
    )
    arguments = {
        "surface_id": "surface.authoritative",
        "calibrated_at": NOW,
        "underlying_id": "underlying.asset.xyz",
        "observations": _surface_inputs(),
        "rejected_points": (rejected,),
        "policy": policy,
    }

    first = calibrate_volatility_surface(**arguments)
    second = calibrate_volatility_surface(**arguments)

    assert first.content_hash == second.content_hash
    assert first.stability_hash == second.stability_hash
    assert first.raw_observations == _surface_inputs()
    assert first.rejected_points == (rejected,)
    assert first.repair_count > 0
    assert any(not item.passed for item in first.pre_repair_diagnostics)
    assert all(item.passed for item in first.post_repair_diagnostics)
    assert first.maximum_price_residual > 0
    assert first.implied_volatility(expiry_at=EXPIRY, strike=Decimal("95")) > 0
    assert (
        first.implied_volatility_for_coordinate(
            expiry_at=EXPIRY,
            coordinate_value=Decimal("0"),
        )
        > 0
    )
    with pytest.raises(OptionAnalyticsError, match="coordinate_policy_mismatch"):
        first.implied_volatility_for_coordinate(
            expiry_at=EXPIRY,
            coordinate_value=Decimal("100"),
            coordinate=SurfaceCoordinate.STRIKE,
        )
    with pytest.raises(OptionAnalyticsError, match="extrapolation_forbidden"):
        first.implied_volatility(expiry_at=EXPIRY, strike=Decimal("50"))

    with pytest.raises(OptionAnalyticsError, match="grid_duplicate"):
        calibrate_volatility_surface(
            surface_id="surface.duplicate",
            calibrated_at=NOW,
            underlying_id="underlying.asset.xyz",
            observations=(*_surface_inputs(), _surface_inputs()[0]),
            policy=policy,
        )
    with pytest.raises(OptionAnalyticsError, match="points_insufficient"):
        calibrate_volatility_surface(
            surface_id="surface.sparse",
            calibrated_at=NOW,
            underlying_id="underlying.asset.xyz",
            observations=_surface_inputs()[:2],
            policy=policy,
        )


def _model_input(
    *,
    input_id: str,
    contract_id: str,
    exercise_style: ExerciseStyle,
    option_type: OptionType = OptionType.CALL,
    underlying_kind: str = "SPOT",
    payoff_kind: str = "VANILLA",
    spot: str = "100",
    strike: str = "100",
    monitoring_steps: int = 0,
) -> OptionModelInput:
    return OptionModelInput(
        input_id=input_id,
        contract_id=contract_id,
        option_type=option_type,
        exercise_style=exercise_style,
        strike=Decimal(strike),
        time_years=Decimal("1"),
        spot=Decimal(spot),
        forward=Decimal("102"),
        volatility=Decimal("0.20"),
        risk_free_rate=Decimal("0.05"),
        dividend_yield=Decimal("0.01"),
        borrow_rate=Decimal("0"),
        payoff_kind=payoff_kind,
        underlying_kind=underlying_kind,
        valuation_at="2026-01-01T00:00:00+00:00",
        expiry_at="2027-01-01T06:00:00+00:00",
        day_count=DayCountConvention.ACT_365_25,
        monitoring_steps=monitoring_steps,
        source_hashes=(_hash(f"model-input:{input_id}"),),
    )


def _model_registry() -> OptionModelRegistry:
    return OptionModelRegistry(
        (
            EuropeanBlackScholesModel(),
            FuturesBlack76Model(),
            AmericanCrrBinomialModel(
                steps=50,
                convergence_tolerance=Decimal("2"),
            ),
            AmericanRichardsonBinomialModel(
                coarse_steps=50,
                convergence_tolerance=Decimal("2"),
            ),
            AsianArithmeticMonteCarloModel(
                paths=256,
                seed=1729,
                convergence_tolerance=Decimal("5"),
            ),
        ),
        registry_version="option_model_registry_test_v1",
    )


def test_model_registry_conformance_covers_european_american_futures_and_exotic() -> (
    None
):
    registry = _model_registry()
    european = _model_input(
        input_id="input.european",
        contract_id="option.european",
        exercise_style=ExerciseStyle.EUROPEAN,
    )
    future = _model_input(
        input_id="input.future",
        contract_id="option.future",
        exercise_style=ExerciseStyle.EUROPEAN,
        underlying_kind="FUTURE",
    )
    american = _model_input(
        input_id="input.american",
        contract_id="option.american.put",
        exercise_style=ExerciseStyle.AMERICAN,
        option_type=OptionType.PUT,
        spot="80",
        strike="100",
    )
    asian = _model_input(
        input_id="input.asian",
        contract_id="option.asian",
        exercise_style=ExerciseStyle.EUROPEAN,
        payoff_kind="ASIAN_ARITHMETIC",
        monitoring_steps=8,
    )

    evidence = validate_option_model_conformance(
        registry,
        european_spot_input=european,
        european_future_input=future,
        american_input=american,
        asian_input=asian,
        american_tolerance=Decimal("2"),
    )

    assert evidence["registry_hash"] == registry.content_hash
    assert set(evidence) >= {
        "black_scholes_result_hash",
        "black_76_result_hash",
        "american_crr_result_hash",
        "american_richardson_result_hash",
        "american_european_benchmark_hash",
        "asian_result_hash",
        "asian_path_grid_variant_hash",
    }
    for kind, inputs in (
        (OptionModelKind.EUROPEAN_BLACK_SCHOLES, european),
        (OptionModelKind.FUTURES_BLACK_76, future),
        (OptionModelKind.AMERICAN_CRR_BINOMIAL, american),
        (OptionModelKind.AMERICAN_RICHARDSON_BINOMIAL, american),
        (OptionModelKind.ASIAN_ARITHMETIC_MONTE_CARLO, asian),
    ):
        result = registry.evaluate(kind, inputs)
        assert result.price >= 0
        assert result.convergence_error <= result.numerical_tolerance
        assert result.assumptions
        assert result.model_hash == registry.resolve(kind, inputs).content_hash
        assert result.greeks.vanna.is_finite()
        assert result.greeks.volga.is_finite()
        assert result.greeks.charm.is_finite()

    crr = registry.evaluate(OptionModelKind.AMERICAN_CRR_BINOMIAL, american)
    assert crr.exercise_boundary
    assert crr.price >= Decimal("20")
    first_asian = registry.evaluate(
        OptionModelKind.ASIAN_ARITHMETIC_MONTE_CARLO,
        asian,
    )
    second_asian = registry.evaluate(
        OptionModelKind.ASIAN_ARITHMETIC_MONTE_CARLO,
        asian,
    )
    assert first_asian == second_asian
    alternate_path = registry.evaluate(
        OptionModelKind.ASIAN_ARITHMETIC_MONTE_CARLO,
        replace(
            asian,
            input_id="input.asian.alternate-path",
            monitoring_steps=12,
        ),
    )
    assert alternate_path.price != first_asian.price
    low_volatility_limit = registry.evaluate(
        OptionModelKind.ASIAN_ARITHMETIC_MONTE_CARLO,
        replace(
            asian,
            input_id="input.asian.low-volatility-limit",
            strike=Decimal("90"),
            forward=Decimal("100"),
            volatility=Decimal("0.001"),
            risk_free_rate=Decimal("0"),
            dividend_yield=Decimal("0"),
        ),
    )
    assert abs(low_volatility_limit.price - Decimal("10")) < Decimal("0.01")
    high_rate_american = registry.evaluate(
        OptionModelKind.AMERICAN_CRR_BINOMIAL,
        replace(
            american,
            input_id="input.american.high-rate",
            risk_free_rate=Decimal("0.10"),
        ),
    )
    assert high_rate_american.exercise_boundary != crr.exercise_boundary
    low_dividend_call = replace(
        american,
        input_id="input.american.call.low-dividend",
        contract_id="option.american.call.low-dividend",
        option_type=OptionType.CALL,
        spot=Decimal("120"),
        dividend_yield=Decimal("0"),
    )
    high_dividend_call = replace(
        low_dividend_call,
        input_id="input.american.call.high-dividend",
        contract_id="option.american.call.high-dividend",
        dividend_yield=Decimal("0.12"),
    )
    low_dividend_result = registry.evaluate(
        OptionModelKind.AMERICAN_CRR_BINOMIAL,
        low_dividend_call,
    )
    high_dividend_result = registry.evaluate(
        OptionModelKind.AMERICAN_CRR_BINOMIAL,
        high_dividend_call,
    )
    assert (
        low_dividend_result.exercise_boundary != high_dividend_result.exercise_boundary
    )
    with pytest.raises(OptionAnalyticsError, match="domain_mismatch"):
        registry.evaluate(OptionModelKind.EUROPEAN_BLACK_SCHOLES, american)
    with pytest.raises(OptionAnalyticsError, match="domain_mismatch"):
        registry.evaluate(
            OptionModelKind.ASIAN_ARITHMETIC_MONTE_CARLO,
            american,
        )
    with pytest.raises(OptionAnalyticsError, match="monitoring_steps_insufficient"):
        replace(asian, monitoring_steps=1)


def _availability() -> AvailabilityTimes:
    return AvailabilityTimes(
        event_at="2026-01-02T12:00:00+00:00",
        published_at="2026-01-02T12:00:01+00:00",
        provider_received_at="2026-01-02T12:00:02+00:00",
        system_received_at="2026-01-02T12:00:03+00:00",
        processed_at="2026-01-02T12:00:04+00:00",
    )


def _valuation_input() -> ValuationInputSnapshot:
    contract = OptionContract(
        contract_id="option.call.100.jul",
        underlying_id="underlying.asset.xyz",
        option_type=OptionType.CALL,
        strike=Decimal("100"),
        expiration_at=EXPIRY,
        exercise_style=ExerciseStyle.EUROPEAN,
        settlement_type=SettlementType.CASH,
        multiplier=Decimal("100"),
        currency="USD",
        exchange="exchange.x",
        listing_at="2025-12-01T00:00:00+00:00",
        last_trade_at=EXPIRY,
        settlement_at="2026-07-02T01:00:00+00:00",
        price_tick=Decimal("0.01"),
    )
    quote = OptionQuote(
        quote_id="quote.option.call.100.jul",
        contract_id=contract.contract_id,
        availability=_availability(),
        as_of=NOW,
        bid=Decimal("5.8"),
        ask=Decimal("6.0"),
        last=Decimal("5.9"),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
        volume=100,
        open_interest=500,
    )
    return ValuationInputSnapshot(
        valuation_input_id="valuation.option.call.100.jul",
        contract=contract,
        quote=quote,
        valuation_at=NOW,
        spot_price=Decimal("100"),
        risk_free_rate=Decimal("0"),
        dividend_yield=Decimal("0"),
        forward_price=Decimal("100"),
        spot_availability=_availability(),
        rate_availability=_availability(),
        dividend_availability=_availability(),
        forward_availability=_availability(),
        source_manifest_hashes=(_hash("valuation-source"),),
    )


def _typed_quote(inputs: ValuationInputSnapshot) -> OptionContractQuote:
    source = inputs.quote
    assert source.bid is not None
    assert source.ask is not None
    return OptionContractQuote(
        contract_id=inputs.contract.contract_id,
        underlying_instrument_id=inputs.contract.underlying_id,
        expiry_at=inputs.contract.expiration_at,
        right=MarketStateOptionRight.CALL,
        strike=inputs.contract.strike,
        currency=inputs.contract.currency,
        price_unit="USD_per_share",
        bid=source.bid,
        ask=source.ask,
        last=source.last,
        settlement=None,
        bid_size=source.bid_size,
        ask_size=source.ask_size,
        volume=Decimal(source.volume),
        open_interest=Decimal(source.open_interest),
        condition=QuoteCondition.NORMAL,
        metadata=ObservationMetadata(
            observed_at=source.availability.event_at,
            knowledge_at=source.availability.processed_at,
            source_hash=source.content_hash,
            calendar_id="calendar.exchange.x",
            max_age_seconds=source.stale_after_seconds,
        ),
    )


def _factory(
    *,
    action: AnalyticsComparisonAction = AnalyticsComparisonAction.REJECT,
) -> AuthoritativeOptionAnalyticsFactory:
    return AuthoritativeOptionAnalyticsFactory(
        registry=default_option_model_registry(),
        comparison_policy=default_analytics_comparison_policy(action=action),
        margin_model_hash=_hash("margin-model"),
    )


def test_factory_is_only_public_authority_and_supplier_values_are_comparisons() -> None:
    inputs = _valuation_input()
    quote = _typed_quote(inputs)
    factory = _factory()
    receipt = factory.derive(
        receipt_id="analytics.receipt.base",
        quote=quote,
        valuation_input=inputs,
        margin_per_contract=Decimal("1"),
        collateral_per_contract=Decimal("2"),
    )
    receipt.require_valid()
    mark = receipt.analytics_mark
    assert receipt.supplier_comparison.status is AnalyticsComparisonStatus.NOT_PROVIDED
    assert receipt.volatility_source is AnalyticsVolatilitySource.MARKET_QUOTE_INVERSION
    assert receipt.implied_volatility == receipt.market_implied_volatility
    assert mark.source_quote_hash == quote.content_hash
    assert mark.valuation_input_hash == inputs.content_hash
    assert mark.model_hash == factory.pricing_adapter.model.content_hash
    model_input = factory._model_input(inputs, receipt.implied_volatility)
    assert (
        receipt.own_model_result.model_hash
        == factory.registry.resolve(
            OptionModelKind.EUROPEAN_BLACK_SCHOLES,
            model_input,
        ).content_hash
    )

    matching_supplier = SupplierAnalyticsObservation(
        provider_id="supplier.analytics",
        contract_id=mark.contract_id,
        observed_at=quote.metadata.observed_at,
        source_hash=_hash("supplier-matching"),
        implied_volatility=mark.implied_volatility,
        delta=mark.delta,
        gamma=mark.gamma,
        vega_per_vol_point=mark.vega,
        theta_per_calendar_day=mark.theta,
        rho_per_rate_point=mark.rho,
    )
    matched = factory.derive(
        receipt_id="analytics.receipt.matched",
        quote=quote,
        valuation_input=inputs,
        margin_per_contract=Decimal("1"),
        collateral_per_contract=Decimal("2"),
        supplier_observation=matching_supplier,
    )
    assert matched.supplier_comparison.status is AnalyticsComparisonStatus.MATCHED

    surface_policy = SurfaceCalibrationPolicy(
        policy_id="factory.surface",
        policy_version="v1",
        coordinate=SurfaceCoordinate.STRIKE,
        extrapolation=SurfaceExtrapolation.REJECT,
        maximum_repair_price_residual=Decimal("100"),
    )
    surface_points = tuple(
        replace(
            item,
            contract_id=(
                inputs.contract.contract_id
                if item.contract_id == "surface.100.near"
                else item.contract_id
            ),
        )
        for item in _surface_inputs()
    )
    surface = calibrate_volatility_surface(
        surface_id="factory.bound.surface",
        calibrated_at=inputs.quote.availability.processed_at,
        underlying_id=inputs.contract.underlying_id,
        observations=surface_points,
        policy=surface_policy,
    )
    surfaced = factory.derive(
        receipt_id="analytics.receipt.surface",
        quote=quote,
        valuation_input=inputs,
        margin_per_contract=Decimal("1"),
        collateral_per_contract=Decimal("2"),
        surface=surface,
    )
    assert surfaced.surface_hash == surface.content_hash
    assert surfaced.volatility_source is AnalyticsVolatilitySource.CALIBRATED_SURFACE
    assert surfaced.analytics_mark.implied_volatility == surface.implied_volatility(
        expiry_at=inputs.contract.expiration_at,
        strike=inputs.contract.strike,
    )

    manipulated_supplier = replace(
        matching_supplier,
        source_hash=_hash("supplier-manipulated"),
        delta=mark.delta - Decimal("0.50"),
    )
    with pytest.raises(OptionAnalyticsError, match="tolerance_exceeded"):
        factory.derive(
            receipt_id="analytics.receipt.rejected",
            quote=quote,
            valuation_input=inputs,
            margin_per_contract=Decimal("1"),
            collateral_per_contract=Decimal("2"),
            supplier_observation=manipulated_supplier,
        )

    degraded = _factory(action=AnalyticsComparisonAction.DEGRADED).derive(
        receipt_id="analytics.receipt.degraded",
        quote=quote,
        valuation_input=inputs,
        margin_per_contract=Decimal("1"),
        collateral_per_contract=Decimal("2"),
        supplier_observation=manipulated_supplier,
    )
    assert degraded.supplier_comparison.status is AnalyticsComparisonStatus.DEGRADED
    assert degraded.supplier_comparison.breached_fields == ("delta",)
    assert degraded.analytics_mark.metadata.quality is MarketDataQuality.INDICATIVE

    with pytest.raises(OptionAnalyticsError, match="requires_authoritative_factory"):
        replace(receipt, receipt_id="analytics.receipt.forged")
    with pytest.raises(OptionAnalyticsError, match="quote_binding_mismatch"):
        factory.derive(
            receipt_id="analytics.receipt.wrong-quote",
            quote=replace(quote, strike=Decimal("101")),
            valuation_input=inputs,
            margin_per_contract=Decimal("1"),
            collateral_per_contract=Decimal("2"),
        )


def test_builtin_public_path_cannot_construct_option_analytics_mark_directly() -> None:
    assert BlackScholesOptionAnalyticsFactory.production_authoritative is False
    package_path = (
        Path(__file__).parents[1] / "src/market_research/research/multi_asset"
    )
    source_path = package_path / "builtin_runner.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    direct_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == "OptionAnalyticsMark"
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == "OptionAnalyticsMark"
        )
    ]
    assert not direct_calls
    source = source_path.read_text(encoding="utf-8")
    assert "AuthoritativeOptionAnalyticsFactory" in source
    assert "analytics_factory.derive" in source

    allowed_factory_modules = {"option_analytics.py", "option_pricing.py"}
    bypasses: list[tuple[str, int]] = []
    for module_path in package_path.glob("*.py"):
        module_tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(module_tree):
            if not isinstance(node, ast.Call):
                continue
            is_constructor = (
                isinstance(node.func, ast.Name)
                and node.func.id == "OptionAnalyticsMark"
            ) or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "OptionAnalyticsMark"
            )
            if is_constructor and module_path.name not in allowed_factory_modules:
                bypasses.append((module_path.name, node.lineno))
    assert not bypasses
