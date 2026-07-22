from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

import pytest

from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.domain import (
    ContractSpecification,
    Instrument,
    InstrumentRegistry,
)
from market_research.research.multi_asset.exposure import (
    ExposureDimension,
    ExposureEngine,
    ExposureEngineError,
    ExposurePosition,
    FuturesValuationAdapter,
    LiquidityBucket,
    OptionValuationAdapter,
    ProductCatalog,
    SpotValuationAdapter,
    UnitValuation,
    _build_totals,
)
from market_research.research.multi_asset.market_state import (
    LiquidityQuote,
    MarketState,
)
from tests.test_multi_asset_domain import (
    _derivative_components,
    _market_state,
    _metadata,
    _registry,
)


def _hash(label: str) -> str:
    return sha256_prefixed({"fixture": label}, label="multi_asset_exposure_test")


def _catalog() -> InstrumentRegistry:
    source = _registry()
    instruments = tuple(replace(item, currency="USD") for item in source.instruments)
    return replace(
        source,
        economic_underlyings=tuple(
            replace(item, currency="USD") for item in source.economic_underlyings
        ),
        instruments=instruments,
        listings=tuple(
            replace(
                item,
                trading_currency="USD",
                price_unit="USD_per_coin",
            )
            for item in source.listings
        ),
        contract_specifications=tuple(
            replace(item, settlement_currency="USD")
            for item in source.contract_specifications
        ),
    )


def _state() -> MarketState:
    source = _market_state()
    metadata = _metadata()
    futures_curve, option_chain = _derivative_components()
    derivative_liquidity = (
        LiquidityQuote(
            instrument_id="inst_btc_future_dec26",
            currency="USD",
            bid=Decimal("99"),
            ask=Decimal("101"),
            price_unit="USD_per_coin",
            depth_quantity=Decimal("0.5"),
            quantity_unit="contract",
            metadata=metadata,
        ),
        LiquidityQuote(
            instrument_id="inst_btc_option_dec26",
            currency="USD",
            bid=Decimal("4.5"),
            ask=Decimal("5.5"),
            price_unit="USD_per_future_contract",
            depth_quantity=Decimal("30"),
            quantity_unit="contract",
            metadata=metadata,
        ),
    )
    return replace(
        source,
        liquidity_quotes=source.liquidity_quotes + derivative_liquidity,
        futures_curves=(futures_curve,),
        option_chains=(option_chain,),
    )


def _positions() -> tuple[ExposurePosition, ...]:
    common = {
        "currency": "USD",
        "opened_at": "2026-01-01T00:00:00+00:00",
    }
    return (
        ExposurePosition(
            position_id="position_spot_lot_long",
            instrument_id="inst_btc_spot",
            quantity=Decimal("3"),
            quantity_unit="coin",
            multiplier=Decimal("1"),
            source_hash=_hash("spot-lot-long"),
            **common,
        ),
        ExposurePosition(
            position_id="position_spot_lot_short",
            instrument_id="inst_btc_spot",
            quantity=Decimal("-1"),
            quantity_unit="coin",
            multiplier=Decimal("1"),
            source_hash=_hash("spot-lot-short"),
            **common,
        ),
        ExposurePosition(
            position_id="position_future_short",
            instrument_id="inst_btc_future_dec26",
            quantity=Decimal("-1"),
            quantity_unit="contract",
            multiplier=Decimal("1"),
            source_hash=_hash("future-short"),
            **common,
        ),
        ExposurePosition(
            position_id="position_option_long",
            instrument_id="inst_btc_option_dec26",
            quantity=Decimal("3"),
            quantity_unit="contract",
            multiplier=Decimal("1"),
            source_hash=_hash("option-long"),
            **common,
        ),
    )


def _engine() -> ExposureEngine:
    catalog: ProductCatalog = _catalog()
    state = _state()
    future = state.futures_contract_quote("inst_btc_future_dec26")
    option = state.option_analytics_mark("inst_btc_option_dec26")
    return ExposureEngine.with_default_spot(
        product_catalog=catalog,
        derivative_adapters=(
            FuturesValuationAdapter(
                margin_model_hash=future.margin_model_hash,
            ),
            OptionValuationAdapter(
                pricing_model_hash=option.model_hash,
                model_specification_hash=option.model_specification_hash,
                margin_model_hash=option.margin_model_hash,
            ),
        ),
    )


