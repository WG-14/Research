from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from market_research.research.multi_asset.market_state import (
    BorrowQuote,
    DividendYieldAssumption,
    FundingRateQuote,
    FuturesContractQuote,
    FuturesCurveState,
    LiquidityQuote,
    MarketState,
    ObservationMetadata,
    OptionAnalyticsMark,
    OptionChainState,
    OptionContractQuote,
    OptionRight,
    QuoteCondition,
    RateQuote,
    SpotQuote,
    VolatilityPoint,
    VolatilitySurface,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    CashDelta,
    PortfolioSnapshot,
    UnifiedPortfolioLedger,
    funding_event,
    trade_event,
)
from market_research.research.multi_asset.scenarios import (
    CommonMarketProjection,
    JointMarketShock,
    JointScenarioEngine,
    PathRiskLimits,
    PathScenarioEngine,
    PathShockStep,
    PathStressScenario,
    ScenarioError,
    ShockedMarketState,
    build_common_market_projection,
)


_SOURCE_HASH = "sha256:" + ("8" * 64)
_MODEL_HASH = "sha256:" + ("9" * 64)
_MARGIN_HASH = "sha256:" + ("a" * 64)
_UNDERLYING_ID = "UNDER"
_FUTURE_ID = "UNDER.DEC26"
_OPTION_ID = "UNDER.C100"
_CURVE_ID = "UNDER.FUTURES"
_SURFACE_ID = "UNDER.VOL"
_RATE_ID = "USD.30D"
_FUNDING_ID = "UNDER.DEC26.FUNDING"


def _metadata() -> ObservationMetadata:
    return ObservationMetadata(
        observed_at="2026-06-01T11:00:00+00:00",
        knowledge_at="2026-06-01T11:00:00+00:00",
        source_hash=_SOURCE_HASH,
        calendar_id="XTEST",
        max_age_seconds=0,
    )


