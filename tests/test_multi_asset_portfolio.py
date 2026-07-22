from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_research.research.derivatives.common import AvailabilityTimes
from market_research.research.derivatives.futures import (
    FuturesFill,
    OrderSide,
    SessionType,
    SettlementEvent,
)
from market_research.research.derivatives.options import (
    ExerciseStyle,
    FillStatus,
    LifecycleEventType,
    OptionContract,
    OptionFill,
    OptionLifecycleEvent,
    OptionSettlementInput,
    OptionType,
    SettlementType as OptionSettlementType,
    TransactionSide,
    position_from_fill,
)
from market_research.research.multi_asset.costs import (
    ExecutionContext,
    ExecutionCostError,
    ExecutionSide,
    FillDisposition,
    LinearExecutionCostModel,
    execution_context_from_fill,
)
from market_research.research.multi_asset.market_state import (
    FXQuote,
    MarketState,
    ObservationMetadata,
    SpotQuote,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    CashDelta,
    ExternalFlowConversionEvidence,
    PortfolioAccountingError,
    PortfolioEventDraft,
    PortfolioEventType,
    UnifiedPortfolioLedger,
    adapt_futures_fill,
    adapt_futures_settlement,
    adapt_corporate_action_application,
    adapt_option_fill,
    adapt_option_lifecycle,
    adapt_spot_posting,
    collateral_income_event,
    funding_event,
    mark_event,
    trade_event,
)
from market_research.research.multi_asset.scenarios import (
    JointMarketShock,
    JointScenarioEngine,
    ScenarioError,
    ShockedMarketState,
)
from market_research.research.multi_asset.spot import (
    BorrowScenario,
    BorrowSnapshot,
    CashBalance as SpotCashBalance,
    CorporateAction,
    CorporateActionType,
    SpotBook,
    SpotPosition,
    accrue_borrow_cost,
    apply_corporate_action,
)


_HASH = "sha256:" + ("1" * 64)
_HASH_2 = "sha256:" + ("2" * 64)
_T0 = "2026-06-01T09:00:00+00:00"


def _eur_flow_evidence(
    observed_at: str,
    *,
    rate: Decimal = Decimal("1.1"),
    source_hash: str = _HASH,
) -> tuple[ExternalFlowConversionEvidence, ...]:
    return (
        ExternalFlowConversionEvidence(
            currency="EUR",
            base_currency="USD",
            observed_at=observed_at,
            fx_rate=rate,
            source_hash=source_hash,
        ),
    )


def _cost_event(
    event_id: str,
    event_type: PortfolioEventType,
    amount: str,
    *,
    occurred_at: str,
) -> PortfolioEventDraft:
    return PortfolioEventDraft(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        currency="USD",
        cash_deltas=(CashDelta("USD", -Decimal(amount)),),
    )


def test_execution_context_models_capacity_partial_unfilled_and_cost_components() -> (
    None
):
    context = ExecutionContext(
        execution_id="exec.option.1",
        instrument_id="OPT.1",
        instrument_kind="OPTION",
        currency="USD",
        side=ExecutionSide.BUY,
        requested_quantity=Decimal("10"),
        filled_quantity=Decimal("4"),
        reference_price=Decimal("100"),
        execution_price=Decimal("101"),
        observed_at=_T0,
        multiplier=Decimal("2"),
        capacity_quantity=Decimal("5"),
        participation_rate=Decimal("0.25"),
        borrow_notional=Decimal("1000"),
        financing_notional=Decimal("500"),
        fx_notional=Decimal("200"),
        option_leg_count=2,
        source_hashes=(_HASH,),
    )
    model = LinearExecutionCostModel(
        commission_per_unit=Decimal("1"),
        minimum_commission=Decimal("2"),
        tax_bps=Decimal("10"),
        impact_bps=Decimal("20"),
        participation_bps=Decimal("5"),
        borrow_bps=Decimal("100"),
        financing_bps=Decimal("20"),
        fx_bps=Decimal("10"),
        option_leg_fee=Decimal("3"),
        tax_on_sell_only=False,
    )

    breakdown = model.estimate(context)

    assert context.disposition is FillDisposition.PARTIAL
    assert context.unfilled_quantity == Decimal("6")
    assert context.capacity_utilization == Decimal("0.8")
    assert context.gross_notional == Decimal("808")
    assert breakdown.execution_hash == context.content_hash
    assert breakdown.spread == Decimal("8")
    assert breakdown.commission == Decimal("4")
    assert breakdown.tax == Decimal("0.808")
    assert breakdown.market_impact == Decimal("1.616")
    assert breakdown.participation == Decimal("0.101")
    assert breakdown.borrow == Decimal("10")
    assert breakdown.financing == Decimal("1")
    assert breakdown.fx == Decimal("0.2")
    assert breakdown.option_leg == Decimal("6")
    assert breakdown.total == Decimal("31.725")

    unfilled = ExecutionContext(
        execution_id="exec.unfilled",
        instrument_id="SPOT.1",
        instrument_kind="SPOT",
        currency="USD",
        side=ExecutionSide.SELL,
        requested_quantity=Decimal("3"),
        filled_quantity=Decimal("0"),
        reference_price=Decimal("10"),
        execution_price=None,
        observed_at=_T0,
        capacity_quantity=Decimal("0"),
    )
    assert unfilled.disposition is FillDisposition.UNFILLED
    assert model.estimate(unfilled).total == Decimal("0")

    with pytest.raises(ExecutionCostError, match="must_be_decimal"):
        replace(context, filled_quantity=4.0)  # type: ignore[arg-type]
    with pytest.raises(ExecutionCostError, match="capacity_exceeded"):
        replace(context, filled_quantity=Decimal("6"))