def test_engine_revalues_positions_and_calculates_cross_asset_totals() -> None:
    snapshot = _engine().evaluate(
        snapshot_id="exposure_btc_portfolio",
        positions=_positions(),
        market_state=_state(),
    )

    assert snapshot.schema_version == 2
    assert snapshot.totals.position_count == 4
    assert snapshot.totals.market_value == Decimal("279500")
    assert snapshot.totals.gross_notional == Decimal("1040000")
    assert snapshot.totals.net_notional == Decimal("520000")
    assert snapshot.totals.offset_notional == Decimal("520000")
    assert snapshot.totals.offset_ratio == Decimal("0.5")
    assert snapshot.totals.delta == Decimal("3250")
    assert snapshot.totals.gamma == Decimal("390")
    assert snapshot.totals.vega == Decimal("780")
    assert snapshot.totals.theta == Decimal("-39")
    assert snapshot.totals.rho == Decimal("195")
    assert snapshot.totals.margin == Decimal("20800")
    assert snapshot.totals.collateral == Decimal("10400")

    source_spot = next(
        item
        for item in snapshot.source_position_sums
        if item.instrument_id == "inst_btc_spot"
    )
    assert source_spot.signed_quantity == Decimal("2")
    assert source_spot.gross_quantity == Decimal("4")
    assert source_spot.position_count == 2
    assert all(item.content_hash.startswith("sha256:") for item in snapshot.positions)
    assert snapshot.evidence.source_positions_hash.startswith("sha256:")
    assert snapshot.evidence.content_hash.startswith("sha256:")
    assert snapshot.content_hash.startswith("sha256:")


def test_offset_notional_never_nets_unrelated_economic_underlyings() -> None:
    snapshot = _engine().evaluate(
        snapshot_id="exposure_unrelated_offset_guard",
        positions=_positions(),
        market_state=_state(),
    )
    long = replace(
        snapshot.positions[0],
        position_id="unrelated_long",
        underlying_id="underlying_alpha",
        gross_notional_base=Decimal("100"),
        net_notional_base=Decimal("100"),
    )
    short = replace(
        snapshot.positions[1],
        position_id="unrelated_short",
        underlying_id="underlying_beta",
        gross_notional_base=Decimal("100"),
        net_notional_base=Decimal("-100"),
    )

    totals = _build_totals((long, short), "USD")

    assert totals.gross_notional == Decimal("200")
    assert totals.net_notional == Decimal("0")
    assert totals.offset_notional == Decimal("0")
    assert totals.offset_ratio == Decimal("0")


def test_engine_builds_underlying_currency_expiry_and_liquidity_buckets() -> None:
    snapshot = _engine().evaluate(
        snapshot_id="exposure_btc_buckets",
        positions=_positions(),
        market_state=_state(),
    )

    underlying = snapshot.bucket(ExposureDimension.UNDERLYING, "underlying_btc")
    assert underlying.totals.gross_notional == snapshot.totals.gross_notional
    assert underlying.totals.offset_notional == Decimal("520000")
    assert snapshot.bucket(
        ExposureDimension.CURRENCY, "USD"
    ).totals.market_value == Decimal("279500")
    assert snapshot.bucket(
        ExposureDimension.EXPIRY, "NON_EXPIRING"
    ).totals.gross_notional == Decimal("520000")
    assert snapshot.concentration(ExposureDimension.UNDERLYING).ratio == Decimal("1")
    expiry_concentration = snapshot.concentration(ExposureDimension.EXPIRY)
    assert expiry_concentration.largest_bucket_key == "NON_EXPIRING"
    assert expiry_concentration.ratio == Decimal("0.5")

    buckets = {item.position_id: item.liquidity_bucket for item in snapshot.positions}
    assert buckets == {
        "position_future_short": LiquidityBucket.INSUFFICIENT,
        "position_option_long": LiquidityBucket.DEEP,
        "position_spot_lot_long": LiquidityBucket.CONSTRAINED,
        "position_spot_lot_short": LiquidityBucket.ADEQUATE,
    }
    option = next(
        item
        for item in snapshot.positions
        if item.position_id == "position_option_long"
    )
    assert option.liquidity_days == Decimal("0.1")