def _full_market_state() -> MarketState:
    metadata = _metadata()
    future = FuturesContractQuote(
        contract_id=_FUTURE_ID,
        underlying_instrument_id=_UNDERLYING_ID,
        expiry_at="2026-12-18T21:00:00+00:00",
        currency="USD",
        price_unit="USD_per_share",
        bid=Decimal("104"),
        ask=Decimal("106"),
        last=Decimal("105"),
        settlement=Decimal("105"),
        bid_size=Decimal("20"),
        ask_size=Decimal("20"),
        volume=Decimal("1000"),
        open_interest=Decimal("5000"),
        condition=QuoteCondition.OFFICIAL_SETTLEMENT,
        initial_margin_per_contract=Decimal("10"),
        collateral_per_contract=Decimal("5"),
        margin_model_hash=_MARGIN_HASH,
        metadata=metadata,
    )
    option_quote = OptionContractQuote(
        contract_id=_OPTION_ID,
        underlying_instrument_id=_UNDERLYING_ID,
        expiry_at="2026-12-18T21:00:00+00:00",
        right=OptionRight.CALL,
        strike=Decimal("100"),
        currency="USD",
        price_unit="USD_per_share",
        bid=Decimal("4"),
        ask=Decimal("6"),
        last=Decimal("5"),
        settlement=None,
        bid_size=Decimal("50"),
        ask_size=Decimal("50"),
        volume=Decimal("500"),
        open_interest=Decimal("2000"),
        condition=QuoteCondition.NORMAL,
        metadata=metadata,
    )
    option_analytics = OptionAnalyticsMark(
        contract_id=_OPTION_ID,
        underlying_instrument_id=_UNDERLYING_ID,
        expiry_at=option_quote.expiry_at,
        currency="USD",
        price_unit="USD_per_share",
        market_price=Decimal("5"),
        model_price=Decimal("5.1"),
        implied_volatility=Decimal("0.25"),
        delta=Decimal("0.5"),
        gamma=Decimal("0.02"),
        vega=Decimal("0.2"),
        theta=Decimal("-0.01"),
        rho=Decimal("0.05"),
        margin_per_contract=Decimal("2"),
        collateral_per_contract=Decimal("1"),
        model_hash=_MODEL_HASH,
        model_specification_hash=_MODEL_HASH,
        margin_model_hash=_MARGIN_HASH,
        valuation_input_hash=_SOURCE_HASH,
        source_quote_hash=option_quote.content_hash,
        metadata=metadata,
    )
    return MarketState(
        state_id="scenario.full.market.state",
        valuation_at="2026-06-01T11:00:00+00:00",
        base_currency="USD",
        calendar_ids=("XTEST",),
        spots=(
            SpotQuote(
                instrument_id=_UNDERLYING_ID,
                price=Decimal("100"),
                currency="USD",
                unit="USD_per_share",
                metadata=metadata,
            ),
        ),
        volatility_surfaces=(
            VolatilitySurface(
                surface_id=_SURFACE_ID,
                underlying_instrument_id=_UNDERLYING_ID,
                quote_currency="USD",
                points=(
                    VolatilityPoint(
                        expiry_at="2026-12-18T21:00:00+00:00",
                        strike=Decimal("100"),
                        volatility=Decimal("0.25"),
                    ),
                ),
                metadata=metadata,
            ),
        ),
        rates=(
            RateQuote(
                rate_id=_RATE_ID,
                currency="USD",
                tenor_days=30,
                rate=Decimal("0.04"),
                metadata=metadata,
            ),
        ),
        borrow_quotes=(
            BorrowQuote(
                instrument_id=_UNDERLYING_ID,
                currency="USD",
                annualized_rate=Decimal("0.02"),
                metadata=metadata,
                available_quantity=Decimal("1000"),
                quantity_unit="share",
            ),
        ),
        liquidity_quotes=(
            LiquidityQuote(
                instrument_id=_UNDERLYING_ID,
                currency="USD",
                bid=Decimal("99"),
                ask=Decimal("101"),
                price_unit="USD_per_share",
                depth_quantity=Decimal("500"),
                quantity_unit="share",
                metadata=metadata,
            ),
        ),
        futures_curves=(
            FuturesCurveState(
                curve_id=_CURVE_ID,
                underlying_instrument_id=_UNDERLYING_ID,
                currency="USD",
                price_unit="USD_per_share",
                contracts=(future,),
                metadata=metadata,
            ),
        ),
        option_chains=(
            OptionChainState(
                chain_id="UNDER.OPTIONS",
                underlying_instrument_id=_UNDERLYING_ID,
                currency="USD",
                price_unit="USD_per_share",
                quotes=(option_quote,),
                analytics=(option_analytics,),
                metadata=metadata,
            ),
        ),
        funding_rates=(
            FundingRateQuote(
                funding_rate_id=_FUNDING_ID,
                instrument_id=_FUTURE_ID,
                currency="USD",
                annualized_rate=Decimal("0.03"),
                settlement_at="2026-06-01T16:00:00+00:00",
                interval_hours=8,
                metadata=metadata,
            ),
        ),
        dividend_yields=(
            DividendYieldAssumption(
                assumption_id="UNDER.DIVIDEND",
                underlying_instrument_id=_UNDERLYING_ID,
                currency="USD",
                annualized_yield=Decimal("0.01"),
                model_hash=_MODEL_HASH,
                metadata=metadata,
            ),
        ),
    )


def _option_only_snapshot() -> PortfolioSnapshot:
    return (
        UnifiedPortfolioLedger.open(
            ledger_id="scenario.option.only",
            base_currency="USD",
        )
        .publish_many(
            (
                funding_event(
                    event_id="scenario.option.funding",
                    occurred_at="2026-06-01T09:00:00+00:00",
                    cash_deltas=(CashDelta("USD", Decimal("10000")),),
                ),
                trade_event(
                    event_id="scenario.option.open",
                    occurred_at="2026-06-01T10:00:00+00:00",
                    asset_class=AssetClass.OPTION,
                    instrument_id=_OPTION_ID,
                    currency="USD",
                    quantity_delta=Decimal("1"),
                    price=Decimal("4"),
                    multiplier=Decimal("100"),
                ),
            )
        )
        .replay()
    )


@dataclass(frozen=True)
class _CommonStateOptionRepricer:
    def reprice(
        self,
        position: object,
        *,
        market_state: object,
        shocked_state: ShockedMarketState,
    ) -> Decimal:
        del position, market_state
        underlying = shocked_state.price_for(_UNDERLYING_ID)
        volatility = shocked_state.volatility_for(
            surface_id=_SURFACE_ID,
            expiry_at="2026-12-18T21:00:00+00:00",
            strike=Decimal("100"),
        )
        rate = shocked_state.factor_value("rate", _RATE_ID)
        return ((underlying / Decimal("25")) + volatility + rate).quantize(
            Decimal("0.000001")
        )