def _complete_ledger() -> UnifiedPortfolioLedger:
    drafts = (
        funding_event(
            event_id="funding",
            occurred_at="2026-06-01T09:00:00+00:00",
            cash_deltas=(
                CashDelta("EUR", Decimal("1000")),
                CashDelta("USD", Decimal("20000")),
            ),
            conversion_evidence=_eur_flow_evidence("2026-06-01T09:00:00+00:00"),
        ),
        trade_event(
            event_id="spot.buy",
            occurred_at="2026-06-01T10:00:00+00:00",
            asset_class=AssetClass.SPOT,
            instrument_id="AAPL",
            currency="USD",
            quantity_delta=Decimal("10"),
            price=Decimal("100"),
        ),
        mark_event(
            event_id="spot.mark",
            occurred_at="2026-06-01T11:00:00+00:00",
            asset_class=AssetClass.SPOT,
            instrument_id="AAPL",
            currency="USD",
            mark_price=Decimal("110"),
        ),
        trade_event(
            event_id="future.open",
            occurred_at="2026-06-01T12:00:00+00:00",
            asset_class=AssetClass.FUTURE,
            instrument_id="ESM6",
            currency="USD",
            quantity_delta=Decimal("2"),
            price=Decimal("4000"),
            multiplier=Decimal("5"),
        ),
        PortfolioEventDraft(
            event_id="collateral.post",
            event_type=PortfolioEventType.COLLATERAL_TRANSFER,
            occurred_at="2026-06-01T12:01:00+00:00",
            currency="USD",
            cash_deltas=(CashDelta("USD", Decimal("-1000")),),
            collateral_delta=Decimal("1000"),
        ),
        PortfolioEventDraft(
            event_id="margin.required",
            event_type=PortfolioEventType.MARGIN_REQUIREMENT,
            occurred_at="2026-06-01T12:02:00+00:00",
            currency="USD",
            instrument_id="ESM6",
            asset_class=AssetClass.FUTURE,
            margin_requirement=Decimal("800"),
        ),
        PortfolioEventDraft(
            event_id="future.settle",
            event_type=PortfolioEventType.FUTURES_SETTLEMENT,
            occurred_at="2026-06-02T12:00:00+00:00",
            currency="USD",
            cash_deltas=(CashDelta("USD", Decimal("100")),),
            instrument_id="ESM6",
            asset_class=AssetClass.FUTURE,
            multiplier=Decimal("5"),
            mark_price=Decimal("4010"),
            realized_pnl=Decimal("100"),
            settlement_quantity=Decimal("2"),
        ),
        trade_event(
            event_id="option.buy",
            occurred_at="2026-06-03T10:00:00+00:00",
            asset_class=AssetClass.OPTION,
            instrument_id="AAPL.C100",
            currency="USD",
            quantity_delta=Decimal("1"),
            price=Decimal("5"),
            multiplier=Decimal("100"),
        ),
        mark_event(
            event_id="option.mark",
            occurred_at="2026-06-30T10:00:00+00:00",
            asset_class=AssetClass.OPTION,
            instrument_id="AAPL.C100",
            currency="USD",
            mark_price=Decimal("20"),
        ),
        PortfolioEventDraft(
            event_id="option.exercise",
            event_type=PortfolioEventType.OPTION_LIFECYCLE,
            occurred_at="2026-07-01T10:00:00+00:00",
            currency="USD",
            cash_deltas=(CashDelta("USD", Decimal("-10000")),),
            instrument_id="AAPL.C100",
            asset_class=AssetClass.OPTION,
            quantity_delta=Decimal("-1"),
            multiplier=Decimal("100"),
            deliverable_asset_id="AAPL",
            deliverable_asset_class=AssetClass.SPOT,
            deliverable_currency="USD",
            deliverable_quantity_delta=Decimal("100"),
            deliverable_basis_price=Decimal("100"),
            deliverable_mark_price=Decimal("120"),
            metadata=(("lifecycle_type", "EXERCISE"),),
        ),
        PortfolioEventDraft(
            event_id="fx.convert",
            event_type=PortfolioEventType.FX_CONVERSION,
            occurred_at="2026-07-01T11:00:00+00:00",
            cash_deltas=(
                CashDelta("EUR", Decimal("-100")),
                CashDelta("USD", Decimal("110")),
            ),
            metadata=(("rate", "1.1_USD_per_EUR"),),
        ),
        _cost_event(
            "fee",
            PortfolioEventType.FEE,
            "10",
            occurred_at="2026-07-01T12:00:00+00:00",
        ),
        _cost_event(
            "tax",
            PortfolioEventType.TAX,
            "5",
            occurred_at="2026-07-01T12:01:00+00:00",
        ),
        _cost_event(
            "borrow",
            PortfolioEventType.BORROW_COST,
            "2",
            occurred_at="2026-07-01T12:02:00+00:00",
        ),
        _cost_event(
            "financing",
            PortfolioEventType.FINANCING_COST,
            "3",
            occurred_at="2026-07-01T12:03:00+00:00",
        ),
    )
    return UnifiedPortfolioLedger.open(
        ledger_id="portfolio.complete",
        base_currency="USD",
    ).publish_many(drafts)


