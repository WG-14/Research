from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.derivatives.common import AvailabilityTimes
from market_research.research.derivatives.options import (
    BlackScholesModel,
    ExerciseStyle,
    OptionContract,
    OptionQuote,
    OptionType,
    SettlementType,
    ValuationInputSnapshot,
)
from market_research.research.multi_asset.option_path import (
    CommonOptionPricingModel,
    OptionGreeks,
)
from market_research.research.multi_asset.option_pricing import (
    BlackScholesOptionAnalyticsFactory,
    BlackScholesPricingAdapter,
    OptionPricingAdapterError,
    OptionPricingState,
    black_scholes_pricing_specification,
)
from market_research.research.multi_asset.market_state import (
    ObservationMetadata,
    OptionChainState,
    OptionContractQuote,
    OptionRight as MarketStateOptionRight,
    QuoteCondition,
)


NOW = "2026-01-02T12:00:10+00:00"
EXPIRY = "2026-07-02T00:00:00+00:00"


def _hash(token: str) -> str:
    return "sha256:" + token * 64


def _availability() -> AvailabilityTimes:
    return AvailabilityTimes(
        event_at="2026-01-02T12:00:00+00:00",
        published_at="2026-01-02T12:00:01+00:00",
        provider_received_at="2026-01-02T12:00:02+00:00",
        system_received_at="2026-01-02T12:00:03+00:00",
        processed_at="2026-01-02T12:00:04+00:00",
    )


def _contract(
    contract_id: str = "option.call.100.jul",
    *,
    strike: str = "100",
    expiration_at: str = EXPIRY,
    exercise_style: ExerciseStyle = ExerciseStyle.EUROPEAN,
) -> OptionContract:
    return OptionContract(
        contract_id=contract_id,
        underlying_id="underlying.asset.xyz",
        option_type=OptionType.CALL,
        strike=Decimal(strike),
        expiration_at=expiration_at,
        exercise_style=exercise_style,
        settlement_type=SettlementType.CASH,
        multiplier=Decimal("100"),
        currency="USD",
        exchange="exchange.x",
        listing_at="2025-12-01T00:00:00+00:00",
        last_trade_at=expiration_at,
        settlement_at="2026-07-02T01:00:00+00:00",
        price_tick=Decimal("0.01"),
    )