def _combined_shock(*, scenario_id: str) -> JointMarketShock:
    return JointMarketShock(
        scenario_id=scenario_id,
        price_returns=((_UNDERLYING_ID, Decimal("-0.10")),),
        volatility_shifts=((_SURFACE_ID, Decimal("0.01")),),
        volatility_skew_shifts=((_SURFACE_ID, Decimal("0.02")),),
        volatility_term_shifts=((_SURFACE_ID, Decimal("0.03")),),
        rate_shifts=((_RATE_ID, Decimal("0.01")),),
        dividend_yield_shifts=((_UNDERLYING_ID, Decimal("0.002")),),
        borrow_rate_shifts=((_UNDERLYING_ID, Decimal("0.003")),),
        funding_rate_shifts=((_FUNDING_ID, Decimal("-0.001")),),
        futures_curve_returns=((_CURVE_ID, Decimal("0.10")),),
        futures_basis_shifts=((_FUTURE_ID, Decimal("1")),),
        spread_multipliers=((_OPTION_ID, Decimal("2")),),
        source_hashes=(_SOURCE_HASH,),
    )


def test_option_only_portfolio_reprices_from_full_hash_bound_common_state() -> None:
    state = _full_market_state()
    snapshot = _option_only_snapshot()
    original_hash = state.state_hash()
    engine = JointScenarioEngine()

    result = engine.evaluate(
        snapshot,
        market_state=state,
        shock=_combined_shock(scenario_id="combined.full.state"),
        repricers={_OPTION_ID: _CommonStateOptionRepricer()},
    )

    shocked = result.shocked_state
    projection = build_common_market_projection(
        state,
        fallback_marks={_OPTION_ID: Decimal("4")},
    )
    assert isinstance(projection, CommonMarketProjection)
    assert shocked.base_projection_hash == projection.content_hash
    assert shocked.price_for(_UNDERLYING_ID) == Decimal("90")
    assert shocked.price_for(_FUTURE_ID) == Decimal("116.50")
    assert shocked.factor_value("rate", _RATE_ID) == Decimal("0.05")
    assert shocked.factor_value("dividend_yield", _UNDERLYING_ID) == Decimal("0.012")
    assert shocked.factor_value("borrow_rate", _UNDERLYING_ID) == Decimal("0.023")
    assert shocked.factor_value("funding_rate", _FUNDING_ID) == Decimal("0.029")
    assert shocked.factor_value("spread", _OPTION_ID) == Decimal("4")
    assert shocked.factor_value("futures_basis", _FUTURE_ID) == Decimal("26.50")
    assert result.position_results[0].base_mark == Decimal("5")
    assert result.position_results[0].repricer == "_CommonStateOptionRepricer"
    assert shocked.source_component_hashes
    assert state.state_hash() == original_hash
    assert (
        engine.evaluate(
            snapshot,
            market_state=state,
            shock=_combined_shock(scenario_id="combined.full.state"),
            repricers={_OPTION_ID: _CommonStateOptionRepricer()},
        ).content_hash
        == result.content_hash
    )