def test_unified_portfolio_replay_hash_nav_pnl_and_available_capital() -> None:
    empty = UnifiedPortfolioLedger.open(
        ledger_id="portfolio.complete",
        base_currency="USD",
    )
    ledger = _complete_ledger()
    replayed = ledger.replay()

    assert empty.events == ()
    assert ledger.events[0].previous_hash == "sha256:" + ("0" * 64)
    assert all(
        current.previous_hash == previous.content_hash
        for previous, current in zip(ledger.events, ledger.events[1:])
    )
    assert _complete_ledger().content_hash == ledger.content_hash
    assert replayed.event_count == 15
    assert replayed.option_positions == ()
    assert replayed.futures_positions[0].quantity == Decimal("2")
    assert replayed.futures_positions[0].average_price == Decimal("4010")
    assert replayed.spot_positions[0].quantity == Decimal("110")
    assert replayed.spot_positions[0].average_price == Decimal("100")
    assert replayed.spot_positions[0].mark_price == Decimal("120")
    assert dict((item.currency, item.amount) for item in replayed.cash) == {
        "EUR": Decimal("900"),
        "USD": Decimal("7690"),
    }
    assert replayed.collateral[0].amount == Decimal("1000")
    assert replayed.margins[0].amount == Decimal("800")
    assert replayed.costs[0].amount == Decimal("20")

    valuation = replayed.valuation(
        fx_rates={"USD": Decimal("1"), "EUR": Decimal("1.1")}
    )

    assert valuation.nav == Decimal("22880")
    assert valuation.external_cash_flow == Decimal("21100")
    assert valuation.economic_pnl == Decimal("1780")
    assert valuation.realized_pnl == Decimal("-400")
    assert valuation.unrealized_pnl == Decimal("2200")
    assert valuation.costs == Decimal("20")
    assert valuation.fx_translation_pnl == Decimal("0")
    assert valuation.available_capital == Decimal("8880")
    assert valuation.reconciled

    bad_first = replace(ledger.events[0], previous_hash=_HASH_2)
    with pytest.raises(PortfolioAccountingError, match="hash_chain_broken"):
        replace(ledger, events=(bad_first, *ledger.events[1:]))
    with pytest.raises(PortfolioAccountingError, match="time_regression"):
        ledger.publish(
            funding_event(
                event_id="late-published-old-event",
                occurred_at="2026-01-01T00:00:00+00:00",
                cash_deltas=(CashDelta("USD", Decimal("1")),),
            )
        )


def _option_contract() -> OptionContract:
    return OptionContract(
        contract_id="OPT.PHYSICAL.C100",
        underlying_id="UNDERLYING",
        option_type=OptionType.CALL,
        strike=Decimal("100"),
        expiration_at="2026-07-01T10:00:00+00:00",
        exercise_style=ExerciseStyle.EUROPEAN,
        settlement_type=OptionSettlementType.PHYSICAL,
        multiplier=Decimal("100"),
        currency="USD",
        exchange="XTEST",
        listing_at="2026-01-01T00:00:00+00:00",
        last_trade_at="2026-07-01T10:00:00+00:00",
        settlement_at="2026-07-02T10:00:00+00:00",
        price_tick=Decimal("0.01"),
        deliverable_asset_id="UNDERLYING",
    )