def _quote(contract: OptionContract) -> OptionQuote:
    return OptionQuote(
        quote_id=f"quote.{contract.contract_id}",
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


def _inputs(
    contract: OptionContract | None = None,
    *,
    spot: str = "100",
    valuation_input_id: str = "valuation.option.call.100.jul",
) -> ValuationInputSnapshot:
    selected = contract or _contract()
    availability = _availability()
    return ValuationInputSnapshot(
        valuation_input_id=valuation_input_id,
        contract=selected,
        quote=_quote(selected),
        valuation_at=NOW,
        spot_price=Decimal(spot),
        risk_free_rate=Decimal("0"),
        dividend_yield=Decimal("0"),
        forward_price=Decimal(spot),
        spot_availability=availability,
        rate_availability=availability,
        dividend_availability=availability,
        forward_availability=availability,
        source_manifest_hashes=(_hash("1"),),
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


def test_adapter_satisfies_common_protocol_and_converts_greek_units() -> None:
    adapter = BlackScholesPricingAdapter()
    common_model: CommonOptionPricingModel = adapter
    inputs = _inputs()
    volatility = Decimal("0.25")
    state = adapter.bind_state(inputs, volatility)

    price = common_model.value(inputs.contract, state)
    legacy_price = adapter.model.price(inputs, volatility)
    legacy_greeks = adapter.model.greeks(inputs, volatility)
    greeks = common_model.greeks(inputs.contract, state)

    assert price == legacy_price
    assert isinstance(state, OptionPricingState)
    assert greeks == OptionGreeks(
        delta=legacy_greeks.delta,
        gamma=legacy_greeks.gamma,
        vega_per_vol_point=legacy_greeks.vega * Decimal("0.01"),
        theta_per_calendar_day=legacy_greeks.theta_per_year / Decimal("365.25"),
        rho_per_rate_point=legacy_greeks.rho * Decimal("0.01"),
    )
    assert state.valuation_input_hash == inputs.content_hash
    assert state.contract_hash == inputs.contract.content_hash
    assert state.pricing_model_hash == adapter.model.content_hash
    assert state.specification_hash == adapter.specification.content_hash
    assert state.content_hash.startswith("sha256:")
    assert adapter.content_hash.startswith("sha256:")


def test_price_to_implied_volatility_to_reprice_round_trip() -> None:
    adapter = BlackScholesPricingAdapter()
    inputs = _inputs()
    state = adapter.bind_state(inputs, Decimal("0.25"))
    observed_price = adapter.value(inputs.contract, state)

    implied = adapter.implied_parameter(inputs.contract, observed_price, state)
    repriced_state = adapter.bind_state(inputs, implied)
    repriced = adapter.value(inputs.contract, repriced_state)

    assert abs(implied - Decimal("0.25")) < Decimal("0.000001")
    assert abs(repriced - observed_price) <= adapter.model.price_tolerance


def test_scenario_value_reprices_the_shocked_valuation_snapshot() -> None:
    adapter = BlackScholesPricingAdapter()
    base_inputs = _inputs()
    base_state = adapter.bind_state(base_inputs, Decimal("0.25"))
    shocked_inputs = _inputs(
        base_inputs.contract,
        spot="110",
        valuation_input_id="valuation.option.call.100.jul.shocked",
    )
    shocked_state = adapter.bind_state(shocked_inputs, Decimal("0.30"))

    base_value = adapter.value(base_inputs.contract, base_state)
    scenario_value = adapter.scenario_value(base_inputs.contract, shocked_state)

    assert scenario_value == adapter.model.price(shocked_inputs, Decimal("0.30"))
    assert scenario_value > base_value
    assert shocked_state.content_hash != base_state.content_hash


def test_contract_input_model_and_specification_bindings_fail_closed() -> None:
    adapter = BlackScholesPricingAdapter()
    inputs = _inputs()
    state = adapter.bind_state(inputs, Decimal("0.25"))
    other_contract = _contract("option.call.105.jul", strike="105")

    with pytest.raises(OptionPricingAdapterError, match="contract_input_mismatch"):
        adapter.value(other_contract, state)
    with pytest.raises(
        OptionPricingAdapterError,
        match="valuation_input_hash_mismatch",
    ):
        replace(state, valuation_input_hash=_hash("a"))

    wrong_model_state = replace(state, pricing_model_hash=_hash("b"))
    with pytest.raises(OptionPricingAdapterError, match="model_hash_mismatch"):
        adapter.value(inputs.contract, wrong_model_state)

    wrong_spec_state = replace(state, specification_hash=_hash("c"))
    with pytest.raises(
        OptionPricingAdapterError,
        match="specification_hash_mismatch",
    ):
        adapter.value(inputs.contract, wrong_spec_state)


def test_model_version_and_assumptions_are_separate_hash_bound_contracts() -> None:
    model = BlackScholesModel(model_version="black_scholes_european_v2")
    with pytest.raises(OptionPricingAdapterError, match="model_version_mismatch"):
        BlackScholesPricingAdapter(model=model)

    specification = black_scholes_pricing_specification(model.model_version)
    adapter = BlackScholesPricingAdapter(
        model=model,
        specification=specification,
    )
    assert adapter.specification.implementation_version == model.model_version
    assert adapter.as_dict()["model_hash"] == model.content_hash
    assert adapter.as_dict()["specification_hash"] == adapter.specification.content_hash

    false_assumptions = replace(specification, day_count="ACT/365")
    with pytest.raises(OptionPricingAdapterError, match="day_count"):
        BlackScholesPricingAdapter(
            model=model,
            specification=false_assumptions,
        )


def test_american_expired_schema_and_volatility_inputs_fail_closed() -> None:
    adapter = BlackScholesPricingAdapter()
    american_inputs = _inputs(
        _contract(
            "option.american.call.100.jul",
            exercise_style=ExerciseStyle.AMERICAN,
        ),
        valuation_input_id="valuation.option.american.call.100.jul",
    )
    with pytest.raises(OptionPricingAdapterError, match="requires_european"):
        adapter.bind_state(american_inputs, Decimal("0.25"))

    expired_contract = _contract(
        "option.expiring.call.100",
        expiration_at=NOW,
    )
    expired_inputs = _inputs(
        expired_contract,
        valuation_input_id="valuation.option.expiring.call.100",
    )
    with pytest.raises(OptionPricingAdapterError, match="contract_expired"):
        adapter.bind_state(expired_inputs, Decimal("0.25"))

    state = adapter.bind_state(_inputs(), Decimal("0.25"))
    with pytest.raises(OptionPricingAdapterError, match="schema_version"):
        replace(state, schema_version=1)
    with pytest.raises(OptionPricingAdapterError, match="volatility_out_of_bounds"):
        adapter.bind_state(_inputs(), Decimal("6"))


def test_implied_volatility_structured_failure_is_not_returned_as_a_value() -> None:
    adapter = BlackScholesPricingAdapter()
    inputs = _inputs()
    state = adapter.bind_state(inputs, Decimal("0.25"))

    with pytest.raises(
        OptionPricingAdapterError,
        match="implied_volatility_failed:OUTSIDE_ARBITRAGE_BOUNDS",
    ):
        adapter.implied_parameter(inputs.contract, Decimal("101"), state)


def test_production_factory_derives_hash_bound_market_state_analytics() -> None:
    adapter = BlackScholesPricingAdapter()
    inputs = _inputs()
    quote = _typed_quote(inputs)
    factory = BlackScholesOptionAnalyticsFactory(
        margin_model_hash=_hash("2"),
        pricing_adapter=adapter,
    )

    mark = factory.derive(
        quote=quote,
        valuation_input=inputs,
        margin_per_contract=Decimal("50"),
        collateral_per_contract=Decimal("25"),
    )
    priced_state = adapter.bind_state(inputs, mark.implied_volatility)
    expected_greeks = adapter.greeks(inputs.contract, priced_state)

    assert mark.market_price == quote.midpoint
    assert mark.model_price == adapter.value(inputs.contract, priced_state)
    assert abs(mark.model_price - mark.market_price) <= adapter.model.price_tolerance
    assert mark.delta == expected_greeks.delta
    assert mark.gamma == expected_greeks.gamma
    assert mark.vega == expected_greeks.vega_per_vol_point
    assert mark.theta == expected_greeks.theta_per_calendar_day
    assert mark.rho == expected_greeks.rho_per_rate_point
    assert mark.model_hash == adapter.model.content_hash
    assert mark.model_specification_hash == adapter.specification.content_hash
    assert mark.valuation_input_hash == inputs.content_hash
    assert mark.source_quote_hash == quote.content_hash
    assert factory.content_hash.startswith("sha256:")

    chain = OptionChainState(
        chain_id="chain.option.call.100.jul",
        underlying_instrument_id=quote.underlying_instrument_id,
        currency=quote.currency,
        price_unit=quote.price_unit,
        quotes=(quote,),
        analytics=(mark,),
        metadata=quote.metadata,
    )
    assert chain.analytics == (mark,)


def test_production_factory_rejects_unbound_typed_quote_values_and_lineage() -> None:
    inputs = _inputs()
    quote = _typed_quote(inputs)
    factory = BlackScholesOptionAnalyticsFactory(margin_model_hash=_hash("2"))

    with pytest.raises(OptionPricingAdapterError, match="quote_value_mismatch"):
        factory.derive(
            quote=replace(quote, bid=quote.bid - Decimal("0.1")),
            valuation_input=inputs,
            margin_per_contract=Decimal("50"),
            collateral_per_contract=Decimal("25"),
        )

    unbound_metadata = replace(quote.metadata, source_hash=_hash("3"))
    with pytest.raises(OptionPricingAdapterError, match="source_quote_hash_unbound"):
        factory.derive(
            quote=replace(quote, metadata=unbound_metadata),
            valuation_input=inputs,
            margin_per_contract=Decimal("50"),
            collateral_per_contract=Decimal("25"),
        )

    with pytest.raises(OptionPricingAdapterError, match="contract_binding_mismatch"):
        factory.derive(
            quote=replace(quote, underlying_instrument_id="underlying.asset.other"),
            valuation_input=inputs,
            margin_per_contract=Decimal("50"),
            collateral_per_contract=Decimal("25"),
        )