def test_market_state_change_forces_fresh_future_and_option_revaluation() -> None:
    engine = _engine()
    state = _state()
    original = engine.evaluate(
        snapshot_id="exposure_btc_reprice",
        positions=_positions(),
        market_state=state,
    )
    repriced_spots = tuple(
        replace(item, price=Decimal("110"))
        if item.instrument_id == "inst_btc_spot"
        else item
        for item in state.spots
    )
    future = state.futures_curves[0].contracts[0]
    repriced_future = replace(
        future,
        bid=Decimal("109"),
        ask=Decimal("111"),
        last=Decimal("110"),
        settlement=Decimal("110"),
    )
    repriced_curve = replace(
        state.futures_curves[0],
        contracts=(repriced_future,),
    )
    option_quote = state.option_chains[0].quotes[0]
    repriced_option_quote = replace(
        option_quote,
        bid=Decimal("5"),
        ask=Decimal("6"),
        last=Decimal("5.5"),
    )
    repriced_option_mark = replace(
        state.option_chains[0].analytics[0],
        market_price=Decimal("5.5"),
        model_price=Decimal("5.7"),
        source_quote_hash=repriced_option_quote.content_hash,
    )
    repriced_chain = replace(
        state.option_chains[0],
        quotes=(repriced_option_quote,),
        analytics=(repriced_option_mark,),
    )
    repriced_state = replace(
        state,
        spots=repriced_spots,
        futures_curves=(repriced_curve,),
        option_chains=(repriced_chain,),
    )
    repriced = engine.evaluate(
        snapshot_id="exposure_btc_reprice",
        positions=_positions(),
        market_state=repriced_state,
    )

    assert repriced.totals.market_value == Decimal("307450")
    assert repriced.totals.gross_notional == Decimal("1144000")
    assert repriced.totals.net_notional == Decimal("572000")
    assert repriced.evidence.market_state_hash != original.evidence.market_state_hash
    assert repriced.content_hash != original.content_hash
    original_valuations = {item.valuation_hash for item in original.positions}
    assert all(
        item.valuation_hash not in original_valuations for item in repriced.positions
    )


def test_snapshot_hash_is_order_stable_and_tampering_breaks_invariants() -> None:
    engine = _engine()
    positions = _positions()
    state = _state()
    snapshot = engine.evaluate(
        snapshot_id="exposure_btc_integrity",
        positions=positions,
        market_state=state,
    )
    reordered = engine.evaluate(
        snapshot_id="exposure_btc_integrity",
        positions=tuple(reversed(positions)),
        market_state=state,
    )
    assert reordered.content_hash == snapshot.content_hash

    with pytest.raises(ExposureEngineError, match="totals_invariant_failed"):
        replace(
            snapshot,
            totals=replace(
                snapshot.totals,
                market_value=snapshot.totals.market_value + Decimal("1"),
            ),
        )
    forged_evidence = replace(
        snapshot.evidence,
        source_position_sums_hash=_hash("forged-source-sum"),
    )
    with pytest.raises(ExposureEngineError, match="evidence_binding_mismatch"):
        replace(snapshot, evidence=forged_evidence)


def test_engine_rejects_position_unit_multiplier_and_currency_mismatch() -> None:
    engine = _engine()
    state = _state()
    positions = _positions()
    with pytest.raises(ExposureEngineError, match="quantity_unit_mismatch"):
        engine.evaluate(
            snapshot_id="exposure_bad_unit",
            positions=(replace(positions[0], quantity_unit="share"),),
            market_state=state,
        )
    with pytest.raises(ExposureEngineError, match="multiplier_mismatch"):
        engine.evaluate(
            snapshot_id="exposure_bad_multiplier",
            positions=(replace(positions[2], multiplier=Decimal("2")),),
            market_state=state,
        )
    with pytest.raises(ExposureEngineError, match="position_currency_mismatch"):
        engine.evaluate(
            snapshot_id="exposure_bad_currency",
            positions=(replace(positions[3], currency="KRW"),),
            market_state=state,
        )


def test_production_derivative_adapters_apply_multiplier_and_fx_once() -> None:
    catalog = _catalog()
    catalog = replace(
        catalog,
        contract_specifications=tuple(
            replace(
                item,
                contract_multiplier=(
                    Decimal("25")
                    if item.instrument_id == "inst_btc_future_dec26"
                    else Decimal("100")
                ),
            )
            for item in catalog.contract_specifications
        ),
    )
    state = _state()
    future_quote = replace(
        state.futures_curves[0].contracts[0],
        initial_margin_per_contract=Decimal("5000"),
        collateral_per_contract=Decimal("2500"),
    )
    curve = replace(state.futures_curves[0], contracts=(future_quote,))
    option_mark = replace(
        state.option_chains[0].analytics[0],
        margin_per_contract=Decimal("500"),
        collateral_per_contract=Decimal("500"),
    )
    chain = replace(state.option_chains[0], analytics=(option_mark,))
    state = replace(
        state,
        futures_curves=(curve,),
        option_chains=(chain,),
    )
    engine = ExposureEngine.with_default_spot(
        product_catalog=catalog,
        derivative_adapters=(
            FuturesValuationAdapter(
                margin_model_hash=future_quote.margin_model_hash,
            ),
            OptionValuationAdapter(
                pricing_model_hash=option_mark.model_hash,
                model_specification_hash=option_mark.model_specification_hash,
                margin_model_hash=option_mark.margin_model_hash,
            ),
        ),
    )
    positions = _positions()
    future_position = replace(
        positions[2],
        quantity=Decimal("2"),
        multiplier=Decimal("25"),
    )
    option_position = replace(
        positions[3],
        quantity=Decimal("1"),
        multiplier=Decimal("100"),
    )
    snapshot = engine.evaluate(
        snapshot_id="exposure_scaled_derivatives",
        positions=(future_position, option_position),
        market_state=state,
    )
    by_id = {item.instrument_id: item for item in snapshot.positions}
    future = by_id[future_position.instrument_id]
    option = by_id[option_position.instrument_id]

    assert future.gross_notional_base == Decimal("6500000")
    assert future.margin_base == Decimal("13000000")
    assert future.collateral_base == Decimal("6500000")
    assert option.market_value_base == Decimal("650000")
    assert option.gross_notional_base == Decimal("13000000")
    assert option.delta_base == Decimal("65000")
    assert option.gamma_base == Decimal("13000")
    assert option.vega_base == Decimal("26000")
    assert option.margin_base == Decimal("650000")