def test_existing_derivative_adapters_preserve_cash_settlement_and_delivery() -> None:
    futures_fill = FuturesFill(
        fill_id="fut.fill.1",
        intent_hash=_HASH,
        contract_id="ESM6",
        quote_hash=_HASH_2,
        filled_at="2026-06-01T10:00:00+00:00",
        trading_date="2026-06-01",
        session=SessionType.DAY,
        side=OrderSide.BUY,
        quantity=2,
        reference_price=Decimal("4000"),
        fill_price=Decimal("4001"),
        multiplier=Decimal("5"),
        commission=Decimal("2"),
        slippage_cost=Decimal("10"),
        realized_trade_pnl=Decimal("0"),
        is_roll_leg=False,
    )
    futures_settlement = SettlementEvent(
        event_id="fut.settlement.1",
        contract_id="ESM6",
        quote_hash=_HASH,
        settled_at="2026-06-02T10:00:00+00:00",
        previous_settlement_price=Decimal("4001"),
        settlement_price=Decimal("4010"),
        quantity=2,
        multiplier=Decimal("5"),
        variation_margin=Decimal("90"),
    )
    contract = _option_contract()
    option_fill = OptionFill(
        fill_id="option.fill.1",
        contract=contract,
        side=TransactionSide.BUY,
        requested_quantity=Decimal("1"),
        filled_quantity=Decimal("1"),
        price=Decimal("5"),
        fee=Decimal("2"),
        slippage_ticks=1,
        filled_at="2026-06-03T10:00:00+00:00",
        quote_hash=_HASH,
        status=FillStatus.FILLED,
    )
    position = position_from_fill(option_fill, position_id="option.position.1")
    available = AvailabilityTimes(
        event_at="2026-07-02T10:00:00+00:00",
        published_at="2026-07-02T10:00:00+00:00",
        provider_received_at="2026-07-02T10:00:00+00:00",
        system_received_at="2026-07-02T10:00:00+00:00",
        processed_at="2026-07-02T10:00:00+00:00",
    )
    settlement_input = OptionSettlementInput(
        settlement_input_id="option.settlement.input.1",
        contract_id=contract.contract_id,
        settlement_at="2026-07-02T10:00:00+00:00",
        availability=available,
        spot_price=Decimal("120"),
        source_manifest_hash=_HASH_2,
    )
    lifecycle = OptionLifecycleEvent(
        event_id="option.lifecycle.1",
        event_type=LifecycleEventType.EXPIRY,
        contract_id=contract.contract_id,
        position_id=position.position_id,
        occurred_at="2026-07-02T10:00:00+00:00",
        settlement_input=settlement_input,
        exercise_fraction=Decimal("1"),
        exercised_quantity=Decimal("1"),
        intrinsic_value_per_unit=Decimal("20"),
        cash_delta=Decimal("-10000"),
        deliverable_quantity_delta=Decimal("100"),
        deliverable_asset_id="UNDERLYING",
        source_position_hash=position.content_hash,
    )

    with pytest.raises(
        PortfolioAccountingError, match="option_lifecycle_position_binding_mismatch"
    ):
        adapt_option_lifecycle(
            replace(lifecycle, source_position_hash=_HASH),
            position=position,
        )
    with pytest.raises(
        PortfolioAccountingError, match="option_lifecycle_economics_mismatch"
    ):
        adapt_option_lifecycle(
            replace(lifecycle, cash_delta=Decimal("-1")),
            position=position,
        )
    with pytest.raises(
        PortfolioAccountingError, match="option_lifecycle_type_mismatch"
    ):
        adapt_option_lifecycle(
            replace(lifecycle, event_type=LifecycleEventType.EXERCISE),
            position=position,
        )
    partial_expiry = adapt_option_lifecycle(
        replace(
            lifecycle,
            exercise_fraction=Decimal("0.5"),
            exercised_quantity=Decimal("0.5"),
            cash_delta=Decimal("-5000"),
            deliverable_quantity_delta=Decimal("50"),
        ),
        position=position,
    )
    assert partial_expiry.quantity_delta == Decimal("-1")

    futures_drafts = adapt_futures_fill(futures_fill, currency="USD")
    option_drafts = adapt_option_fill(option_fill)
    assert sum(
        (delta.amount for draft in futures_drafts for delta in draft.cash_deltas),
        start=Decimal("0"),
    ) == (
        futures_fill.realized_trade_pnl
        - futures_fill.commission
        - futures_fill.slippage_cost
    )
    assert (
        sum(
            (delta.amount for draft in option_drafts for delta in draft.cash_deltas),
            start=Decimal("0"),
        )
        == option_fill.cash_flow
    )

    drafts = (
        funding_event(
            event_id="funding.adapter",
            occurred_at=_T0,
            cash_deltas=(CashDelta("USD", Decimal("20000")),),
        ),
        *futures_drafts,
        adapt_futures_settlement(futures_settlement, currency="USD"),
        *option_drafts,
        adapt_option_lifecycle(
            lifecycle,
            position=position,
        ),
    )
    ledger = UnifiedPortfolioLedger.open(
        ledger_id="portfolio.adapters",
        base_currency="USD",
    ).publish_many(drafts)
    snapshot = ledger.replay()

    assert snapshot.option_positions == ()
    assert snapshot.futures_positions[0].average_price == Decimal("4010")
    assert snapshot.spot_positions[0].instrument_id == "UNDERLYING"
    assert snapshot.spot_positions[0].quantity == Decimal("100")
    assert dict((item.currency, item.amount) for item in snapshot.costs) == {
        "USD": Decimal("14")
    }

    unfilled = OptionFill(
        fill_id="option.unfilled.1",
        contract=contract,
        side=TransactionSide.BUY,
        requested_quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        price=None,
        fee=Decimal("0"),
        slippage_ticks=0,
        filled_at="2026-06-03T10:01:00+00:00",
        quote_hash=_HASH,
        status=FillStatus.UNFILLED,
        failure_code="capacity_exhausted",
    )
    attempt = adapt_option_fill(unfilled)[0]
    assert attempt.event_type is PortfolioEventType.EXECUTION_ATTEMPT
    assert attempt.cash_deltas == ()
    assert dict(attempt.metadata) == {
        "failure_code": "capacity_exhausted",
        "fill_status": "UNFILLED",
    }
    unfilled_context = execution_context_from_fill(
        unfilled,
        instrument_id=contract.contract_id,
        instrument_kind="OPTION",
        currency=contract.currency,
        reference_price=Decimal("5"),
        capacity_quantity=Decimal("0"),
    )
    assert unfilled_context.disposition is FillDisposition.UNFILLED