def test_path_compounds_curve_spread_and_additive_common_factors() -> None:
    state = _full_market_state()
    snapshot = _option_only_snapshot()
    first = PathShockStep(
        sequence=1,
        step_id="common.factor.1",
        effective_at="2026-06-02T11:00:00+00:00",
        predecessor_hash=state.state_hash(),
        shock=JointMarketShock(
            scenario_id="common.factor.increment.1",
            price_returns=((_UNDERLYING_ID, Decimal("-0.10")),),
            volatility_skew_shifts=((_SURFACE_ID, Decimal("0.01")),),
            volatility_term_shifts=((_SURFACE_ID, Decimal("0.02")),),
            rate_shifts=((_RATE_ID, Decimal("0.003")),),
            dividend_yield_shifts=((_UNDERLYING_ID, Decimal("0.001")),),
            borrow_rate_shifts=((_UNDERLYING_ID, Decimal("0.003")),),
            funding_rate_shifts=((_FUNDING_ID, Decimal("-0.001")),),
            futures_curve_returns=((_CURVE_ID, Decimal("0.10")),),
            futures_basis_shifts=((_FUTURE_ID, Decimal("1")),),
            spread_multipliers=((_OPTION_ID, Decimal("2")),),
        ),
    )
    second = PathShockStep(
        sequence=2,
        step_id="common.factor.2",
        effective_at="2026-06-03T11:00:00+00:00",
        predecessor_hash=first.content_hash,
        shock=JointMarketShock(
            scenario_id="common.factor.increment.2",
            volatility_skew_shifts=((_SURFACE_ID, Decimal("0.02")),),
            volatility_term_shifts=((_SURFACE_ID, Decimal("0.01")),),
            rate_shifts=((_RATE_ID, Decimal("0.002")),),
            dividend_yield_shifts=((_UNDERLYING_ID, Decimal("0.002")),),
            borrow_rate_shifts=((_UNDERLYING_ID, Decimal("0.004")),),
            funding_rate_shifts=((_FUNDING_ID, Decimal("0.002")),),
            futures_curve_returns=((_CURVE_ID, Decimal("-0.10")),),
            futures_basis_shifts=((_FUTURE_ID, Decimal("2")),),
            spread_multipliers=((_OPTION_ID, Decimal("1.5")),),
        ),
    )
    scenario = PathStressScenario(
        path_id="common.factor.path",
        expected_base_state_hash=state.state_hash(),
        expected_ledger_hash=snapshot.ledger_hash,
        steps=(first, second),
        risk_limits=PathRiskLimits(maximum_drawdown_fraction=Decimal("1")),
    )

    result = PathScenarioEngine().evaluate(
        snapshot,
        market_state=state,
        scenario=scenario,
        repricers={_OPTION_ID: _CommonStateOptionRepricer()},
    )
    shocked = result.steps[-1].scenario_result.shocked_state

    assert shocked.price_for(_UNDERLYING_ID) == Decimal("90")
    assert shocked.price_for(_FUTURE_ID) == Decimal("106.850")
    assert shocked.futures_curve_returns == ((_CURVE_ID, Decimal("-0.01")),)
    assert shocked.futures_basis_shifts == ((_FUTURE_ID, Decimal("3")),)
    assert shocked.volatility_skew_shifts == ((_SURFACE_ID, Decimal("0.03")),)
    assert shocked.volatility_term_shifts == ((_SURFACE_ID, Decimal("0.03")),)
    assert shocked.factor_value("rate", _RATE_ID) == Decimal("0.045")
    assert shocked.factor_value("dividend_yield", _UNDERLYING_ID) == Decimal("0.013")
    assert shocked.factor_value("borrow_rate", _UNDERLYING_ID) == Decimal("0.027")
    assert shocked.factor_value("funding_rate", _FUNDING_ID) == Decimal("0.031")
    assert shocked.factor_value("spread", _OPTION_ID) == Decimal("6.0")
    assert (
        PathScenarioEngine()
        .evaluate(
            snapshot,
            market_state=state,
            scenario=scenario,
            repricers={_OPTION_ID: _CommonStateOptionRepricer()},
        )
        .content_hash
        == result.content_hash
    )


@pytest.mark.parametrize(
    ("shock", "error"),
    (
        (
            JointMarketShock(
                scenario_id="unknown.price",
                price_returns=(("UNKNOWN", Decimal("0.1")),),
            ),
            "scenario_price_target_not_held:UNKNOWN",
        ),
        (
            JointMarketShock(
                scenario_id="unknown.dividend",
                dividend_yield_shifts=(("UNKNOWN", Decimal("0.01")),),
            ),
            "scenario_dividend_yield_target_unknown:UNKNOWN",
        ),
        (
            JointMarketShock(
                scenario_id="unknown.spread",
                spread_multipliers=(("UNKNOWN", Decimal("2")),),
            ),
            "scenario_spread_target_unknown:UNKNOWN",
        ),
        (
            JointMarketShock(
                scenario_id="unknown.curve",
                futures_curve_returns=(("UNKNOWN", Decimal("0.1")),),
            ),
            "scenario_futures_curve_target_unknown:UNKNOWN",
        ),
    ),
)
def test_common_state_shocks_fail_closed_for_unknown_targets(
    shock: JointMarketShock,
    error: str,
) -> None:
    with pytest.raises(ScenarioError, match=error):
        JointScenarioEngine().evaluate(
            _option_only_snapshot(),
            market_state=_full_market_state(),
            shock=shock,
            repricers={_OPTION_ID: _CommonStateOptionRepricer()},
        )