def test_production_option_adapter_rejects_model_and_missing_state_bindings() -> None:
    state = _state()
    option_mark = state.option_analytics_mark("inst_btc_option_dec26")
    bad_model_engine = ExposureEngine.with_default_spot(
        product_catalog=_catalog(),
        derivative_adapters=(
            FuturesValuationAdapter(
                margin_model_hash=state.futures_contract_quote(
                    "inst_btc_future_dec26"
                ).margin_model_hash,
            ),
            OptionValuationAdapter(
                pricing_model_hash=_hash("wrong-option-model"),
                model_specification_hash=option_mark.model_specification_hash,
                margin_model_hash=option_mark.margin_model_hash,
            ),
        ),
    )
    option = _positions()[3]
    with pytest.raises(ExposureEngineError, match="pricing_model_hash_mismatch"):
        bad_model_engine.evaluate(
            snapshot_id="exposure_wrong_option_model",
            positions=(option,),
            market_state=state,
        )

    missing_derivatives = replace(state, futures_curves=(), option_chains=())
    with pytest.raises(
        ExposureEngineError,
        match="option_contract_quote_not_unique",
    ):
        _engine().evaluate(
            snapshot_id="exposure_missing_option_state",
            positions=(option,),
            market_state=missing_derivatives,
        )


@dataclass(frozen=True, slots=True)
class StaleBoundFutureAdapter(FuturesValuationAdapter):
    def value(
        self,
        *,
        position: ExposurePosition,
        instrument: Instrument,
        contract_specification: ContractSpecification | None,
        market_state: MarketState,
        product_catalog_hash: str,
    ) -> UnitValuation:
        valid = FuturesValuationAdapter.value(
            self,
            position=position,
            instrument=instrument,
            contract_specification=contract_specification,
            market_state=market_state,
            product_catalog_hash=product_catalog_hash,
        )
        return replace(valid, market_state_hash=_hash("stale-market-state"))


def test_adapter_result_must_bind_exact_market_state_and_catalog() -> None:
    state = _state()
    future_quote = state.futures_contract_quote("inst_btc_future_dec26")
    option_mark = state.option_analytics_mark("inst_btc_option_dec26")
    engine = ExposureEngine.with_default_spot(
        product_catalog=_catalog(),
        derivative_adapters=(
            StaleBoundFutureAdapter(
                margin_model_hash=future_quote.margin_model_hash,
            ),
            OptionValuationAdapter(
                pricing_model_hash=option_mark.model_hash,
                model_specification_hash=option_mark.model_specification_hash,
                margin_model_hash=option_mark.margin_model_hash,
            ),
        ),
    )
    future = next(
        item for item in _positions() if item.instrument_id == "inst_btc_future_dec26"
    )
    with pytest.raises(ExposureEngineError, match="market_state_hash_mismatch"):
        engine.evaluate(
            snapshot_id="exposure_stale_adapter",
            positions=(future,),
            market_state=state,
        )


def test_spot_adapter_is_state_backed_not_caller_precomputed() -> None:
    catalog = _catalog()
    adapter = SpotValuationAdapter()
    state = _state()
    position = _positions()[0]
    instrument = catalog.instrument_as_of(position.instrument_id, state.valuation_at)
    assert instrument is not None

    valuation = adapter.value(
        position=position,
        instrument=instrument,
        contract_specification=None,
        market_state=state,
        product_catalog_hash=catalog.contract_hash(),
    )
    assert valuation.mark_price == state.spot_price(position.instrument_id).price
    assert valuation.market_state_hash == state.state_hash()