def _scenario_snapshot() -> tuple[UnifiedPortfolioLedger, object]:
    ledger = UnifiedPortfolioLedger.open(
        ledger_id="portfolio.scenario",
        base_currency="USD",
    ).publish_many(
        (
            funding_event(
                event_id="scenario.funding",
                occurred_at=_T0,
                cash_deltas=(CashDelta("USD", Decimal("20000")),),
            ),
            trade_event(
                event_id="scenario.spot",
                occurred_at="2026-06-01T10:00:00+00:00",
                asset_class=AssetClass.SPOT,
                instrument_id="AAPL",
                currency="USD",
                quantity_delta=Decimal("10"),
                price=Decimal("100"),
            ),
            trade_event(
                event_id="scenario.future",
                occurred_at="2026-06-01T10:01:00+00:00",
                asset_class=AssetClass.FUTURE,
                instrument_id="ESM6",
                currency="USD",
                quantity_delta=Decimal("2"),
                price=Decimal("4000"),
                multiplier=Decimal("5"),
            ),
            PortfolioEventDraft(
                event_id="scenario.margin",
                event_type=PortfolioEventType.MARGIN_REQUIREMENT,
                occurred_at="2026-06-01T10:02:00+00:00",
                currency="USD",
                instrument_id="ESM6",
                asset_class=AssetClass.FUTURE,
                margin_requirement=Decimal("1000"),
            ),
            trade_event(
                event_id="scenario.option",
                occurred_at="2026-06-01T10:03:00+00:00",
                asset_class=AssetClass.OPTION,
                instrument_id="AAPL.PUT",
                currency="USD",
                quantity_delta=Decimal("1"),
                price=Decimal("5"),
                multiplier=Decimal("100"),
            ),
        )
    )
    return ledger, ledger.replay()


@dataclass(frozen=True)
class _OptionRepricer:
    shocked_price: Decimal

    def reprice(
        self,
        position: object,
        *,
        market_state: object,
        shocked_state: ShockedMarketState,
    ) -> Decimal:
        assert shocked_state.volatility_shifts == (
            ("AAPL.vol.surface", Decimal("0.05")),
        )
        return self.shocked_price


def _market_state() -> MarketState:
    metadata = ObservationMetadata(
        observed_at="2026-06-01T11:00:00+00:00",
        knowledge_at="2026-06-01T11:00:00+00:00",
        source_hash=_HASH,
        calendar_id="XNYS",
        max_age_seconds=0,
    )
    return MarketState(
        state_id="market.state.scenario",
        valuation_at="2026-06-01T11:00:00+00:00",
        base_currency="USD",
        calendar_ids=("XNYS",),
        spots=(
            SpotQuote(
                instrument_id="AAPL",
                price=Decimal("120"),
                currency="USD",
                unit="USD_per_share",
                metadata=metadata,
            ),
        ),
        fx_quotes=(
            FXQuote(
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.1"),
                unit="USD_per_EUR",
                metadata=metadata,
            ),
        ),
    )


def test_joint_scenario_preserves_market_state_and_reprices_all_asset_classes() -> None:
    _, snapshot = _scenario_snapshot()
    state = _market_state()
    original_hash = state.state_hash()
    shock = JointMarketShock(
        scenario_id="joint.downside",
        price_returns=(
            ("AAPL", Decimal("-0.10")),
            ("ESM6", Decimal("-0.05")),
        ),
        volatility_shifts=(("AAPL.vol.surface", Decimal("0.05")),),
        rate_shifts=(("USD.30D", Decimal("0.01")),),
        liquidity_haircuts=(("AAPL", Decimal("0.02")),),
        liquidity_cost_multiplier=Decimal("3"),
        margin_multiplier=Decimal("1.5"),
        source_hashes=(_HASH_2,),
    )
    engine = JointScenarioEngine()

    result = engine.evaluate(
        snapshot,
        market_state=state,
        shock=shock,
        repricers={"AAPL.PUT": _OptionRepricer(Decimal("12"))},
        base_liquidation_costs={"AAPL": Decimal("4"), "ESM6": Decimal("6")},
    )

    by_id = {item.instrument_id: item for item in result.position_results}
    assert state.state_hash() == original_hash
    assert result.base_state_hash == original_hash
    assert result.shocked_state.parent_state_hash == original_hash
    assert result.shocked_state.content_hash == result.shocked_state_hash
    assert result.original_state_unchanged
    assert result.base_valuation.reconciled
    assert result.shocked_valuation.reconciled
    assert by_id["AAPL"].base_mark == Decimal("120")
    assert by_id["AAPL"].shocked_mark == Decimal("105.840")
    assert by_id["ESM6"].shocked_mark == Decimal("3800.00")
    assert by_id["AAPL.PUT"].shocked_mark == Decimal("12")
    assert by_id["AAPL.PUT"].repricer == "_OptionRepricer"
    assert result.liquidity_reserve == Decimal("30")
    assert result.shocked_valuation.available_capital < (
        result.base_valuation.available_capital
    )
    assert (
        result.content_hash
        == engine.evaluate(
            snapshot,
            market_state=state,
            shock=shock,
            repricers={"AAPL.PUT": _OptionRepricer(Decimal("12"))},
            base_liquidation_costs={"AAPL": Decimal("4"), "ESM6": Decimal("6")},
        ).content_hash
    )

    with pytest.raises(ScenarioError, match="option_repricer_required"):
        engine.evaluate(snapshot, market_state=state, shock=shock)


