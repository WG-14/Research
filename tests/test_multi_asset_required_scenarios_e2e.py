from __future__ import annotations

import json
import platform
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.derivatives.common import AvailabilityTimes
from market_research.research.derivatives.futures import (
    ContractChainSnapshot,
    ContractQuote,
    ContinuousFuturesPoint,
    FuturesContract,
    FuturesCostPolicy,
    FuturesFill,
    MarginCallAction,
    MarginSimulationPolicy,
    OrderSide,
    RollExecution,
    SessionType,
    SettlementEvent,
    SettlementType as FuturesSettlementType,
)
from market_research.research.derivatives.options import (
    ExerciseStyle,
    OptionChainSnapshot,
    OptionContract,
    OptionQuote,
    OptionSettlementInput,
    OptionType,
    SettlementType as OptionSettlementType,
    TransactionSide,
    ValuationInputSnapshot,
    position_from_fill,
    simulate_option_fill,
    simulate_option_lifecycle,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.instrument_kinds import InstrumentKind
from market_research.research.multi_asset.accounting import (
    LedgerPnlReconciliation,
    ReportLedgerReconciliation,
    ReportPnlSummary,
    encode_report_payload,
    report_payload_hash,
)
from market_research.research.multi_asset.costs import (
    ExecutionContext,
    ExecutionSide,
    LinearExecutionCostModel,
)
from market_research.research.multi_asset.data import (
    AppendOnlyBitemporalStore,
    BitemporalRecord,
    DataLayer,
    DataLineage,
    ObservationClocks,
)
from market_research.research.multi_asset.domain import (
    ContractSpecification,
    EconomicUnderlying,
    EffectivePeriod,
    Instrument,
    InstrumentRegistry,
    InstrumentRelationship,
    InstrumentRelationshipType,
    Issuer,
    Listing,
    SettlementType as DomainSettlementType,
    SourceReference,
    SymbolAlias,
)
from market_research.research.multi_asset.evidence import (
    ResearchEvidenceBindings,
    ScenarioObjectHashes,
    compare_studies,
    evidence_hash,
    publish_validated_study,
    scenario_object_hashes,
)
from market_research.research.multi_asset.exposure import (
    ExposureEngine,
    ExposurePosition,
    FuturesValuationAdapter,
    OptionValuationAdapter,
    PortfolioExposureSnapshot,
    ProductCatalog,
)
from market_research.research.multi_asset.expression import (
    DEFAULT_EXPRESSION_POLICY,
    DesiredEconomicPayoff,
    Direction,
    EconomicHypothesis,
    ExecutionMode,
    ExpectedMarketDistribution,
    ExpressionCandidate,
    ExpressionKind,
    InstrumentChoice,
    InstrumentExpressionEngine,
    ExpressionDecision,
    LegRole,
    LegSelectionRule,
    ProductKind,
    ScenarioRange,
    StrategyTargets,
)
from market_research.research.multi_asset.futures_path import (
    ExistingFuturesCostPolicyAdapter,
    FuturesReferenceHistory,
    FuturesReferenceSnapshot,
    FuturesCurveSnapshot,
    RollPlanningPolicy,
    adapt_existing_futures_contract,
    adapt_existing_margin_policy,
    build_futures_curve_snapshot,
    plan_exposure_preserving_roll,
    reconcile_existing_futures_pnl,
    select_roll_target,
    trace_continuous_signal,
)
from market_research.research.multi_asset.market_state import (
    BorrowQuote,
    CurvePoint,
    FuturesContractQuote,
    FuturesCurveState,
    LiquidityQuote,
    MarketState,
    ObservationMetadata,
    OptionChainState,
    OptionContractQuote,
    OptionRight as MarketStateOptionRight,
    QuoteCondition,
    RateQuote,
    SpotQuote,
    VolatilityPoint,
    VolatilitySurface,
    YieldCurve,
)
from market_research.research.multi_asset.option_path import (
    CalculatedOptionDelta,
    DEFAULT_OPTION_CLEANING_POLICY,
    DeltaFallback,
    ForwardEstimate,
    ForwardMethod,
    OptionChainCleaner,
    CleanedOptionChain,
    OptionAttributionPolicy,
    OptionPathMark,
    OptionRight,
    OptionSelectionPolicy,
    OptionSelectionDecision,
    RawOptionObservation,
    attribute_option_path,
    select_option_contract,
)
from market_research.research.multi_asset.option_pricing import (
    BlackScholesOptionAnalyticsFactory,
    BlackScholesPricingAdapter,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    CashDelta,
    PortfolioEventDraft,
    PortfolioEvent,
    PortfolioEventType,
    PortfolioSnapshot,
    PortfolioValuation,
    PositionView,
    UnifiedPortfolioLedger,
    adapt_corporate_action_application,
    adapt_futures_fill,
    adapt_futures_settlement,
    adapt_option_fill,
    adapt_option_lifecycle,
    collateral_income_event,
    cost_events_from_breakdown,
    funding_event,
    mark_event,
    trade_event,
)
from market_research.research.multi_asset.scenarios import (
    JointMarketShock,
    JointScenarioResult,
    JointScenarioEngine,
)
from market_research.research.multi_asset.spot import (
    CashBalance as SpotCashBalance,
    CorporateAction,
    CorporateActionType,
    PointInTimeSpotUniverse,
    SpotBook,
    SpotPosition,
    UniverseMembership,
    apply_corporate_action,
)
from market_research.research.multi_asset.study import (
    FuturesScenarioTrace,
    FuturesSourceMapping,
    IntegratedLegResult,
    IntegratedScenarioTrace,
    OptionScenarioTrace,
    ReproducibilityScenarioTrace,
    ScenarioAccounting,
    SpotScenarioTrace,
    build_validated_multi_asset_study,
    reproduction_object_hashes,
)
from market_research.settings import ResearchSettings


SPOT_ID = "inst_btc_spot"
OLD_FUTURE_ID = "inst_btc_future_apr26"
NEW_FUTURE_ID = "inst_btc_future_may26"
FAR_FUTURE_ID = "inst_btc_future_jun26"
OPTION_ID = "inst_btc_put45_apr26"
ALT_OPTION_ID = "inst_btc_put50_apr26"
UNDERLYING_ID = "underlying_btc"
CALENDAR_ID = "calendar_xoff"
DECISION = datetime(2026, 1, 2, 15, tzinfo=UTC)
FUTURES_AS_OF = "2026-03-10T16:00:00Z"
OPTION_DECISION = datetime(2026, 3, 10, 17, tzinfo=UTC)
FINAL_MARK_AT = datetime(2026, 3, 13, 18, tzinfo=UTC)
OPTION_EXPIRY = datetime(2026, 4, 24, 17, tzinfo=UTC)


def _hash(label: str) -> str:
    return sha256_prefixed({"required_scenario_fixture": label}, label="e2e-input")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _availability(at: str) -> AvailabilityTimes:
    return AvailabilityTimes(
        event_at=at,
        published_at=at,
        provider_received_at=at,
        system_received_at=at,
        processed_at=at,
    )


def _registry() -> InstrumentRegistry:
    validity = EffectivePeriod(
        "2026-01-01T00:00:00+00:00",
        "2027-01-01T00:00:00+00:00",
    )
    source = SourceReference(
        source_id="reviewed_product_master",
        source_version="v2",
        content_hash=_hash("product-master"),
        observed_at="2025-12-15T00:00:00+00:00",
        source_uri="/var/lib/market-research-inputs/product-master-v2.json",
    )
    underlying = EconomicUnderlying(
        underlying_id=UNDERLYING_ID,
        name="Bitcoin research underlying",
        asset_class="digital_asset",
        unit="coin",
        currency="USD",
        validity=validity,
        source=source,
    )
    issuer = Issuer(
        issuer_id="issuer_reviewed_venue",
        legal_name="Reviewed Research Venue",
        jurisdiction="US",
        validity=validity,
        source=source,
    )

    def instrument(instrument_id: str, kind: InstrumentKind, name: str) -> Instrument:
        return Instrument(
            instrument_id=instrument_id,
            kind=kind,
            name=name,
            economic_underlying_id=UNDERLYING_ID,
            issuer_id=issuer.issuer_id,
            currency="USD",
            unit="coin" if kind is InstrumentKind.SPOT else "contract",
            validity=validity,
            source=source,
        )

    instruments = (
        instrument(SPOT_ID, InstrumentKind.SPOT, "BTC spot research listing"),
        instrument(OLD_FUTURE_ID, InstrumentKind.FUTURE, "BTC April future"),
        instrument(NEW_FUTURE_ID, InstrumentKind.FUTURE, "BTC May future"),
        instrument(FAR_FUTURE_ID, InstrumentKind.FUTURE, "BTC June future"),
        instrument(OPTION_ID, InstrumentKind.OPTION, "BTC April 45 put"),
        instrument(ALT_OPTION_ID, InstrumentKind.OPTION, "BTC April 50 put"),
    )
    expiries = {
        OLD_FUTURE_ID: "2026-04-09T00:00:00+00:00",
        NEW_FUTURE_ID: "2026-05-09T00:00:00+00:00",
        FAR_FUTURE_ID: "2026-06-08T00:00:00+00:00",
        OPTION_ID: _iso(OPTION_EXPIRY),
        ALT_OPTION_ID: _iso(OPTION_EXPIRY),
    }
    specifications = tuple(
        ContractSpecification(
            contract_specification_id=f"spec_{instrument_id}",
            instrument_id=instrument_id,
            contract_multiplier=(
                Decimal("25") if "future" in instrument_id else Decimal("100")
            ),
            contract_unit="coin",
            settlement_type=DomainSettlementType.CASH,
            settlement_currency="USD",
            expiry_at=expiry,
            last_trade_at=expiry,
            exercise_style=("EUROPEAN" if "put" in instrument_id else None),
            validity=validity,
            source=source,
        )
        for instrument_id, expiry in expiries.items()
    )
    relationships = tuple(
        InstrumentRelationship(
            relationship_id=f"rel_{instrument_id}_underlying",
            source_instrument_id=instrument_id,
            target_instrument_id=SPOT_ID,
            relationship_type=(
                InstrumentRelationshipType.FUTURE_UNDERLYING
                if "future" in instrument_id
                else InstrumentRelationshipType.OPTION_UNDERLYING
            ),
            validity=validity,
            source=source,
        )
        for instrument_id in expiries
    )
    listing = Listing(
        listing_id="listing_btc_spot_xoff",
        instrument_id=SPOT_ID,
        venue_mic="XOFF",
        symbol="BTCUSD",
        trading_currency="USD",
        price_unit="USD_per_coin",
        quantity_unit="coin",
        calendar_id=CALENDAR_ID,
        validity=validity,
        source=source,
    )
    return InstrumentRegistry(
        economic_underlyings=(underlying,),
        issuers=(issuer,),
        instruments=instruments,
        listings=(listing,),
        contract_specifications=specifications,
        symbol_aliases=(
            SymbolAlias(
                alias_id="alias_btc_prepared_vendor",
                instrument_id=SPOT_ID,
                listing_id=listing.listing_id,
                provider_id="prepared_vendor",
                symbol="XBTUSD",
                validity=validity,
                source=source,
            ),
        ),
        relationships=relationships,
    )


def _data_store() -> AppendOnlyBitemporalStore:
    schema_hash = _hash("prepared-schema")

    def clocks(minute: int) -> ObservationClocks:
        return ObservationClocks(
            event_at="2026-01-02T14:00:00+00:00",
            knowledge_at=f"2026-01-02T14:{minute:02d}:00+00:00",
            revision_at=f"2026-01-02T14:{minute:02d}:00+00:00",
            received_at=f"2026-01-02T14:{minute + 1:02d}:00+00:00",
            ingested_at=f"2026-01-02T14:{minute + 2:02d}:00+00:00",
        )

    raw = BitemporalRecord(
        record_id="raw_btc_spot_20260102",
        version=1,
        layer=DataLayer.RAW,
        instrument_id=SPOT_ID,
        data_kind="spot_bar",
        clocks=clocks(30),
        payload={"close": "100", "currency": "USD", "eligible": True},
        lineage=DataLineage(
            source_id="prepared_dataset",
            source_version="snapshot_20260102",
            source_artifact_hash=_hash("raw-dataset"),
            source_schema_hash=schema_hash,
        ),
    )
    normalized = BitemporalRecord(
        record_id="normalized_btc_spot_20260102",
        version=1,
        layer=DataLayer.NORMALIZED,
        instrument_id=SPOT_ID,
        data_kind="spot_bar",
        clocks=clocks(35),
        payload={"close": "100", "currency": "USD", "eligible": True},
        lineage=DataLineage(
            source_id="prepared_dataset",
            source_version="snapshot_20260102",
            source_artifact_hash=_hash("normalized-dataset"),
            source_schema_hash=schema_hash,
            upstream_record_hashes=(raw.record_hash(),),
            transformation_id="normalize_spot_bar",
            transformation_version="v2",
            parameters_hash=_hash("normalize-policy"),
        ),
    )
    derived = BitemporalRecord(
        record_id="derived_btc_spot_signal_20260102",
        version=1,
        layer=DataLayer.DERIVED,
        instrument_id=SPOT_ID,
        data_kind="research_signal",
        clocks=clocks(40),
        payload={"expected_return": "0.08", "direction": "LONG"},
        lineage=DataLineage(
            source_id="prepared_dataset",
            source_version="snapshot_20260102",
            source_artifact_hash=_hash("derived-dataset"),
            source_schema_hash=schema_hash,
            upstream_record_hashes=(normalized.record_hash(),),
            transformation_id="derive_research_signal",
            transformation_version="v2",
            parameters_hash=_hash("signal-policy"),
        ),
    )
    return AppendOnlyBitemporalStore().append(raw).append(normalized).append(derived)


def _market_state_for(
    *,
    valuation: datetime,
    spot: Decimal,
    volatility: Decimal,
    state_id: str,
) -> MarketState:
    observed = valuation - timedelta(seconds=30)
    metadata = ObservationMetadata(
        observed_at=_iso(observed),
        knowledge_at=_iso(observed + timedelta(seconds=1)),
        source_hash=_hash(f"market-{state_id}"),
        calendar_id=CALENDAR_ID,
        max_age_seconds=60,
    )
    return MarketState(
        state_id=state_id,
        valuation_at=_iso(valuation),
        base_currency="USD",
        calendar_ids=(CALENDAR_ID,),
        spots=(
            SpotQuote(
                instrument_id=SPOT_ID,
                price=spot,
                currency="USD",
                unit="USD_per_coin",
                metadata=metadata,
            ),
        ),
        curves=(
            YieldCurve(
                curve_id="curve_usd_ois",
                currency="USD",
                curve_type="ois",
                points=(CurvePoint(30, Decimal("0.04")),),
                metadata=metadata,
            ),
        ),
        volatility_surfaces=(
            VolatilitySurface(
                surface_id="surface_btc_puts",
                underlying_instrument_id=SPOT_ID,
                quote_currency="USD",
                points=(
                    VolatilityPoint(_iso(OPTION_EXPIRY), Decimal("45"), volatility),
                ),
                metadata=metadata,
            ),
        ),
        rates=(
            RateQuote(
                rate_id="rate_usd_30d",
                currency="USD",
                tenor_days=30,
                rate=Decimal("0.04"),
                metadata=metadata,
            ),
        ),
        borrow_quotes=(
            BorrowQuote(
                instrument_id=SPOT_ID,
                currency="USD",
                annualized_rate=Decimal("0.005"),
                available_quantity=Decimal("10000"),
                quantity_unit="coin",
                metadata=metadata,
            ),
        ),
        liquidity_quotes=(
            LiquidityQuote(
                instrument_id=SPOT_ID,
                currency="USD",
                bid=spot - Decimal("0.1"),
                ask=spot + Decimal("0.1"),
                price_unit="USD_per_coin",
                depth_quantity=Decimal("10000"),
                quantity_unit="coin",
                metadata=metadata,
            ),
            LiquidityQuote(
                instrument_id=NEW_FUTURE_ID,
                currency="USD",
                bid=Decimal("99.5"),
                ask=Decimal("100.5"),
                price_unit="USD_per_coin",
                depth_quantity=Decimal("100"),
                quantity_unit="contract",
                metadata=metadata,
            ),
            LiquidityQuote(
                instrument_id=OPTION_ID,
                currency="USD",
                bid=Decimal("4.4"),
                ask=Decimal("4.6"),
                price_unit="USD_per_coin",
                depth_quantity=Decimal("100"),
                quantity_unit="contract",
                metadata=metadata,
            ),
        ),
    )


def _market_state(*, final: bool) -> MarketState:
    return _market_state_for(
        valuation=FINAL_MARK_AT if final else OPTION_DECISION,
        spot=Decimal("49") if final else Decimal("50"),
        volatility=Decimal("0.27") if final else Decimal("0.25"),
        state_id="state_final" if final else "state_option_decision",
    )


def _spot_expression_decision() -> tuple[EconomicHypothesis, ExpressionDecision]:
    distribution = ExpectedMarketDistribution(
        expected_return=Decimal("0.08"),
        annualized_volatility=Decimal("0.20"),
        downside_tail_return=Decimal("-0.20"),
        upside_return=Decimal("0.25"),
        horizon_days=60,
        risk_free_rate=Decimal("0.04"),
        dividend_yield=Decimal("0.01"),
        volatility_change=Decimal("0"),
        liquidity_change=Decimal("0"),
        scenarios=(
            ScenarioRange("bear", Decimal("0.25"), Decimal("-0.20"), Decimal("-0.05")),
            ScenarioRange("base", Decimal("0.50"), Decimal("-0.05"), Decimal("0.12")),
            ScenarioRange("bull", Decimal("0.25"), Decimal("0.12"), Decimal("0.30")),
        ),
    )
    hypothesis = EconomicHypothesis(
        hypothesis_id="hypothesis_btc_upside",
        version="2",
        economic_underlying_id=UNDERLYING_ID,
        rationale="Prepared immutable evidence supports a positive research horizon.",
        expected_direction=Direction.LONG,
        distribution=distribution,
        conditions=("prepared signal remains positive",),
        failure_conditions=("prepared signal becomes non-positive",),
        prediction_target="60-day total return",
        evaluation_metrics=("net return",),
    )
    payoff = DesiredEconomicPayoff(
        underlying_id=UNDERLYING_ID,
        direction=Direction.LONG,
        horizon_days=60,
        target_notional=Decimal("100000"),
        target_delta=Decimal("100000"),
        target_vega=None,
        target_volatility=None,
        maximum_loss=Decimal("100000"),
        maximum_premium=None,
        tail_protection_required=False,
        bounded_loss_required=False,
        allowed_expression_kinds=(ExpressionKind.SPOT,),
    )
    choice = InstrumentChoice(
        instrument_id=SPOT_ID,
        economic_underlying_id=UNDERLYING_ID,
        product_kind=ProductKind.SPOT,
        currency="USD",
        known_at=DECISION - timedelta(minutes=1),
        unit_price=Decimal("100"),
        contract_multiplier=Decimal("1"),
        economic_notional_per_unit=Decimal("100"),
        liquidity_score=Decimal("0.90"),
        expected_return=Decimal("0.08"),
        expected_carry=Decimal("0.01"),
        expected_roll_cost=Decimal("0"),
        expected_time_value_decay=Decimal("0"),
        implied_volatility=None,
        transaction_cost=Decimal("10"),
        initial_margin=Decimal("0"),
        tail_loss=Decimal("0.20"),
        model_sensitivity=Decimal("0.05"),
        data_confidence=Decimal("0.95"),
    )
    candidate = ExpressionCandidate(
        candidate_id="candidate_spot_btc",
        expression_kind=ExpressionKind.SPOT,
        choices=(choice,),
        directions=(Direction.LONG,),
        roles=(LegRole.PRIMARY,),
        leg_ratios=(Decimal("1"),),
        selection_rules=(
            LegSelectionRule(
                product_kind=ProductKind.SPOT,
                minimum_liquidity_score=Decimal("0.50"),
                sizing_method="TARGET_NOTIONAL",
            ),
        ),
        execution_mode=ExecutionMode.COMPLEX_CONSERVATIVE,
        expected_return=Decimal("0.08"),
        pnl_dispersion=Decimal("0.20"),
        maximum_loss=Decimal("100000"),
        carry=Decimal("0.01"),
        roll_cost=Decimal("0"),
        time_value_decay=Decimal("0"),
        implied_volatility_cost=Decimal("0"),
        liquidity_score=Decimal("0.90"),
        transaction_cost=Decimal("10"),
        margin_required=Decimal("0"),
        tail_risk=Decimal("0.20"),
        model_sensitivity=Decimal("0.05"),
        data_confidence=Decimal("0.95"),
        targets=StrategyTargets(
            net_delta=Decimal("100000"),
            target_notional=Decimal("100000"),
            maximum_loss=Decimal("100000"),
        ),
    )
    decision = InstrumentExpressionEngine(DEFAULT_EXPRESSION_POLICY).select(
        hypothesis=hypothesis,
        payoff=payoff,
        candidates=(candidate,),
        as_of=DECISION,
    )
    return hypothesis, decision


def _futures_contract(contract_id: str, expiration: str) -> FuturesContract:
    return FuturesContract(
        contract_id=contract_id,
        root_id="btc_future_root",
        listing_date="2026-01-01",
        first_trade_date="2026-01-02",
        last_trade_date=expiration,
        first_notice_date=None,
        final_settlement_date=expiration,
        expiration_date=expiration,
        contract_multiplier=Decimal("25"),
        tick_size=Decimal("0.25"),
        settlement_type=FuturesSettlementType.CASH_SETTLED,
        spec_effective_at="2026-01-01T00:00:00Z",
        spec_version="v2",
        availability=_availability("2026-01-02T00:00:00Z"),
    )


def _futures_quote(
    contract: FuturesContract, price: str, sequence: int
) -> ContractQuote:
    value = Decimal(price)
    return ContractQuote(
        quote_id=f"quote_{contract.contract_id}_{sequence}",
        contract_id=contract.contract_id,
        root_id=contract.root_id,
        observed_at=FUTURES_AS_OF,
        trading_date="2026-03-10",
        session=SessionType.COMBINED,
        session_sequence=sequence,
        open_price=value,
        high_price=value + Decimal("1"),
        low_price=value - Decimal("1"),
        close_price=value,
        settlement_price=value,
        volume=Decimal("1000"),
        open_interest=Decimal("5000"),
        availability=_availability(FUTURES_AS_OF),
        source_hash=_hash(f"quote-source-{contract.contract_id}"),
        bid_price=value - Decimal("0.25"),
        ask_price=value + Decimal("0.25"),
    )


def _typed_futures_curve(
    *,
    contracts: tuple[FuturesContract, ...],
    quotes: tuple[ContractQuote, ...],
    margin_model_hash: str,
    curve_source_hash: str,
) -> FuturesCurveState:
    curve_metadata = ObservationMetadata(
        observed_at=FUTURES_AS_OF,
        knowledge_at=FUTURES_AS_OF,
        source_hash=curve_source_hash,
        calendar_id=CALENDAR_ID,
        max_age_seconds=7 * 24 * 60 * 60,
    )
    typed_quotes: list[FuturesContractQuote] = []
    for contract, quote in zip(contracts, quotes, strict=True):
        if quote.bid_price is None or quote.ask_price is None:
            raise AssertionError("prepared futures quote must be two-sided")
        typed_quotes.append(
            FuturesContractQuote(
                contract_id=contract.contract_id,
                underlying_instrument_id=SPOT_ID,
                expiry_at=f"{contract.expiration_date}T00:00:00+00:00",
                currency="USD",
                price_unit="USD_per_coin",
                bid=quote.bid_price,
                ask=quote.ask_price,
                last=quote.close_price,
                settlement=quote.settlement_price,
                bid_size=Decimal("100"),
                ask_size=Decimal("100"),
                volume=quote.volume,
                open_interest=quote.open_interest,
                condition=QuoteCondition.OFFICIAL_SETTLEMENT,
                initial_margin_per_contract=Decimal("5000"),
                collateral_per_contract=Decimal("2500"),
                margin_model_hash=margin_model_hash,
                metadata=ObservationMetadata(
                    observed_at=quote.observed_at,
                    knowledge_at=quote.observed_at,
                    source_hash=quote.content_hash,
                    calendar_id=CALENDAR_ID,
                    max_age_seconds=7 * 24 * 60 * 60,
                ),
            )
        )
    return FuturesCurveState(
        curve_id="market_state_btc_futures_curve",
        underlying_instrument_id=SPOT_ID,
        currency="USD",
        price_unit="USD_per_coin",
        contracts=tuple(typed_quotes),
        metadata=curve_metadata,
    )


def _position_payload(position: PositionView) -> dict[str, object]:
    return {
        "instrument_id": getattr(position, "instrument_id"),
        "asset_class": getattr(getattr(position, "asset_class"), "value"),
        "currency": getattr(position, "currency"),
        "quantity": str(getattr(position, "quantity")),
        "average_price": str(getattr(position, "average_price")),
        "mark_price": str(getattr(position, "mark_price")),
        "multiplier": str(getattr(position, "multiplier")),
    }


def _valuation_payload(valuation: PortfolioValuation) -> dict[str, str]:
    return {
        name: str(getattr(valuation, name))
        for name in (
            "nav",
            "external_cash_flow",
            "economic_pnl",
            "realized_pnl",
            "unrealized_pnl",
            "income",
            "costs",
            "attributed_pnl",
        )
    }


def _accounting(
    snapshot: PortfolioSnapshot,
) -> tuple[ScenarioAccounting, PortfolioValuation]:
    valuation = snapshot.valuation(fx_rates={"USD": Decimal("1")})
    return (
        ScenarioAccounting(
            opening_nav=Decimal("0"),
            external_cash_flow=valuation.external_cash_flow,
            closing_nav=valuation.nav,
            ledger_pnl=valuation.economic_pnl,
            report_pnl=valuation.attributed_pnl,
        ),
        valuation,
    )


def _report_ledger_reconciliation(
    ledger: UnifiedPortfolioLedger,
) -> ReportLedgerReconciliation:
    """Build the report receipt from independent ledger/accounting projections."""

    snapshot = ledger.replay()
    if snapshot.as_of is None:
        raise AssertionError("integrated ledger must have an accounting close time")
    opened_at = _iso(DECISION - timedelta(minutes=10))
    opening_ledger = UnifiedPortfolioLedger.open(
        ledger_id=ledger.ledger_id,
        base_currency=ledger.base_currency,
    )
    ledger_receipt = LedgerPnlReconciliation.from_ledger_projection(
        reconciliation_id="ledger:required-scenarios",
        opening_ledger=opening_ledger,
        closing_ledger=ledger,
        opened_at=opened_at,
        closed_at=snapshot.as_of,
        fx_observations=(),
    )
    report_payload = encode_report_payload(
        report_id="report:required-scenarios",
        ledger=ledger_receipt,
    )
    report = ReportPnlSummary.from_json(
        report_payload,
        expected_payload_hash=report_payload_hash(report_payload),
    )
    return ReportLedgerReconciliation(
        reconciliation_id="report-ledger:required-scenarios",
        ledger=ledger_receipt,
        report=report,
    )


def _objects(
    *,
    trades: tuple[object, ...],
    positions: tuple[PositionView, ...],
    events: tuple[PortfolioEvent, ...],
    valuation: PortfolioValuation,
    exposure: object,
    attribution: object,
    scenario_output: object,
) -> ScenarioObjectHashes:
    return scenario_object_hashes(
        trades=trades,
        positions=tuple(_position_payload(item) for item in positions),
        ledger_events=tuple(item.as_dict() for item in events),
        nav=(_valuation_payload(valuation),),
        exposure=exposure,
        attribution=attribution,
        scenario_output=scenario_output,
    )


@dataclass(frozen=True, slots=True)
class _Run:
    registry: InstrumentRegistry
    store: AppendOnlyBitemporalStore
    pit_records: tuple[BitemporalRecord, ...]
    decision_state: MarketState
    final_state: MarketState
    hypothesis: EconomicHypothesis
    expression_decision: ExpressionDecision
    futures_reference: FuturesReferenceSnapshot
    futures_curve: FuturesCurveSnapshot
    option_chain: CleanedOptionChain
    option_selection: OptionSelectionDecision
    spot_trace: SpotScenarioTrace
    futures_trace: FuturesScenarioTrace
    option_trace: OptionScenarioTrace
    integrated_trace: IntegratedScenarioTrace
    accounting_reconciliation: ReportLedgerReconciliation
    core_objects: ScenarioObjectHashes
    core_artifact_hash: str
    integrated_ledger: UnifiedPortfolioLedger
    terminal_ledger: UnifiedPortfolioLedger
    exposure: PortfolioExposureSnapshot
    joint_scenario: JointScenarioResult


def _execute_required_scenarios() -> _Run:
    registry = _registry()
    assert (
        registry.resolve_symbol(
            provider_id="prepared_vendor",
            symbol="XBTUSD",
            as_of=_iso(DECISION),
        ).instrument_id
        == SPOT_ID
    )
    store = _data_store()
    pit_records = store.query_as_of(
        event_as_of=_iso(DECISION),
        knowledge_as_of=_iso(DECISION),
    )
    hypothesis, expression_decision = _spot_expression_decision()
    assert expression_decision.selected_candidate_id == "candidate_spot_btc"
    selected_leg = expression_decision.selected_legs[0]
    assert selected_leg.instrument_id == SPOT_ID
    assert selected_leg.quantity == Decimal("1000")

    membership = UniverseMembership(
        universe_id="universe_btc_research",
        instrument_id=SPOT_ID,
        effective_from=DECISION - timedelta(days=1),
        effective_to=None,
        announcement_at=DECISION - timedelta(days=2),
        implementation_at=DECISION - timedelta(days=1),
        known_at=DECISION - timedelta(minutes=2),
        membership_source_hash=_hash("universe-membership"),
    )
    universe = PointInTimeSpotUniverse((membership,))
    members = universe.members(
        membership.universe_id,
        effective_at=DECISION,
        knowledge_at=DECISION,
    )
    assert members == (SPOT_ID,)
    universe_hash = evidence_hash(
        {
            "universe_id": membership.universe_id,
            "members": members,
            "effective_at": _iso(DECISION),
            "maximum_knowledge_at": _iso(membership.known_at),
            "membership_source_hash": membership.membership_source_hash,
        },
        label="pit-universe-snapshot",
    )

    execution = ExecutionContext(
        execution_id="execution_spot_entry",
        instrument_id=SPOT_ID,
        instrument_kind="SPOT",
        currency="USD",
        side=ExecutionSide.BUY,
        requested_quantity=selected_leg.quantity,
        filled_quantity=selected_leg.quantity,
        reference_price=Decimal("100"),
        execution_price=Decimal("100"),
        observed_at=_iso(DECISION),
        capacity_quantity=Decimal("10000"),
        participation_rate=Decimal("0.10"),
        source_hashes=tuple(
            sorted((expression_decision.content_hash, store.content_hash()))
        ),
    )
    spot_cost = LinearExecutionCostModel(commission_per_unit=Decimal("0.01")).estimate(
        execution
    )
    spot_ledger = UnifiedPortfolioLedger.open(
        ledger_id="ledger_required_multi_asset",
        base_currency="USD",
    ).publish(
        funding_event(
            event_id="funding_spot",
            occurred_at=_iso(DECISION - timedelta(minutes=10)),
            cash_deltas=(CashDelta("USD", Decimal("200000")),),
        )
    )
    spot_ledger = spot_ledger.publish(
        trade_event(
            event_id="spot_entry",
            occurred_at=_iso(DECISION),
            asset_class=AssetClass.SPOT,
            instrument_id=SPOT_ID,
            currency="USD",
            quantity_delta=selected_leg.quantity,
            price=Decimal("100"),
            source_hashes=(expression_decision.content_hash,),
            execution_context_hash=execution.content_hash,
        )
    ).publish_many(
        cost_events_from_breakdown(
            spot_cost,
            event_id_prefix="spot_entry_cost",
            occurred_at=_iso(DECISION),
            instrument_id=SPOT_ID,
            asset_class=AssetClass.SPOT,
            source_hashes=(expression_decision.content_hash,),
        )
    )
    entry_book = SpotBook(
        positions=(
            SpotPosition(
                instrument_id=SPOT_ID,
                quantity=selected_leg.quantity,
                total_cost_basis=Decimal("100000"),
                currency="USD",
            ),
        ),
        cash=(SpotCashBalance("USD", Decimal("100000")),),
    )
    split = CorporateAction(
        action_id="action_btc_split",
        revision=1,
        action_type=CorporateActionType.SPLIT,
        instrument_id=SPOT_ID,
        announced_at=DECISION - timedelta(days=1),
        known_at=DECISION - timedelta(hours=12),
        record_at=None,
        ex_at=None,
        payment_at=None,
        effective_at=DECISION + timedelta(days=1),
        source_id="prepared_corporate_actions",
        source_record_hash=_hash("split-source"),
        ratio=Decimal("2"),
    )
    split_application = apply_corporate_action(
        entry_book,
        split,
        applied_at=split.effective_at,
    )
    split_events = adapt_corporate_action_application(
        split_application,
        mark_prices_after={SPOT_ID: Decimal("50")},
    )
    spot_ledger = spot_ledger.publish_many(split_events)
    dividend = CorporateAction(
        action_id="action_btc_dividend",
        revision=1,
        action_type=CorporateActionType.CASH_DIVIDEND,
        instrument_id=SPOT_ID,
        announced_at=DECISION - timedelta(days=1),
        known_at=DECISION - timedelta(hours=12),
        record_at=DECISION + timedelta(days=1),
        ex_at=DECISION + timedelta(days=1),
        payment_at=DECISION + timedelta(days=2),
        effective_at=DECISION + timedelta(days=2),
        source_id="prepared_corporate_actions",
        source_record_hash=_hash("dividend-source"),
        currency="USD",
        cash_per_share=Decimal("1"),
        tax_rate=Decimal("0.10"),
    )
    dividend_application = apply_corporate_action(
        split_application.book_after,
        dividend,
        applied_at=dividend.effective_at,
        entitlement_book=split_application.book_after,
    )
    dividend_events = adapt_corporate_action_application(dividend_application)
    spot_ledger = spot_ledger.publish_many(dividend_events)
    spot_snapshot = spot_ledger.replay()
    spot_accounting, spot_valuation = _accounting(spot_snapshot)
    before_value = split_application.book_before.value(
        prices={SPOT_ID: Decimal("100")},
        fx_to_base={"USD": Decimal("1")},
    )
    after_value = split_application.book_after.value(
        prices={SPOT_ID: Decimal("50")},
        fx_to_base={"USD": Decimal("1")},
    )
    dividend_cashflow = dividend_application.book_after.cash_amount(
        "USD"
    ) - dividend_application.book_before.cash_amount("USD")
    ledger_dividend_cashflow = sum(
        (delta.amount for event in dividend_events for delta in event.cash_deltas),
        Decimal("0"),
    )

    decision_state = _market_state(final=False)
    final_state = _market_state(final=True)
    spot_position = spot_snapshot.spot_positions[0]
    spot_exposure_position = ExposurePosition(
        position_id="exposure_spot_position",
        instrument_id=spot_position.instrument_id,
        quantity=spot_position.quantity,
        quantity_unit="coin",
        multiplier=spot_position.multiplier,
        currency=spot_position.currency,
        source_hash=next(
            item.content_hash
            for item in spot_ledger.events
            if item.event_id == "spot_entry"
        ),
        opened_at=_iso(DECISION),
    )
    spot_exposure = ExposureEngine.with_default_spot(product_catalog=registry).evaluate(
        snapshot_id="exposure_spot_required",
        positions=(spot_exposure_position,),
        market_state=final_state,
    )
    spot_objects = _objects(
        trades=(execution.identity_payload(),),
        positions=spot_snapshot.positions,
        events=spot_ledger.events,
        valuation=spot_valuation,
        exposure=spot_exposure.as_dict(),
        attribution={
            "gross_performance": str(
                spot_valuation.economic_pnl + spot_valuation.costs
            ),
            "net_performance": str(spot_valuation.economic_pnl),
            "cost_hash": spot_cost.content_hash,
        },
        scenario_output={
            "split_application_hash": split_application.book_after_hash,
            "dividend_application_hash": dividend_application.book_after_hash,
        },
    )
    spot_artifact_hash = evidence_hash(
        {
            "ledger_hash": spot_ledger.content_hash,
            "exposure_hash": spot_exposure.content_hash,
            "decision_hash": expression_decision.content_hash,
        },
        label="spot-required-artifact",
    )
    spot_trace = SpotScenarioTrace(
        decision_at=_iso(DECISION),
        maximum_universe_knowledge_at=_iso(membership.known_at),
        universe_snapshot_hash=universe_hash,
        signal_hash=expression_decision.content_hash,
        selected_instrument_ids=members,
        trade_hashes=(execution.content_hash,),
        position_hash=spot_exposure_position.position_hash(),
        ledger_hash=spot_ledger.content_hash,
        nav_hash=spot_objects.nav_hash,
        exposure_hash=spot_exposure.content_hash,
        artifact_hash=spot_artifact_hash,
        corporate_action_value_before=before_value,
        corporate_action_value_after=after_value,
        portfolio_cashflow=dividend_cashflow,
        ledger_cashflow=ledger_dividend_cashflow,
        gross_performance=spot_valuation.economic_pnl + spot_valuation.costs,
        net_performance=spot_valuation.economic_pnl,
        data_version_hashes=(store.content_hash(),),
        code_hash=_hash("required-scenario-code-v2"),
        accounting=spot_accounting,
        object_hashes=spot_objects,
    )

    old_contract = _futures_contract(OLD_FUTURE_ID, "2026-04-09")
    new_contract = _futures_contract(NEW_FUTURE_ID, "2026-05-09")
    far_contract = _futures_contract(FAR_FUTURE_ID, "2026-06-08")
    old_quote = _futures_quote(old_contract, "102", 0)
    new_quote = _futures_quote(new_contract, "100", 1)
    far_quote = _futures_quote(far_contract, "103", 2)
    chain = ContractChainSnapshot(
        snapshot_id="chain_btc_20260310",
        root_id=old_contract.root_id,
        observed_at=FUTURES_AS_OF,
        availability=_availability(FUTURES_AS_OF),
        contracts=(old_contract, new_contract, far_contract),
        quotes=(old_quote, new_quote, far_quote),
        lifecycle_events=(),
        quality_results=(),
        source_manifest_hashes=tuple(
            sorted(
                {old_quote.source_hash, new_quote.source_hash, far_quote.source_hash}
            )
        ),
    )
    futures_curve = build_futures_curve_snapshot(
        chain,
        snapshot_id="curve_btc_20260310",
        feature_version="v2",
        as_of=FUTURES_AS_OF,
        spot_price=Decimal("50"),
        spot_availability=_availability(FUTURES_AS_OF),
        spot_source_hash=decision_state.state_hash(),
    )
    first_continuous = ContinuousFuturesPoint(
        point_id="continuous_btc_20260309",
        series_id="continuous_btc_future",
        root_id=old_contract.root_id,
        observed_at="2026-03-09T16:00:00Z",
        source_contract_id=OLD_FUTURE_ID,
        source_quote_hash=_hash("entry-future-quote"),
        source_price=Decimal("100"),
        continuous_price=Decimal("100"),
        additive_adjustment=Decimal("0"),
        multiplicative_adjustment=Decimal("1"),
        roll_gap=Decimal("0"),
        policy_hash=_hash("continuous-policy"),
        roll_decision_hash=_hash("continuous-decision-1"),
        chain_snapshot_hash=chain.content_hash,
        previous_point_hash=None,
    )
    second_continuous = ContinuousFuturesPoint(
        point_id="continuous_btc_20260310",
        series_id="continuous_btc_future",
        root_id=old_contract.root_id,
        observed_at=FUTURES_AS_OF,
        source_contract_id=NEW_FUTURE_ID,
        source_quote_hash=new_quote.content_hash,
        source_price=Decimal("100"),
        continuous_price=Decimal("100"),
        additive_adjustment=Decimal("0"),
        multiplicative_adjustment=Decimal("1"),
        roll_gap=Decimal("0"),
        policy_hash=_hash("continuous-policy"),
        roll_decision_hash=_hash("continuous-decision-2"),
        chain_snapshot_hash=chain.content_hash,
        previous_point_hash=first_continuous.content_hash,
    )
    continuous_trace = trace_continuous_signal(
        (first_continuous, second_continuous),
        trace_id="trace_btc_continuous",
    )
    roll_policy = RollPlanningPolicy(
        policy_id="roll_btc_fixed_60",
        policy_version="v2",
        fixed_maturity_days=60,
        fixed_maturity_tolerance_days=0,
        minimum_days_to_notice=5,
        minimum_days_to_expiration=5,
        minimum_days_to_last_trade=1,
    )
    assert (
        select_roll_target(
            old_contract,
            (far_contract, new_contract),
            as_of=FUTURES_AS_OF,
            policy=roll_policy,
        )
        is new_contract
    )
    margin_policy = MarginSimulationPolicy(
        policy_id="margin_btc_research",
        policy_version="v2",
        initial_margin_per_contract=Decimal("5000"),
        maintenance_margin_per_contract=Decimal("4000"),
        collateral_fraction=Decimal("1"),
        margin_call_action=MarginCallAction.BLOCK_NEW_TRADES,
    )
    specification_version = adapt_existing_futures_contract(
        new_contract,
        quote_currency="USD",
    )
    margin_version = adapt_existing_margin_policy(
        margin_policy,
        contract_id=NEW_FUTURE_ID,
        currency="USD",
        effective_from="2026-01-01T00:00:00Z",
        effective_to=None,
        knowledge_at="2026-01-02T00:00:00Z",
    )
    reference_history = FuturesReferenceHistory(
        history_id="history_btc_future_may26",
        contract_id=NEW_FUTURE_ID,
        specifications=(specification_version,),
        margins=(margin_version,),
    )
    futures_reference = reference_history.as_of(
        valid_at=FUTURES_AS_OF,
        known_at=FUTURES_AS_OF,
    )
    market_state_futures_curve = _typed_futures_curve(
        contracts=(old_contract, new_contract, far_contract),
        quotes=(old_quote, new_quote, far_quote),
        margin_model_hash=margin_version.content_hash,
        curve_source_hash=futures_curve.content_hash,
    )
    futures_cost_model = ExistingFuturesCostPolicyAdapter(
        FuturesCostPolicy(
            policy_id="cost_btc_roll",
            policy_version="v2",
            commission_per_contract=Decimal("2"),
            execution_slippage_ticks=Decimal("1"),
            roll_slippage_ticks=Decimal("1"),
            spread_legging_ticks=Decimal("0"),
        )
    )
    roll_plan = plan_exposure_preserving_roll(
        plan_id="plan_btc_roll",
        as_of=FUTURES_AS_OF,
        old_contract=old_contract,
        new_contract=new_contract,
        old_quote=old_quote,
        new_quote=new_quote,
        current_old_quantity=2,
        target_exposure=Decimal("5000"),
        policy=roll_policy,
        cost_model=futures_cost_model,
    )
    close_leg, open_leg = roll_plan.legs
    entry_fill = FuturesFill(
        fill_id="fill_btc_future_entry",
        intent_hash=first_continuous.content_hash,
        contract_id=OLD_FUTURE_ID,
        quote_hash=first_continuous.source_quote_hash,
        filled_at="2026-03-09T16:00:00Z",
        trading_date="2026-03-09",
        session=SessionType.COMBINED,
        side=OrderSide.BUY,
        quantity=2,
        reference_price=Decimal("100"),
        fill_price=Decimal("100"),
        multiplier=Decimal("25"),
        commission=Decimal("4"),
        slippage_cost=Decimal("0"),
        realized_trade_pnl=Decimal("0"),
        is_roll_leg=False,
    )
    settlement = SettlementEvent(
        event_id="settlement_btc_old",
        contract_id=OLD_FUTURE_ID,
        quote_hash=old_quote.content_hash,
        settled_at=FUTURES_AS_OF,
        previous_settlement_price=Decimal("100"),
        settlement_price=Decimal("102"),
        quantity=2,
        multiplier=Decimal("25"),
        variation_margin=Decimal("100"),
    )
    close_fill = FuturesFill(
        fill_id="fill_btc_roll_close",
        intent_hash=roll_plan.content_hash,
        contract_id=OLD_FUTURE_ID,
        quote_hash=old_quote.content_hash,
        filled_at=FUTURES_AS_OF,
        trading_date="2026-03-10",
        session=SessionType.COMBINED,
        side=close_leg.side,
        quantity=close_leg.quantity,
        reference_price=close_leg.reference_price,
        fill_price=close_leg.cost.expected_fill_price,
        multiplier=old_contract.contract_multiplier,
        commission=close_leg.cost.commission,
        slippage_cost=close_leg.cost.slippage_cost,
        realized_trade_pnl=Decimal("-25"),
        is_roll_leg=True,
    )
    open_fill = FuturesFill(
        fill_id="fill_btc_roll_open",
        intent_hash=roll_plan.content_hash,
        contract_id=NEW_FUTURE_ID,
        quote_hash=new_quote.content_hash,
        filled_at=FUTURES_AS_OF,
        trading_date="2026-03-10",
        session=SessionType.COMBINED,
        side=open_leg.side,
        quantity=open_leg.quantity,
        reference_price=open_leg.reference_price,
        fill_price=open_leg.cost.expected_fill_price,
        multiplier=new_contract.contract_multiplier,
        commission=open_leg.cost.commission,
        slippage_cost=open_leg.cost.slippage_cost,
        realized_trade_pnl=Decimal("0"),
        is_roll_leg=True,
    )
    roll_execution = RollExecution(
        execution_id="execution_btc_roll",
        decision_hash=roll_plan.content_hash,
        executed_at=FUTURES_AS_OF,
        from_contract_id=OLD_FUTURE_ID,
        to_contract_id=NEW_FUTURE_ID,
        close_fill_hash=close_fill.content_hash,
        open_fill_hash=open_fill.content_hash,
        close_cost=close_fill.total_cost,
        open_cost=open_fill.total_cost,
        price_gap=new_quote.close_price - old_quote.close_price,
        roll_yield=Decimal("0"),
    )
    futures_pnl = reconcile_existing_futures_pnl(
        evidence_id="reconcile_btc_futures",
        observed_at=FUTURES_AS_OF,
        opening_cash=Decimal("5001"),
        closing_cash=Decimal("5018"),
        settlement_events=(settlement,),
        roll_execution=roll_execution,
        roll_fills=(close_fill, open_fill),
        roll_plan=roll_plan,
    )
    futures_pnl.require_reconciled()
    entry_futures_drafts = adapt_futures_fill(
        entry_fill,  # type: ignore[arg-type]
        currency="USD",
    )
    settlement_draft = adapt_futures_settlement(
        settlement,  # type: ignore[arg-type]
        currency="USD",
    )
    close_drafts = adapt_futures_fill(
        close_fill,  # type: ignore[arg-type]
        currency="USD",
    )
    open_drafts = adapt_futures_fill(
        open_fill,  # type: ignore[arg-type]
        currency="USD",
    )
    futures_operating_drafts = (
        PortfolioEventDraft(
            event_id="collateral_post_btc",
            event_type=PortfolioEventType.COLLATERAL_TRANSFER,
            occurred_at="2026-03-09T15:00:00Z",
            currency="USD",
            cash_deltas=(CashDelta("USD", Decimal("-5000")),),
            collateral_delta=Decimal("5000"),
            source_hashes=(margin_version.content_hash,),
        ),
        *entry_futures_drafts,
        PortfolioEventDraft(
            event_id="margin_old_btc",
            event_type=PortfolioEventType.MARGIN_REQUIREMENT,
            occurred_at="2026-03-09T16:01:00Z",
            currency="USD",
            instrument_id=OLD_FUTURE_ID,
            asset_class=AssetClass.FUTURE,
            margin_requirement=Decimal("10000"),
            source_hashes=(margin_version.content_hash,),
        ),
        collateral_income_event(
            event_id="collateral_income_btc",
            occurred_at="2026-03-09T16:02:00Z",
            currency="USD",
            amount=Decimal("5"),
            source_hashes=(margin_version.content_hash,),
        ),
        settlement_draft,
        *close_drafts,
        *open_drafts,
        PortfolioEventDraft(
            event_id="margin_old_btc_zero",
            event_type=PortfolioEventType.MARGIN_REQUIREMENT,
            occurred_at=FUTURES_AS_OF,
            currency="USD",
            instrument_id=OLD_FUTURE_ID,
            asset_class=AssetClass.FUTURE,
            margin_requirement=Decimal("0"),
            source_hashes=(roll_plan.content_hash,),
        ),
        PortfolioEventDraft(
            event_id="margin_new_btc",
            event_type=PortfolioEventType.MARGIN_REQUIREMENT,
            occurred_at=FUTURES_AS_OF,
            currency="USD",
            instrument_id=NEW_FUTURE_ID,
            asset_class=AssetClass.FUTURE,
            margin_requirement=Decimal("10000"),
            source_hashes=(roll_plan.content_hash,),
        ),
    )
    futures_ledger = (
        UnifiedPortfolioLedger.open(
            ledger_id="ledger_futures_required",
            base_currency="USD",
        )
        .publish(
            funding_event(
                event_id="funding_futures",
                occurred_at="2026-03-09T14:00:00Z",
                cash_deltas=(CashDelta("USD", Decimal("10000")),),
            )
        )
        .publish_many(futures_operating_drafts)
    )
    futures_snapshot = futures_ledger.replay()
    futures_accounting, futures_valuation = _accounting(futures_snapshot)
    futures_objects = _objects(
        trades=(entry_fill.as_dict(), close_fill.as_dict(), open_fill.as_dict()),
        positions=futures_snapshot.positions,
        events=futures_ledger.events,
        valuation=futures_valuation,
        exposure=roll_plan.as_dict(),
        attribution=futures_pnl.as_dict(),
        scenario_output=futures_curve.as_dict(),
    )
    roll_ledger_hashes = tuple(
        item.content_hash
        for item in futures_ledger.events
        if item.event_id
        in {f"{close_fill.fill_id}:trade", f"{open_fill.fill_id}:trade"}
    )
    futures_trace = FuturesScenarioTrace(
        continuous_series_id=continuous_trace.series_id,
        source_mappings=tuple(
            FuturesSourceMapping(
                trading_date=item.observed_at[:10],
                continuous_point_hash=item.point_hash,
                source_contract_id=item.source_contract_id,
            )
            for item in continuous_trace.mappings
        ),
        executed_contract_ids=(OLD_FUTURE_ID, NEW_FUTURE_ID),
        entry_fill_hashes=(entry_fill.content_hash,),
        settlement_hashes=(settlement.content_hash,),
        roll_close_fill_hash=close_fill.content_hash,
        roll_open_fill_hash=open_fill.content_hash,
        roll_ledger_event_hashes=roll_ledger_hashes,
        last_notice_at="2026-04-09T00:00:00+00:00",
        last_trade_at="2026-04-09T00:00:00+00:00",
        final_action_at="2026-03-10T16:00:00+00:00",
        settlement_pnl=futures_pnl.settlement_pnl,
        ledger_pnl=settlement.variation_margin,
        accounting=futures_accounting,
        object_hashes=futures_objects,
    )

    def option_contract(contract_id: str, strike: str) -> OptionContract:
        return OptionContract(
            contract_id=contract_id,
            underlying_id=SPOT_ID,
            option_type=OptionType.PUT,
            strike=Decimal(strike),
            expiration_at=_iso(OPTION_EXPIRY),
            exercise_style=ExerciseStyle.EUROPEAN,
            settlement_type=OptionSettlementType.CASH,
            multiplier=Decimal("100"),
            currency="USD",
            exchange="XOFF",
            listing_at="2026-01-01T00:00:00+00:00",
            last_trade_at=_iso(OPTION_EXPIRY),
            settlement_at=_iso(OPTION_EXPIRY + timedelta(hours=1)),
            price_tick=Decimal("0.01"),
        )

    selected_contract = option_contract(OPTION_ID, "45")
    alternate_contract = option_contract(ALT_OPTION_ID, "50")
    option_quote_availability = _availability(
        _iso(OPTION_DECISION - timedelta(seconds=5))
    )
    selected_quote = OptionQuote(
        quote_id="quote_btc_put45",
        contract_id=OPTION_ID,
        availability=option_quote_availability,
        as_of=_iso(OPTION_DECISION - timedelta(seconds=1)),
        bid=Decimal("4.8"),
        ask=Decimal("5.2"),
        last=Decimal("5"),
        bid_size=Decimal("20"),
        ask_size=Decimal("20"),
        volume=100,
        open_interest=1000,
    )
    alternate_quote = OptionQuote(
        quote_id="quote_btc_put50",
        contract_id=ALT_OPTION_ID,
        availability=option_quote_availability,
        as_of=_iso(OPTION_DECISION - timedelta(seconds=1)),
        bid=Decimal("6.8"),
        ask=Decimal("7.2"),
        last=Decimal("7"),
        bid_size=Decimal("20"),
        ask_size=Decimal("20"),
        volume=100,
        open_interest=1000,
    )
    derivative_option_chain = OptionChainSnapshot(
        chain_snapshot_id="chain_btc_puts_actual",
        underlying_id=SPOT_ID,
        knowledge_time=_iso(OPTION_DECISION),
        underlying_price=Decimal("50"),
        contracts=(selected_contract, alternate_contract),
        quotes=(selected_quote, alternate_quote),
        source_manifest_hashes=(_hash("option-chain-manifest"),),
    )
    forward = ForwardEstimate(
        value=Decimal("50.2"),
        method=ForwardMethod.BORROW_ADJUSTED_CARRY,
        estimated_at=OPTION_DECISION,
        input_hashes=(
            decision_state.state_hash(),
            store.content_hash(),
            derivative_option_chain.content_hash,
        ),
        rate=Decimal("0.04"),
        dividend_yield=Decimal("0.01"),
        borrow_rate=Decimal("0.005"),
    )

    def raw_observation(
        contract: OptionContract,
        quote: OptionQuote,
        delta: str,
    ) -> RawOptionObservation:
        return RawOptionObservation(
            contract_id=contract.contract_id,
            underlying_id=SPOT_ID,
            right=OptionRight.PUT,
            strike=contract.strike,
            expiry=OPTION_EXPIRY,
            observed_at=OPTION_DECISION - timedelta(seconds=5),
            known_at=OPTION_DECISION - timedelta(seconds=1),
            bid=quote.bid,
            ask=quote.ask,
            bid_size=quote.bid_size,
            ask_size=quote.ask_size,
            volume=quote.volume,
            open_interest=quote.open_interest,
            bid_iv=Decimal("0.24"),
            ask_iv=Decimal("0.26"),
            delta=Decimal(delta),
            source_quote_hash=quote.content_hash,
        )

    cleaned_chain = OptionChainCleaner(DEFAULT_OPTION_CLEANING_POLICY).clean(
        underlying_id=SPOT_ID,
        decision_at=OPTION_DECISION,
        spot=Decimal("50"),
        forward=forward,
        observations=(
            raw_observation(selected_contract, selected_quote, "-0.28"),
            raw_observation(alternate_contract, alternate_quote, "-0.45"),
        ),
    )
    option_selection_policy = OptionSelectionPolicy(
        policy_id="select_btc_put_30_delta",
        version="2",
        right=OptionRight.PUT,
        target_days_to_expiry=45,
        minimum_days_to_expiry=40,
        maximum_days_to_expiry=50,
        target_delta=Decimal("-0.30"),
        maximum_delta_distance=Decimal("0.05"),
        minimum_liquidity_weight=Decimal("0.25"),
        fallback=DeltaFallback.REJECT,
    )
    pricing_adapter = BlackScholesPricingAdapter()

    def calculated_delta(
        contract: OptionContract,
        quote: OptionQuote,
    ) -> CalculatedOptionDelta:
        observed_price = quote.last
        assert observed_price is not None
        valuation_input = ValuationInputSnapshot(
            valuation_input_id=f"selection_input_{contract.contract_id}",
            contract=contract,
            quote=quote,
            valuation_at=quote.as_of,
            spot_price=Decimal("50"),
            risk_free_rate=Decimal("0.04"),
            dividend_yield=Decimal("0.01"),
            forward_price=forward.value,
            spot_availability=option_quote_availability,
            rate_availability=option_quote_availability,
            dividend_availability=option_quote_availability,
            forward_availability=option_quote_availability,
            source_manifest_hashes=tuple(
                sorted((quote.content_hash, decision_state.state_hash()))
            ),
        )
        seed_state = pricing_adapter.bind_state(valuation_input, Decimal("0.50"))
        implied_volatility = pricing_adapter.implied_parameter(
            contract,
            observed_price,
            seed_state,
        )
        pricing_state = pricing_adapter.bind_state(
            valuation_input,
            implied_volatility,
        )
        return CalculatedOptionDelta(
            contract_id=contract.contract_id,
            calculated_at=OPTION_DECISION,
            known_at=OPTION_DECISION,
            delta=pricing_adapter.greeks(contract, pricing_state).delta,
            market_state_hash=decision_state.state_hash(),
            model_specification_hash=pricing_adapter.specification.content_hash,
            valuation_input_hash=pricing_state.valuation_input_hash,
        )

    option_selection = select_option_contract(
        cleaned_chain,
        option_selection_policy,
        (
            calculated_delta(selected_contract, selected_quote),
            calculated_delta(alternate_contract, alternate_quote),
        ),
    )
    assert option_selection.selected_contract_id == selected_contract.contract_id
    assert option_selection.selected_delta_evidence_hash is not None
    option_fill = simulate_option_fill(
        fill_id="fill_btc_put45_entry",
        contract=selected_contract,
        quote=selected_quote,
        side=TransactionSide.BUY,
        quantity=Decimal("1"),
        filled_at=_iso(OPTION_DECISION),
        fee_per_contract=Decimal("1"),
    )
    assert option_fill.price is not None
    option_position = position_from_fill(
        option_fill,
        position_id="position_btc_put45",
    )
    option_margin_model_hash = _hash("option-margin-policy")
    analytics_factory = BlackScholesOptionAnalyticsFactory(
        margin_model_hash=option_margin_model_hash,
        pricing_adapter=pricing_adapter,
    )

    def priced_path_state(
        at: datetime,
        *,
        label: str,
        market: Decimal,
        spot: Decimal,
        transaction_cost: Decimal = Decimal("0"),
    ) -> tuple[MarketState, OptionPathMark]:
        availability = _availability(_iso(at - timedelta(seconds=5)))
        market_quote = OptionQuote(
            quote_id=f"pricing_quote_{label}",
            contract_id=selected_contract.contract_id,
            availability=availability,
            as_of=_iso(at),
            bid=market - Decimal("0.2"),
            ask=market + Decimal("0.2"),
            last=market,
            bid_size=Decimal("20"),
            ask_size=Decimal("20"),
            volume=100,
            open_interest=1000,
        )
        valuation_input = ValuationInputSnapshot(
            valuation_input_id=f"valuation_input_{label}",
            contract=selected_contract,
            quote=market_quote,
            valuation_at=_iso(at),
            spot_price=spot,
            risk_free_rate=Decimal("0.04"),
            dividend_yield=Decimal("0.01"),
            forward_price=spot,
            spot_availability=availability,
            rate_availability=availability,
            dividend_availability=availability,
            forward_availability=availability,
            source_manifest_hashes=tuple(
                sorted((market_quote.content_hash, store.content_hash()))
            ),
        )
        quote_metadata = ObservationMetadata(
            observed_at=_iso(at - timedelta(seconds=5)),
            knowledge_at=_iso(at - timedelta(seconds=5)),
            source_hash=market_quote.content_hash,
            calendar_id=CALENDAR_ID,
            max_age_seconds=60,
        )
        typed_quote = OptionContractQuote(
            contract_id=selected_contract.contract_id,
            underlying_instrument_id=selected_contract.underlying_id,
            expiry_at=selected_contract.expiration_at,
            right=MarketStateOptionRight.PUT,
            strike=selected_contract.strike,
            currency=selected_contract.currency,
            price_unit="USD_per_coin",
            bid=market_quote.bid or market,
            ask=market_quote.ask or market,
            last=market_quote.last,
            settlement=None,
            bid_size=market_quote.bid_size,
            ask_size=market_quote.ask_size,
            volume=Decimal(market_quote.volume),
            open_interest=Decimal(market_quote.open_interest),
            condition=QuoteCondition.NORMAL,
            metadata=quote_metadata,
        )
        analytics = analytics_factory.derive(
            quote=typed_quote,
            valuation_input=valuation_input,
            margin_per_contract=market * selected_contract.multiplier,
            collateral_per_contract=market * selected_contract.multiplier,
        )
        implied_volatility = analytics.implied_volatility
        model_price = analytics.model_price
        pricing_state = pricing_adapter.bind_state(
            valuation_input,
            implied_volatility,
        )
        greeks = pricing_adapter.greeks(selected_contract, pricing_state)
        assert (
            greeks.delta,
            greeks.gamma,
            greeks.vega_per_vol_point,
            greeks.theta_per_calendar_day,
            greeks.rho_per_rate_point,
        ) == (
            analytics.delta,
            analytics.gamma,
            analytics.vega,
            analytics.theta,
            analytics.rho,
        )
        option_chain_state = OptionChainState(
            chain_id=f"market_state_option_chain_{label}",
            underlying_instrument_id=selected_contract.underlying_id,
            currency=selected_contract.currency,
            price_unit=typed_quote.price_unit,
            quotes=(typed_quote,),
            analytics=(analytics,),
            metadata=ObservationMetadata(
                observed_at=_iso(at),
                knowledge_at=_iso(at),
                source_hash=evidence_hash(
                    {
                        "quote": typed_quote.content_hash,
                        "analytics": analytics.content_hash,
                    },
                    label="option-chain-market-state",
                ),
                calendar_id=CALENDAR_ID,
                max_age_seconds=60,
            ),
        )
        base_state = _market_state_for(
            valuation=at,
            spot=spot,
            volatility=implied_volatility,
            state_id=f"state_{label}",
        )
        liquidity = tuple(
            replace(
                item,
                bid=typed_quote.bid,
                ask=typed_quote.ask,
                metadata=quote_metadata,
            )
            if item.instrument_id == OPTION_ID
            else item
            for item in base_state.liquidity_quotes
        )
        state = replace(
            base_state,
            liquidity_quotes=liquidity,
            futures_curves=(market_state_futures_curve,),
            option_chains=(option_chain_state,),
        )
        path = OptionPathMark(
            contract_id=OPTION_ID,
            marked_at=at,
            market_state_hash=state.state_hash(),
            market_quote_hash=typed_quote.content_hash,
            model_specification_hash=pricing_adapter.specification.content_hash,
            market_price=market,
            theoretical_price=model_price,
            spot_price=spot,
            implied_volatility=implied_volatility,
            rate=Decimal("0.04"),
            dividend_yield=Decimal("0.01"),
            skew=Decimal("-0.05"),
            greeks=greeks,
            transaction_cost_since_previous=transaction_cost,
        )
        assert abs(model_price - market) <= pricing_adapter.model.price_tolerance
        assert analytics.valuation_input_hash == valuation_input.content_hash
        return state, path

    decision_state, decision_option_mark = priced_path_state(
        OPTION_DECISION,
        label="option_decision",
        market=option_fill.price,
        spot=Decimal("50"),
    )
    intermediate_state, intermediate_option_mark = priced_path_state(
        OPTION_DECISION + timedelta(days=1),
        label="option_day_1",
        market=Decimal("6.2"),
        spot=Decimal("47"),
        transaction_cost=Decimal("1"),
    )
    final_state, final_option_mark = priced_path_state(
        FINAL_MARK_AT,
        label="option_day_3",
        market=Decimal("4.5"),
        spot=Decimal("49"),
    )
    option_marks = (
        decision_option_mark,
        intermediate_option_mark,
        final_option_mark,
    )
    assert intermediate_state.option_analytics_mark(OPTION_ID).content_hash.startswith(
        "sha256:"
    )
    option_attribution = attribute_option_path(
        option_marks,
        signed_quantity=Decimal("1"),
        multiplier=selected_contract.multiplier,
        policy=OptionAttributionPolicy(
            policy_id="btc-vanilla-path-residual-v1",
            version="1",
            maximum_absolute_residual=Decimal("1000"),
            maximum_relative_residual=Decimal("0.25"),
        ),
    )
    settlement_input = OptionSettlementInput(
        settlement_input_id="settlement_input_btc_put45",
        contract_id=OPTION_ID,
        settlement_at=_iso(OPTION_EXPIRY),
        availability=_availability(_iso(OPTION_EXPIRY)),
        spot_price=Decimal("40"),
        source_manifest_hash=_hash("option-expiry-settlement"),
    )
    option_lifecycle = simulate_option_lifecycle(
        option_position,
        event_id="lifecycle_btc_put45_expiry",
        event_at=_iso(OPTION_EXPIRY),
        settlement_input=settlement_input,
    )
    option_entry_drafts = adapt_option_fill(option_fill)  # type: ignore[arg-type]
    option_mark_drafts = tuple(
        mark_event(
            event_id=f"option_mark_{index}",
            occurred_at=_iso(mark.marked_at),
            asset_class=AssetClass.OPTION,
            instrument_id=OPTION_ID,
            currency="USD",
            mark_price=mark.market_price,
            source_hashes=(mark.content_hash,),
        )
        for index, mark in enumerate(option_marks[1:], start=1)
    )
    lifecycle_draft = adapt_option_lifecycle(
        option_lifecycle,
        position=option_position,
    )
    option_live_ledger = (
        UnifiedPortfolioLedger.open(
            ledger_id="ledger_option_required",
            base_currency="USD",
        )
        .publish(
            funding_event(
                event_id="funding_option",
                occurred_at=_iso(OPTION_DECISION - timedelta(minutes=1)),
                cash_deltas=(CashDelta("USD", Decimal("1000")),),
            )
        )
        .publish_many(option_entry_drafts)
        .publish_many(option_mark_drafts)
    )
    option_live_snapshot = option_live_ledger.replay()
    _, option_live_valuation = _accounting(option_live_snapshot)
    option_ledger = option_live_ledger.publish(lifecycle_draft)
    option_snapshot = option_ledger.replay()
    option_accounting, option_valuation = _accounting(option_snapshot)
    option_ledger_cashflow = sum(
        (
            delta.amount
            for event in option_ledger.events
            if event.event_type is not PortfolioEventType.FUNDING
            for delta in event.cash_deltas
        ),
        Decimal("0"),
    )
    option_objects = _objects(
        trades=(option_fill.as_dict(),),
        positions=option_live_snapshot.positions,
        events=option_ledger.events,
        valuation=option_valuation,
        exposure={
            "position_hash": option_position.content_hash,
            "terminal_quantity": "0",
            "mark_hash": option_marks[-1].content_hash,
        },
        attribution={
            "content_hash": option_attribution.content_hash,
            "actual_pnl": str(option_attribution.actual_pnl),
            "attributed_pnl": str(option_attribution.attributed_pnl),
        },
        scenario_output={
            "chain_hash": cleaned_chain.content_hash,
            "selection_hash": option_selection.content_hash,
            "lifecycle_hash": option_lifecycle.content_hash,
        },
    )
    option_trace = OptionScenarioTrace(
        decision_at=_iso(OPTION_DECISION),
        maximum_chain_knowledge_at=_iso(
            max(item.known_at for item in cleaned_chain.points)
        ),
        chain_hash=cleaned_chain.content_hash,
        selected_contract_id=option_selection.selected_contract_id or "",
        selection_hash=option_selection.content_hash,
        entry_fill_hash=option_fill.content_hash,
        path_mark_hashes=tuple(item.content_hash for item in option_marks),
        lifecycle_hash=option_lifecycle.content_hash,
        ledger_hash=option_ledger.content_hash,
        market_price_hash=evidence_hash(
            {
                "quote_hash": selected_quote.content_hash,
                "market_price": str(option_fill.price),
            },
            label="option-market-price",
        ),
        model_price_hash=evidence_hash(
            {
                "model_hash": pricing_adapter.model.content_hash,
                "model_specification_hash": (
                    pricing_adapter.specification.content_hash
                ),
                "theoretical_price": str(option_marks[0].theoretical_price),
            },
            label="option-model-price",
        ),
        premium_and_lifecycle_cashflow=(
            option_fill.cash_flow + option_lifecycle.cash_delta
        ),
        ledger_option_cashflow=option_ledger_cashflow,
        attributed_pnl=option_attribution.attributed_pnl,
        actual_pnl=option_attribution.actual_pnl,
        accounting=option_accounting,
        object_hashes=option_objects,
    )

    integrated_ledger = (
        spot_ledger.publish_many(futures_operating_drafts)
        .publish_many(option_entry_drafts)
        .publish_many(option_mark_drafts)
    )
    integrated_snapshot = integrated_ledger.replay()
    integrated_accounting, integrated_valuation = _accounting(integrated_snapshot)
    source_hash_by_id = {
        SPOT_ID: next(
            item.content_hash
            for item in integrated_ledger.events
            if item.event_id == "spot_entry"
        ),
        NEW_FUTURE_ID: open_fill.content_hash,
        OPTION_ID: option_fill.content_hash,
    }
    opened_at_by_id = {
        SPOT_ID: _iso(DECISION),
        NEW_FUTURE_ID: FUTURES_AS_OF,
        OPTION_ID: _iso(OPTION_DECISION),
    }
    exposure_positions = tuple(
        ExposurePosition(
            position_id=f"exposure_{item.instrument_id}",
            instrument_id=item.instrument_id,
            quantity=item.quantity,
            quantity_unit=(
                "coin" if item.asset_class is AssetClass.SPOT else "contract"
            ),
            multiplier=item.multiplier,
            currency=item.currency,
            source_hash=source_hash_by_id[item.instrument_id],
            opened_at=opened_at_by_id[item.instrument_id],
        )
        for item in integrated_snapshot.positions
    )
    catalog: ProductCatalog = registry
    exposure_engine = ExposureEngine.with_default_spot(
        product_catalog=catalog,
        derivative_adapters=(
            FuturesValuationAdapter(
                margin_model_hash=final_state.futures_contract_quote(
                    NEW_FUTURE_ID
                ).margin_model_hash,
            ),
            OptionValuationAdapter(
                pricing_model_hash=final_state.option_analytics_mark(
                    OPTION_ID
                ).model_hash,
                model_specification_hash=final_state.option_analytics_mark(
                    OPTION_ID
                ).model_specification_hash,
                margin_model_hash=final_state.option_analytics_mark(
                    OPTION_ID
                ).margin_model_hash,
            ),
        ),
    )
    exposure = exposure_engine.evaluate(
        snapshot_id="exposure_required_integrated",
        positions=exposure_positions,
        market_state=final_state,
    )
    joint_shock = JointMarketShock(
        scenario_id="joint_btc_downside",
        price_returns=(
            (SPOT_ID, Decimal("-0.10")),
            (NEW_FUTURE_ID, Decimal("-0.08")),
            (OPTION_ID, Decimal("0.25")),
        ),
        volatility_shifts=(("surface_btc_puts", Decimal("0.05")),),
        rate_shifts=(("rate_usd_30d", Decimal("0.01")),),
        liquidity_haircuts=((SPOT_ID, Decimal("0.02")),),
        liquidity_cost_multiplier=Decimal("2"),
        margin_multiplier=Decimal("1.25"),
        source_hashes=(final_state.state_hash(),),
    )
    joint_scenario = JointScenarioEngine().evaluate(
        integrated_snapshot,
        market_state=final_state,  # type: ignore[arg-type]
        shock=joint_shock,
        base_liquidation_costs={
            SPOT_ID: spot_cost.total,
            NEW_FUTURE_ID: open_fill.total_cost,
            OPTION_ID: option_fill.fee,
        },
    )
    terminal_ledger = integrated_ledger.publish(lifecycle_draft)
    terminal_snapshot = terminal_ledger.replay()
    terminal_quantities = {
        item.instrument_id: item.quantity for item in terminal_snapshot.positions
    }
    integrated_leg_pnls = (
        spot_valuation.economic_pnl,
        futures_valuation.economic_pnl,
        option_live_valuation.economic_pnl,
    )
    integrated_legs = (
        IntegratedLegResult(
            leg_id="leg_spot",
            instrument_id=SPOT_ID,
            trade_hash=source_hash_by_id[SPOT_ID],
            cost=spot_cost.total,
            pnl=integrated_leg_pnls[0],
            terminal_quantity=terminal_quantities[SPOT_ID],
        ),
        IntegratedLegResult(
            leg_id="leg_future",
            instrument_id=NEW_FUTURE_ID,
            trade_hash=open_fill.content_hash,
            cost=open_fill.total_cost,
            pnl=integrated_leg_pnls[1],
            terminal_quantity=terminal_quantities[NEW_FUTURE_ID],
        ),
        IntegratedLegResult(
            leg_id="leg_option",
            instrument_id=OPTION_ID,
            trade_hash=option_fill.content_hash,
            cost=option_fill.fee,
            pnl=integrated_leg_pnls[2],
            terminal_quantity=terminal_quantities.get(OPTION_ID, Decimal("0")),
        ),
    )
    integrated_objects = _objects(
        trades=(
            execution.identity_payload(),
            open_fill.as_dict(),
            option_fill.as_dict(),
        ),
        positions=integrated_snapshot.positions,
        events=integrated_ledger.events,
        valuation=integrated_valuation,
        exposure=exposure.as_dict(),
        attribution={
            "legs": [
                {
                    "instrument_id": item.instrument_id,
                    "cost": str(item.cost),
                    "pnl": str(item.pnl),
                    "terminal_quantity": str(item.terminal_quantity),
                }
                for item in integrated_legs
            ],
            "option_attribution_hash": option_attribution.content_hash,
            "futures_reconciliation_hash": futures_pnl.content_hash,
        },
        scenario_output=joint_scenario.identity_payload(),
    )
    integrated_trace = IntegratedScenarioTrace(
        execution_mode="SIMULTANEOUS_ATOMIC",
        legs=integrated_legs,
        common_ledger_hash=integrated_ledger.content_hash,
        ledger_reconciled=integrated_valuation.reconciled,
        exposure_hash=exposure.content_hash,
        exposure_reconciled=all(
            item.expected == item.actual for item in exposure.evidence.invariant_checks
        ),
        scenario_result_hash=joint_scenario.content_hash,
        scenario_repriced=(
            joint_scenario.original_state_unchanged
            and any(
                item.shocked_mark != item.base_mark
                for item in joint_scenario.position_results
            )
        ),
        strategy_pnl=sum(integrated_leg_pnls, Decimal("0")),
        accounting=integrated_accounting,
        object_hashes=integrated_objects,
    )
    accounting_reconciliation = _report_ledger_reconciliation(
        integrated_ledger,
    )
    core_objects = _objects(
        trades=(
            execution.identity_payload(),
            entry_fill.as_dict(),
            close_fill.as_dict(),
            open_fill.as_dict(),
            option_fill.as_dict(),
        ),
        positions=integrated_snapshot.positions,
        events=terminal_ledger.events,
        valuation=integrated_valuation,
        exposure=exposure.as_dict(),
        attribution={
            "option": option_attribution.content_hash,
            "futures": futures_pnl.content_hash,
            "leg_pnl": [str(item) for item in integrated_leg_pnls],
        },
        scenario_output=joint_scenario.identity_payload(),
    )
    core_artifact_hash = evidence_hash(
        {
            "objects": core_objects.as_dict(),
            "registry": registry.contract_hash(),
            "dataset": store.content_hash(),
            "market_state": final_state.state_hash(),
        },
        label="required-scenarios-core-artifact",
    )
    return _Run(
        registry=registry,
        store=store,
        pit_records=pit_records,
        decision_state=decision_state,
        final_state=final_state,
        hypothesis=hypothesis,
        expression_decision=expression_decision,
        futures_reference=futures_reference,
        futures_curve=futures_curve,
        option_chain=cleaned_chain,
        option_selection=option_selection,
        spot_trace=spot_trace,
        futures_trace=futures_trace,
        option_trace=option_trace,
        integrated_trace=integrated_trace,
        accounting_reconciliation=accounting_reconciliation,
        core_objects=core_objects,
        core_artifact_hash=core_artifact_hash,
        integrated_ledger=integrated_ledger,
        terminal_ledger=terminal_ledger,
        exposure=exposure,
        joint_scenario=joint_scenario,
    )


@pytest.fixture
def required_scenario_runs() -> tuple[_Run, _Run]:
    return _execute_required_scenarios(), _execute_required_scenarios()


def _bindings(run: _Run) -> ResearchEvidenceBindings:
    return ResearchEvidenceBindings(
        dataset_snapshot_hashes=(run.store.content_hash(),),
        product_registry_hash=run.registry.contract_hash(),
        market_state_hashes=tuple(
            sorted(
                {
                    run.decision_state.state_hash(),
                    run.final_state.state_hash(),
                }
            )
        ),
        hypothesis_hash=run.hypothesis.content_hash,
        policy_hashes=tuple(
            sorted(
                {
                    DEFAULT_EXPRESSION_POLICY.content_hash,
                    DEFAULT_OPTION_CLEANING_POLICY.content_hash,
                    run.expression_decision.policy_hash,
                    run.option_selection.policy_hash,
                    run.exposure.evidence.policy_hash,
                }
            )
        ),
        code_hash=_hash("required-scenario-code-v2"),
        environment_hash=evidence_hash(
            {"python": platform.python_version()},
            label="required-scenario-environment",
        ),
        configuration_hash=evidence_hash(
            {
                "seed": 7,
                "spot_quantity": "1000",
                "future_quantity": "2",
                "option_quantity": "1",
            },
            label="required-scenario-configuration",
        ),
        seed=7,
    )


def test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence(
    required_scenario_runs: tuple[_Run, _Run],
    tmp_path: Path,
) -> None:
    first, second = required_scenario_runs
    reproduction = ReproducibilityScenarioTrace(
        first=first.core_objects,
        second=second.core_objects,
        first_core_artifact_hash=first.core_artifact_hash,
        second_core_artifact_hash=second.core_artifact_hash,
        object_hashes=reproduction_object_hashes(
            first.core_objects,
            second.core_objects,
        ),
    )
    first_study = build_validated_multi_asset_study(
        experiment_id="required-scenarios-e2e",
        bindings=_bindings(first),
        spot=first.spot_trace,
        futures=first.futures_trace,
        option=first.option_trace,
        integrated=first.integrated_trace,
        reproduction=reproduction,
        accounting_reconciliation=first.accounting_reconciliation,
    )
    second_study = build_validated_multi_asset_study(
        experiment_id="required-scenarios-e2e",
        bindings=_bindings(second),
        spot=second.spot_trace,
        futures=second.futures_trace,
        option=second.option_trace,
        integrated=second.integrated_trace,
        reproduction=reproduction,
        accounting_reconciliation=second.accounting_reconciliation,
    )
    receipt = compare_studies(first_study, second_study)

    assert [item.layer for item in first.pit_records] == [
        DataLayer.DERIVED,
        DataLayer.NORMALIZED,
        DataLayer.RAW,
    ]
    assert all(
        datetime.fromisoformat(item.clocks.knowledge_at) <= DECISION
        for item in first.pit_records
    )
    assert first.futures_reference.specification.contract_id == NEW_FUTURE_ID
    assert first.futures_curve.chain_snapshot_hash.startswith("sha256:")
    assert first.option_selection.selected_contract_id == OPTION_ID
    assert len(first.option_trace.path_mark_hashes) == 3
    futures_market_quote = first.final_state.futures_contract_quote(NEW_FUTURE_ID)
    option_market_quote = first.final_state.option_contract_quote(OPTION_ID)
    option_analytics = first.final_state.option_analytics_mark(OPTION_ID)
    assert futures_market_quote.mark_price == Decimal("100")
    assert option_analytics.source_quote_hash == option_market_quote.content_hash
    assert option_analytics.market_price == Decimal("4.5")
    assert option_analytics.model_price > Decimal("0")
    assert option_analytics.implied_volatility > Decimal("0")
    assert option_analytics.delta < Decimal("0")
    production_adapter_hashes = {
        FuturesValuationAdapter(
            margin_model_hash=futures_market_quote.margin_model_hash,
        ).content_hash,
        OptionValuationAdapter(
            pricing_model_hash=option_analytics.model_hash,
            model_specification_hash=option_analytics.model_specification_hash,
            margin_model_hash=option_analytics.margin_model_hash,
        ).content_hash,
    }
    assert production_adapter_hashes <= {
        item.adapter_hash for item in first.exposure.positions
    }
    assert {item.instrument_id for item in first.exposure.positions} == {
        SPOT_ID,
        NEW_FUTURE_ID,
        OPTION_ID,
    }
    assert first.joint_scenario.ledger_hash == first.integrated_ledger.content_hash
    assert OPTION_ID not in {
        item.instrument_id for item in first.terminal_ledger.replay().positions
    }
    assert tuple(item.scenario_id for item in first_study.scenarios) == (
        "T-01",
        "T-02",
        "T-03",
        "T-04",
        "T-05",
    )
    assert all(item.status.value == "PASS" for item in first_study.scenarios)
    assert first_study.accounting_reconciliation.ledger.ledger_hash == (
        first.integrated_ledger.content_hash
    )
    assert first_study.accounting_reconciliation.ledger.nav_identity_error == 0
    assert first_study.accounting_reconciliation.ledger.attribution_identity_error == 0
    assert receipt.reproduced
    assert first.core_objects == second.core_objects

    external_root = tmp_path.resolve()
    project_root = Path(__file__).resolve().parents[1]
    assert not ResearchPathManager.is_within(external_root, project_root)
    settings = ResearchSettings(
        data_root=external_root / "datasets",
        artifact_root=external_root / "artifacts",
        report_root=external_root / "reports",
        cache_root=external_root / "cache",
        db_path=None,
        max_workers=1,
        random_seed=7,
    )
    paths = ResearchPathManager.from_settings(settings, project_root=project_root)
    created = publish_validated_study(first_study, paths=paths)
    reverified = publish_validated_study(second_study, paths=paths)
    artifact_payload = json.loads(created.artifact_path.read_text(encoding="utf-8"))
    report_payload = json.loads(created.report_path.read_text(encoding="utf-8"))

    assert created.created
    assert not reverified.created
    assert created.artifact_hash == first_study.content_hash
    assert reverified.artifact_hash == second_study.content_hash
    assert artifact_payload == first_study.as_dict()
    assert (
        artifact_payload["accounting_reconciliation"]["receipt"]["content_hash"]
        == first.accounting_reconciliation.content_hash
    )
    assert report_payload["study_content_hash"] == first_study.content_hash
    assert report_payload["all_mandatory_scenarios_passed"] is True
    assert report_payload["ledger_nav_reconciled"] is True
    assert report_payload["report_ledger_reconciled"] is True
    assert report_payload["attribution_reconciled"] is True