_SPOT_T0 = datetime(2026, 6, 1, 12, tzinfo=UTC)


def _spot_action(
    action_type: CorporateActionType,
    *,
    action_id: str,
    instrument_id: str = "SPOT.OLD",
    effective_day: int,
    ratio: str = "1",
    cash_per_share: str = "0",
    tax_rate: str = "0",
    replacement: str | None = None,
    child: str | None = None,
    child_basis: str = "0",
) -> CorporateAction:
    effective_at = _SPOT_T0 + timedelta(days=effective_day)
    dividend = action_type in {
        CorporateActionType.CASH_DIVIDEND,
        CorporateActionType.SPECIAL_DIVIDEND,
    }
    cash_exit = action_type in {
        CorporateActionType.LIQUIDATION,
        CorporateActionType.TENDER_OFFER,
    }
    return CorporateAction(
        action_id=action_id,
        revision=1,
        action_type=action_type,
        instrument_id=instrument_id,
        announced_at=_SPOT_T0,
        known_at=_SPOT_T0,
        record_at=_SPOT_T0 + timedelta(days=1) if dividend else None,
        ex_at=_SPOT_T0 + timedelta(days=1) if dividend else None,
        payment_at=effective_at if dividend else None,
        effective_at=effective_at,
        source_id="source.exchange",
        source_record_hash=_HASH_2,
        currency="USD" if dividend or cash_exit else None,
        cash_per_share=Decimal(cash_per_share),
        ratio=Decimal(ratio),
        tax_rate=Decimal(tax_rate),
        replacement_instrument_id=replacement,
        child_instrument_id=child,
        child_cost_basis_fraction=Decimal(child_basis),
    )


def test_spot_corporate_actions_borrow_and_income_reconcile_end_to_end() -> None:
    book = SpotBook(
        positions=(
            SpotPosition(
                instrument_id="SPOT.OLD",
                quantity=Decimal("100"),
                total_cost_basis=Decimal("8000"),
                currency="USD",
            ),
        ),
        cash=(SpotCashBalance(currency="USD", amount=Decimal("2000")),),
    )
    ledger = UnifiedPortfolioLedger.open(
        ledger_id="portfolio.spot.cf05",
        base_currency="USD",
    ).publish_many(
        (
            funding_event(
                event_id="spot.funding",
                occurred_at="2026-06-01T09:00:00+00:00",
                cash_deltas=(CashDelta("USD", Decimal("10000")),),
            ),
            trade_event(
                event_id="spot.long.open",
                occurred_at="2026-06-01T10:00:00+00:00",
                asset_class=AssetClass.SPOT,
                instrument_id="SPOT.OLD",
                currency="USD",
                quantity_delta=Decimal("100"),
                price=Decimal("80"),
            ),
            trade_event(
                event_id="spot.short.open",
                occurred_at="2026-06-01T11:00:00+00:00",
                asset_class=AssetClass.SPOT,
                instrument_id="SPOT.SHORT",
                currency="USD",
                quantity_delta=Decimal("-10"),
                price=Decimal("100"),
            ),
        )
    )

    split = _spot_action(
        CorporateActionType.SPLIT,
        action_id="action.split",
        effective_day=1,
        ratio="2",
    )
    split_application = apply_corporate_action(
        book,
        split,
        applied_at=split.effective_at,
    )
    ledger = ledger.publish_many(adapt_corporate_action_application(split_application))
    book = split_application.book_after
    split_position = ledger.replay().spot_positions
    split_by_id = {item.instrument_id: item for item in split_position}
    assert split_by_id["SPOT.OLD"].quantity == Decimal("200")
    assert split_by_id["SPOT.OLD"].average_price == Decimal("40")
    assert split_by_id["SPOT.OLD"].mark_price == Decimal("40")

    dividend = _spot_action(
        CorporateActionType.CASH_DIVIDEND,
        action_id="action.dividend.long",
        effective_day=2,
        cash_per_share="2",
        tax_rate="0.15",
    )
    dividend_application = apply_corporate_action(
        book,
        dividend,
        applied_at=dividend.effective_at,
        entitlement_book=book,
    )
    dividend_events = adapt_corporate_action_application(dividend_application)
    assert [item.event_type for item in dividend_events] == [
        PortfolioEventType.DIVIDEND_INCOME,
        PortfolioEventType.TAX,
    ]
    assert sum(
        (delta.amount for event in dividend_events for delta in event.cash_deltas),
        start=Decimal("0"),
    ) == Decimal("340")
    assert all(
        dividend.content_hash in event.source_hashes for event in dividend_events
    )
    ledger = ledger.publish_many(dividend_events)
    book = dividend_application.book_after

    short_book = SpotBook(
        positions=(
            SpotPosition(
                instrument_id="SPOT.SHORT",
                quantity=Decimal("-10"),
                total_cost_basis=Decimal("1000"),
                currency="USD",
            ),
        ),
        cash=(SpotCashBalance(currency="USD", amount=Decimal("1000")),),
    )
    short_dividend = _spot_action(
        CorporateActionType.CASH_DIVIDEND,
        action_id="action.dividend.short",
        instrument_id="SPOT.SHORT",
        effective_day=2,
        cash_per_share="2",
    )
    short_application = apply_corporate_action(
        short_book,
        short_dividend,
        applied_at=short_dividend.effective_at,
        entitlement_book=short_book,
    )
    short_events = adapt_corporate_action_application(short_application)
    assert len(short_events) == 1
    assert short_events[0].event_type is PortfolioEventType.SHORT_DIVIDEND_COMPENSATION
    ledger = ledger.publish_many(short_events)

    spin_off = _spot_action(
        CorporateActionType.SPIN_OFF,
        action_id="action.spin",
        effective_day=3,
        ratio="0.5",
        child="SPOT.CHILD",
        child_basis="0.20",
    )
    spin_application = apply_corporate_action(
        book,
        spin_off,
        applied_at=spin_off.effective_at,
    )
    ledger = ledger.publish_many(adapt_corporate_action_application(spin_application))
    book = spin_application.book_after
    spun = {item.instrument_id: item for item in ledger.replay().spot_positions}
    assert spun["SPOT.OLD"].quantity == Decimal("200")
    assert spun["SPOT.OLD"].average_price == Decimal("32")
    assert spun["SPOT.OLD"].mark_price == Decimal("32")
    assert spun["SPOT.CHILD"].quantity == Decimal("100")
    assert spun["SPOT.CHILD"].average_price == Decimal("16")
    assert spun["SPOT.CHILD"].mark_price == Decimal("16")

    merger = _spot_action(
        CorporateActionType.MERGER,
        action_id="action.merger",
        effective_day=4,
        ratio="1.25",
        replacement="SPOT.NEW",
    )
    merger_application = apply_corporate_action(
        book,
        merger,
        applied_at=merger.effective_at,
    )
    merger_events = adapt_corporate_action_application(merger_application)
    assert {item.event_type for item in merger_events} == {
        PortfolioEventType.POSITION_TRANSFORMATION,
        PortfolioEventType.REPLACEMENT_DELIVERY,
    }
    assert all(merger.content_hash in item.source_hashes for item in merger_events)
    ledger = ledger.publish_many(merger_events)
    book = merger_application.book_after
    merged = {item.instrument_id: item for item in ledger.replay().spot_positions}
    assert "SPOT.OLD" not in merged
    assert merged["SPOT.NEW"].quantity == Decimal("250")
    assert merged["SPOT.NEW"].average_price == Decimal("25.6")
    assert merged["SPOT.NEW"].mark_price == Decimal("25.6")

    liquidation = _spot_action(
        CorporateActionType.LIQUIDATION,
        action_id="action.liquidation",
        instrument_id="SPOT.NEW",
        effective_day=5,
        cash_per_share="30",
        tax_rate="0.10",
    )
    liquidation_application = apply_corporate_action(
        book,
        liquidation,
        applied_at=liquidation.effective_at,
    )
    liquidation_events = adapt_corporate_action_application(liquidation_application)
    assert [item.event_type for item in liquidation_events] == [
        PortfolioEventType.TERMINAL_SETTLEMENT,
        PortfolioEventType.TAX,
    ]
    assert liquidation_events[0].realized_pnl == Decimal("1100")
    ledger = ledger.publish_many(liquidation_events)

    ledger = ledger.publish(
        collateral_income_event(
            event_id="collateral.interest",
            occurred_at="2026-06-07T12:00:00+00:00",
            currency="USD",
            amount=Decimal("10"),
            source_hashes=(_HASH,),
        )
    )
    borrow_snapshot = BorrowSnapshot(
        snapshot_id="borrow.snapshot.base",
        scenario=BorrowScenario.BASE,
        instrument_id="SPOT.SHORT",
        observed_at=_SPOT_T0,
        known_at=_SPOT_T0,
        effective_from=_SPOT_T0,
        effective_to=None,
        borrowable=True,
        available_quantity=Decimal("100"),
        annual_fee_rate=Decimal("0.365"),
        recall_probability=Decimal("0.01"),
        short_sale_ban=False,
        uptick_restriction=False,
        trade_halted=False,
        hard_to_borrow=False,
        maximum_holding_days=30,
        source_hash=_HASH,
    )
    borrow_posting = accrue_borrow_cost(
        short_book.positions[0],
        borrow_snapshot,
        price=Decimal("100"),
        elapsed_days=Decimal("10"),
        occurred_at=_SPOT_T0 + timedelta(days=7),
        knowledge_at=_SPOT_T0 + timedelta(days=7),
    )
    assert borrow_posting.cash_delta == Decimal("-10")
    borrow_events = adapt_spot_posting(borrow_posting)
    assert borrow_events[0].event_type is PortfolioEventType.BORROW_COST
    assert borrow_snapshot.content_hash in borrow_events[0].source_hashes
    ledger = ledger.publish_many(borrow_events)

    final = ledger.replay()
    positions = {item.instrument_id: item for item in final.spot_positions}
    assert set(positions) == {"SPOT.CHILD", "SPOT.SHORT"}
    assert positions["SPOT.CHILD"].quantity == Decimal("100")
    assert positions["SPOT.CHILD"].average_price == Decimal("16")
    assert positions["SPOT.SHORT"].quantity == Decimal("-10")
    assert dict((item.currency, item.amount) for item in final.cash) == {
        "USD": Decimal("10710")
    }
    assert final.income == (
        type(final.income[0])(currency="USD", amount=Decimal("390")),
    )
    assert final.realized_pnl == (
        type(final.realized_pnl[0])(currency="USD", amount=Decimal("1100")),
    )
    assert final.costs == (type(final.costs[0])(currency="USD", amount=Decimal("180")),)

    valuation = final.valuation(fx_rates={"USD": Decimal("1")})
    assert valuation.nav == Decimal("11310")
    assert valuation.external_cash_flow == Decimal("10000")
    assert valuation.realized_pnl == Decimal("1100")
    assert valuation.unrealized_pnl == Decimal("0")
    assert valuation.income == Decimal("390")
    assert valuation.costs == Decimal("180")
    assert valuation.economic_pnl == Decimal("1310")
    assert valuation.fx_translation_pnl == Decimal("0")
    assert valuation.reconciled


def test_fx_attribution_is_independent_and_cannot_hide_reconciliation_error() -> None:
    ledger = UnifiedPortfolioLedger.open(
        ledger_id="ledger.fx-independent",
        base_currency="USD",
    )
    ledger = ledger.publish_many(
        (
            funding_event(
                event_id="funding.usd",
                occurred_at="2025-01-02T00:00:00+00:00",
                cash_deltas=(CashDelta("USD", Decimal("100")),),
            ),
            PortfolioEventDraft(
                event_id="fx.usd-eur",
                event_type=PortfolioEventType.FX_CONVERSION,
                occurred_at="2025-01-02T00:01:00+00:00",
                cash_deltas=(
                    CashDelta("USD", Decimal("-100")),
                    CashDelta("EUR", Decimal("100")),
                ),
                source_hashes=(_HASH,),
            ),
        )
    )
    snapshot = ledger.replay()

    unexplained = snapshot.valuation(fx_rates={"EUR": Decimal("1.10")})
    explained = snapshot.valuation(
        fx_rates={"EUR": Decimal("1.10")},
        fx_translation_pnl=Decimal("10"),
    )

    assert unexplained.economic_pnl == Decimal("10.00")
    assert unexplained.fx_translation_pnl == Decimal("0")
    assert unexplained.reconciliation_error == Decimal("10.00")
    assert not unexplained.reconciled
    assert explained.fx_translation_pnl == Decimal("10")
    assert explained.reconciliation_error == Decimal("0.00")
    assert explained.reconciled


def test_external_eur_principal_uses_event_time_fx_not_current_valuation_fx() -> None:
    occurred_at = "2025-01-01T00:00:00Z"
    ledger = UnifiedPortfolioLedger.open(
        ledger_id="ledger.external-flow-event-fx",
        base_currency="USD",
    ).publish(
        funding_event(
            event_id="funding.eur",
            occurred_at=occurred_at,
            cash_deltas=(CashDelta("EUR", Decimal("100")),),
            conversion_evidence=_eur_flow_evidence(
                occurred_at,
                rate=Decimal("1.10"),
            ),
        )
    )
    snapshot = ledger.replay()

    unexplained = snapshot.valuation(fx_rates={"EUR": Decimal("1.20")})
    explained = snapshot.valuation(
        fx_rates={"EUR": Decimal("1.20")},
        fx_translation_pnl=Decimal("10"),
    )

    assert snapshot.external_cash_flow_base == Decimal("110.00")
    assert unexplained.nav == Decimal("120.00")
    assert unexplained.external_cash_flow == Decimal("110.00")
    assert unexplained.economic_pnl == Decimal("10.00")
    assert unexplained.reconciliation_error == Decimal("10.00")
    assert explained.attributed_pnl == Decimal("10")
    assert explained.reconciliation_error == Decimal("0.00")


def test_nonbase_funding_without_event_time_conversion_evidence_fails_closed() -> None:
    ledger = UnifiedPortfolioLedger.open(
        ledger_id="ledger.external-flow-missing-fx",
        base_currency="USD",
    )

    with pytest.raises(
        PortfolioAccountingError,
        match="funding_event_conversion_evidence_incomplete",
    ):
        ledger.publish(
            funding_event(
                event_id="funding.eur.missing-fx",
                occurred_at="2025-01-01T00:00:00Z",
                cash_deltas=(CashDelta("EUR", Decimal("100")),),
            )
        )
