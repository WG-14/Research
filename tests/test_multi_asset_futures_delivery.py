from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.futures_delivery import (
    CollateralAssetKind,
    CollateralEligibility,
    CollateralHolding,
    ContinuousSeriesManifest,
    ContractSelectionPolicy,
    DeliverableBasket,
    DeliverableGrade,
    DeliveryPolicy,
    FuturesContractMasterHistory,
    FuturesContractMasterVersion,
    FuturesDeliveryError,
    FuturesLifecycleEventType,
    FuturesMarginPolicyVersion,
    FuturesSelectionCandidate,
    FuturesSettlementMode,
    FuturesTermsMetadata,
    RollAdjustmentMethod,
    RollYieldPolicy,
    evaluate_margin_waterfall,
    select_actual_contract,
    select_cheapest_to_deliver,
    settle_futures_position,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    CashDelta,
    PortfolioEventType,
    UnifiedPortfolioLedger,
    adapt_futures_lifecycle_posting,
    adapt_futures_margin_waterfall,
    funding_event,
    trade_event,
)


def _hash(value: str) -> str:
    return sha256_prefixed(value, label="futures-delivery-test")


def _metadata(
    *,
    valid_from: str = "2025-01-01T00:00:00+00:00",
    knowledge_at: str = "2024-12-01T00:00:00+00:00",
) -> FuturesTermsMetadata:
    return FuturesTermsMetadata(
        valid_from=valid_from,
        valid_to=None,
        knowledge_at=knowledge_at,
        source_id="exchange.reviewed.rules",
        source_version="v1",
        source_hash=_hash(f"terms:{valid_from}:{knowledge_at}"),
        policy_version="v1",
    )


def _physical_contract(
    *,
    contract_id: str = "future.usbond.202603",
    last_trade_at: str = "2026-03-20T16:00:00+00:00",
) -> FuturesContractMasterVersion:
    return FuturesContractMasterVersion(
        record_id=f"master.{contract_id}",
        version=1,
        contract_id=contract_id,
        root_id="root.usbond",
        economic_underlying_id="underlying.us.treasury.basket",
        exchange_mic="XCBT",
        contract_month="2026-03",
        listed_at="2025-06-01T00:00:00+00:00",
        last_trade_at=last_trade_at,
        first_notice_at="2026-03-10T00:00:00+00:00",
        final_settlement_at="2026-03-21T16:00:00+00:00",
        delivery_start_at="2026-03-21T16:00:00+00:00",
        delivery_end_at="2026-03-31T16:00:00+00:00",
        contract_multiplier=Decimal("1000"),
        contract_unit="bond_point",
        minimum_tick=Decimal("0.01"),
        tick_value=Decimal("10"),
        trading_currency="USD",
        settlement_currency="USD",
        settlement_mode=FuturesSettlementMode.PHYSICAL,
        settlement_formula_id="formula.cbot.invoice.v1",
        calendar_id="cal_xcbt_v1",
        session_id="session_xcbt_regular",
        daily_price_limit=Decimal("5"),
        margin_policy_id="margin.xcbt.bond.v1",
        deliverable_basket_id="basket.usbond.202603",
        metadata=_metadata(),
    )


def _cash_contract(
    contract_id: str = "future.index.202603",
) -> FuturesContractMasterVersion:
    return FuturesContractMasterVersion(
        record_id=f"master.{contract_id}",
        version=1,
        contract_id=contract_id,
        root_id="root.index",
        economic_underlying_id="underlying.index",
        exchange_mic="XCME",
        contract_month="2026-03",
        listed_at="2025-06-01T00:00:00+00:00",
        last_trade_at="2026-03-20T16:00:00+00:00",
        first_notice_at=None,
        final_settlement_at="2026-03-21T16:00:00+00:00",
        delivery_start_at=None,
        delivery_end_at=None,
        contract_multiplier=Decimal("50"),
        contract_unit="index_point",
        minimum_tick=Decimal("0.25"),
        tick_value=Decimal("12.5"),
        trading_currency="USD",
        settlement_currency="USD",
        settlement_mode=FuturesSettlementMode.CASH,
        settlement_formula_id="formula.index.soq.v1",
        calendar_id="cal_xcme_v1",
        session_id="session_xcme_regular",
        daily_price_limit=None,
        margin_policy_id="margin.xcme.index.v1",
        deliverable_basket_id=None,
        metadata=_metadata(),
    )


def _basket() -> DeliverableBasket:
    return DeliverableBasket(
        basket_id="basket.usbond.202603",
        version="v1",
        contract_root_id="root.usbond",
        delivery_unit=Decimal("1000"),
        grades=(
            DeliverableGrade(
                grade_id="grade.treasury.a",
                instrument_id="bond.treasury.a",
                location_id="location.fedwire",
                clean_cash_price=Decimal("108"),
                accrued_interest=Decimal("1"),
                conversion_factor=Decimal("1.05"),
                quality_adjustment=Decimal("0"),
                location_adjustment=Decimal("0"),
                delivery_cost=Decimal("20"),
                source_hash=_hash("grade-a"),
            ),
            DeliverableGrade(
                grade_id="grade.treasury.b",
                instrument_id="bond.treasury.b",
                location_id="location.fedwire",
                clean_cash_price=Decimal("104"),
                accrued_interest=Decimal("0.5"),
                conversion_factor=Decimal("1"),
                quality_adjustment=Decimal("0.1"),
                location_adjustment=Decimal("0"),
                delivery_cost=Decimal("10"),
                source_hash=_hash("grade-b"),
            ),
        ),
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to="2026-04-01T00:00:00+00:00",
        knowledge_at="2025-12-01T00:00:00+00:00",
        source_hash=_hash("basket"),
    )


def test_continuous_manifest_is_complete_and_never_tradable() -> None:
    manifest = ContinuousSeriesManifest(
        series_id="continuous.root.index",
        root_id="root.index",
        source_contract_ids=(
            "future.index.202603",
            "future.index.202606",
        ),
        roll_rule_id="roll.volume_oi_notice.v1",
        roll_window_days=5,
        liquidity_rule_id="liquidity.volume_oi_spread.v1",
        delivery_avoidance_rule_id="delivery.before_notice.v1",
        adjustment_method=RollAdjustmentMethod.DIFFERENCE,
        adjustment_values=(
            ("future.index.202603", Decimal("0")),
            ("future.index.202606", Decimal("-5")),
        ),
        builder_version="builder.v2",
        source_snapshot_hashes=tuple(
            sorted((_hash("chain-1"), _hash("chain-2")))
        ),
        generated_series_hash=_hash("series"),
        generated_at="2026-01-02T00:00:00+00:00",
    )
    assert (
        manifest.require_actual_contract("future.index.202603")
        == "future.index.202603"
    )
    with pytest.raises(FuturesDeliveryError, match="not_tradable"):
        manifest.require_actual_contract(manifest.series_id)
    with pytest.raises(FuturesDeliveryError, match="source_hashes_not_canonical"):
        replace(
            manifest,
            source_snapshot_hashes=(
                manifest.source_snapshot_hashes[0],
                manifest.source_snapshot_hashes[0],
            ),
        )


def test_contract_selection_uses_notice_trade_liquidity_oi_volume_and_spread() -> None:
    near = _physical_contract()
    far = replace(
        near,
        record_id="master.future.usbond.202606",
        contract_id="future.usbond.202606",
        contract_month="2026-06",
        last_trade_at="2026-06-20T16:00:00+00:00",
        first_notice_at="2026-06-10T00:00:00+00:00",
        final_settlement_at="2026-06-21T16:00:00+00:00",
        delivery_start_at="2026-06-21T16:00:00+00:00",
        delivery_end_at="2026-06-30T16:00:00+00:00",
        deliverable_basket_id="basket.usbond.202606",
    )
    candidates = (
        FuturesSelectionCandidate(
            contract=near,
            observed_at="2026-03-09T15:00:00+00:00",
            knowledge_at="2026-03-09T15:00:01+00:00",
            bid=Decimal("110"),
            ask=Decimal("110.02"),
            settlement_price=Decimal("110.01"),
            volume=Decimal("10000"),
            open_interest=Decimal("50000"),
            source_quote_hash=_hash("near-quote"),
        ),
        FuturesSelectionCandidate(
            contract=far,
            observed_at="2026-03-09T15:00:00+00:00",
            knowledge_at="2026-03-09T15:00:01+00:00",
            bid=Decimal("111"),
            ask=Decimal("111.01"),
            settlement_price=Decimal("111"),
            volume=Decimal("8000"),
            open_interest=Decimal("40000"),
            source_quote_hash=_hash("far-quote"),
        ),
    )
    policy = ContractSelectionPolicy(
        policy_id="select.actual.v1",
        version="v1",
        minimum_days_to_notice=5,
        minimum_days_to_last_trade=3,
        minimum_volume=Decimal("1000"),
        minimum_open_interest=Decimal("5000"),
        maximum_spread=Decimal("0.05"),
    )
    receipt = select_actual_contract(
        candidates,
        root_id="root.usbond",
        decision_at="2026-03-09T15:00:02+00:00",
        knowledge_at="2026-03-09T15:00:01+00:00",
        policy=policy,
    )
    assert receipt.selected_contract_id == "future.usbond.202606"
    assert receipt.rejected == (
        ("future.usbond.202603", ("FIRST_NOTICE",)),
    )
    with pytest.raises(FuturesDeliveryError, match="future_knowledge"):
        select_actual_contract(
            candidates,
            root_id="root.usbond",
            decision_at="2026-03-09T15:00:00+00:00",
            knowledge_at="2026-03-09T15:00:01+00:00",
            policy=policy,
        )


def test_contract_multiplier_terms_revision_is_bitemporal_and_hash_chained() -> None:
    first = _cash_contract()
    revision = replace(
        first,
        version=2,
        contract_multiplier=Decimal("25"),
        tick_value=Decimal("6.25"),
        metadata=_metadata(
            valid_from="2026-01-01T00:00:00+00:00",
            knowledge_at="2026-02-01T00:00:00+00:00",
        ),
        supersedes_hash=first.contract_hash(),
    )
    history = FuturesContractMasterHistory((first, revision))
    assert history.resolve(
        first.contract_id,
        valid_at="2026-02-10T00:00:00+00:00",
        knowledge_at="2026-01-15T00:00:00+00:00",
    ).contract_multiplier == Decimal("50")
    assert history.resolve(
        first.contract_id,
        valid_at="2026-02-10T00:00:00+00:00",
        knowledge_at="2026-02-10T00:00:00+00:00",
    ).contract_multiplier == Decimal("25")
    with pytest.raises(FuturesDeliveryError, match="chain_broken"):
        FuturesContractMasterHistory(
            (first, replace(revision, supersedes_hash=_hash("forged")))
        )


def test_ctd_invoice_conversion_factor_and_physical_delivery_reconcile() -> None:
    contract = _physical_contract()
    basket = _basket()
    ctd = select_cheapest_to_deliver(
        contract,
        basket,
        futures_settlement_price=Decimal("105"),
    )
    assert ctd.selected_grade_id == "grade.treasury.a"
    assert len(ctd.comparisons) == 2
    selected = ctd.comparisons[0]
    assert selected.invoice_amount == Decimal("111250")
    assert selected.net_basis == Decimal("-2230")
    postings = settle_futures_position(
        contract,
        quantity=Decimal("2"),
        prior_settlement_price=Decimal("104"),
        final_settlement_price=Decimal("105"),
        occurred_at="2026-03-21T16:00:00+00:00",
        policy=DeliveryPolicy(
            policy_id="delivery.physical.v1",
            version="v1",
            physical_delivery_enabled=True,
            close_before_notice_days=5,
            default_closeout_penalty_rate=Decimal("0.02"),
        ),
        ctd=ctd,
    )
    assert postings[0].event_type is FuturesLifecycleEventType.DELIVERY
    assert postings[0].contract_quantity_delta == Decimal("-2")
    assert postings[0].delivered_instrument_id == "bond.treasury.a"
    assert postings[0].delivered_quantity_delta == Decimal("2000")
    assert postings[0].cash_delta == Decimal("-220500")

    with pytest.raises(FuturesDeliveryError, match="binding_mismatch"):
        select_cheapest_to_deliver(
            replace(contract, deliverable_basket_id="basket.forged"),
            basket,
            futures_settlement_price=Decimal("105"),
        )


def test_cash_settlement_default_and_ctd_nonapplicability_are_distinct() -> None:
    contract = _cash_contract()
    policy = DeliveryPolicy(
        policy_id="delivery.cash.v1",
        version="v1",
        physical_delivery_enabled=False,
        close_before_notice_days=0,
        default_closeout_penalty_rate=Decimal("0.01"),
    )
    settled = settle_futures_position(
        contract,
        quantity=Decimal("-3"),
        prior_settlement_price=Decimal("5000"),
        final_settlement_price=Decimal("4990"),
        occurred_at="2026-03-21T16:00:00+00:00",
        policy=policy,
    )
    assert settled[0].event_type is FuturesLifecycleEventType.CASH_SETTLEMENT
    assert settled[0].cash_delta == Decimal("1500")
    defaulted = settle_futures_position(
        contract,
        quantity=Decimal("1"),
        prior_settlement_price=Decimal("5000"),
        final_settlement_price=Decimal("4990"),
        occurred_at="2026-03-21T16:00:00+00:00",
        policy=policy,
        defaulted=True,
    )
    assert defaulted[0].event_type is FuturesLifecycleEventType.DEFAULT
    assert defaulted[0].cash_delta == Decimal("-2995")
    with pytest.raises(FuturesDeliveryError, match="not_applicable"):
        select_cheapest_to_deliver(
            contract,
            _basket(),
            futures_settlement_price=Decimal("105"),
        )


def _margin_policy(*, funding: bool) -> FuturesMarginPolicyVersion:
    return FuturesMarginPolicyVersion(
        policy_id="margin.xcme.index.v1",
        version=1,
        exchange_mic="XCME",
        currency="USD",
        initial_per_contract=Decimal("10000"),
        maintenance_per_contract=Decimal("8000"),
        variation_frequency="DAILY",
        collateral=(
            CollateralEligibility(
                asset_id="collateral.cash.usd",
                kind=CollateralAssetKind.CASH,
                currency="USD",
                haircut=Decimal("0"),
                concentration_limit=Decimal("1"),
                source_hash=_hash("cash-rule"),
            ),
            CollateralEligibility(
                asset_id="collateral.treasury",
                kind=CollateralAssetKind.GOVERNMENT_SECURITY,
                currency="USD",
                haircut=Decimal("0.1"),
                concentration_limit=Decimal("1"),
                source_hash=_hash("treasury-rule"),
            ),
        ),
        collateral_waterfall=(
            "collateral.cash.usd",
            "collateral.treasury",
        ),
        collateral_yield_rate=Decimal("0.0365"),
        spread_offset_rate=Decimal("0.5"),
        additional_funding_allowed=funding,
        metadata=_metadata(),
    )


def test_versioned_margin_collateral_waterfall_yield_offset_and_default() -> None:
    result = evaluate_margin_waterfall(
        _margin_policy(funding=True),
        outright_contracts=Decimal("1"),
        spread_contract_pairs=Decimal("1"),
        variation_margin=Decimal("-1000"),
        collateral_holdings=(
            CollateralHolding(
                asset_id="collateral.cash.usd",
                market_value=Decimal("5000"),
            ),
            CollateralHolding(
                asset_id="collateral.treasury",
                market_value=Decimal("10000"),
            ),
        ),
        elapsed_days=Decimal("10"),
    )
    assert result.gross_initial_requirement == Decimal("30000")
    assert result.spread_offset == Decimal("10000")
    assert result.net_initial_requirement == Decimal("20000")
    assert result.eligible_collateral_value == Decimal("14000")
    assert result.collateral_income == Decimal("14")
    assert result.margin_call == Decimal("6986")
    assert result.additional_funding == Decimal("6986")
    assert result.default_amount == Decimal("0")
    no_funding = evaluate_margin_waterfall(
        _margin_policy(funding=False),
        outright_contracts=Decimal("1"),
        spread_contract_pairs=Decimal("1"),
        variation_margin=Decimal("-1000"),
        collateral_holdings=(
            CollateralHolding(
                asset_id="collateral.cash.usd",
                market_value=Decimal("5000"),
            ),
            CollateralHolding(
                asset_id="collateral.treasury",
                market_value=Decimal("10000"),
            ),
        ),
        elapsed_days=Decimal("10"),
    )
    assert no_funding.default_amount == Decimal("6986")
    with pytest.raises(FuturesDeliveryError, match="ineligible_collateral"):
        evaluate_margin_waterfall(
            _margin_policy(funding=True),
            outright_contracts=Decimal("1"),
            spread_contract_pairs=Decimal("0"),
            variation_margin=Decimal("0"),
            collateral_holdings=(
                CollateralHolding(
                    asset_id="collateral.forged",
                    market_value=Decimal("1"),
                ),
            ),
            elapsed_days=Decimal("1"),
        )


def test_common_ledger_cash_delivery_and_default_postings_reconcile() -> None:
    cash_contract = _cash_contract()
    cash_policy = DeliveryPolicy(
        policy_id="delivery.cash.ledger.v1",
        version="v1",
        physical_delivery_enabled=False,
        close_before_notice_days=0,
        default_closeout_penalty_rate=Decimal("0.01"),
    )
    cash_ledger = UnifiedPortfolioLedger.open(
        ledger_id="futures.delivery.cash-ledger",
        base_currency="USD",
    ).publish_many(
        (
            funding_event(
                event_id="cash-ledger.funding",
                occurred_at="2026-03-20T15:00:00+00:00",
                cash_deltas=(CashDelta("USD", Decimal("100000")),),
            ),
            trade_event(
                event_id="cash-ledger.short",
                occurred_at="2026-03-20T16:00:00+00:00",
                asset_class=AssetClass.FUTURE,
                instrument_id=cash_contract.contract_id,
                currency="USD",
                quantity_delta=Decimal("-3"),
                price=Decimal("5000"),
                multiplier=cash_contract.contract_multiplier,
            ),
        )
    )
    cash_posting = settle_futures_position(
        cash_contract,
        quantity=Decimal("-3"),
        prior_settlement_price=Decimal("5000"),
        final_settlement_price=Decimal("4990"),
        occurred_at="2026-03-21T16:00:00+00:00",
        policy=cash_policy,
    )[0]
    cash_drafts = adapt_futures_lifecycle_posting(
        cash_posting,
        ledger=cash_ledger,
        contract=cash_contract,
        policy=cash_policy,
    )
    assert [item.event_type for item in cash_drafts] == [
        PortfolioEventType.FUTURES_TRADE
    ]
    cash_snapshot = cash_ledger.publish_many(cash_drafts).replay()
    assert cash_snapshot.positions == ()
    cash_valuation = cash_snapshot.valuation(fx_rates={"USD": Decimal("1")})
    assert cash_valuation.nav == Decimal("101500")
    assert cash_valuation.realized_pnl == Decimal("1500")
    assert cash_valuation.reconciled

    default_ledger = UnifiedPortfolioLedger.open(
        ledger_id="futures.delivery.default-ledger",
        base_currency="USD",
    ).publish_many(
        (
            funding_event(
                event_id="default-ledger.funding",
                occurred_at="2026-03-20T15:00:00+00:00",
                cash_deltas=(CashDelta("USD", Decimal("100000")),),
            ),
            trade_event(
                event_id="default-ledger.long",
                occurred_at="2026-03-20T16:00:00+00:00",
                asset_class=AssetClass.FUTURE,
                instrument_id=cash_contract.contract_id,
                currency="USD",
                quantity_delta=Decimal("1"),
                price=Decimal("5000"),
                multiplier=cash_contract.contract_multiplier,
            ),
        )
    )
    default_posting = settle_futures_position(
        cash_contract,
        quantity=Decimal("1"),
        prior_settlement_price=Decimal("5000"),
        final_settlement_price=Decimal("4990"),
        occurred_at="2026-03-21T16:00:00+00:00",
        policy=cash_policy,
        defaulted=True,
    )[0]
    default_drafts = adapt_futures_lifecycle_posting(
        default_posting,
        ledger=default_ledger,
        contract=cash_contract,
        policy=cash_policy,
    )
    assert [item.event_type for item in default_drafts] == [
        PortfolioEventType.FUTURES_TRADE,
        PortfolioEventType.EXECUTION_COST,
        PortfolioEventType.DEFAULT,
        PortfolioEventType.FORCED_LIQUIDATION,
    ]
    default_snapshot = default_ledger.publish_many(default_drafts).replay()
    valuation = default_snapshot.valuation(fx_rates={"USD": Decimal("1")})
    assert default_snapshot.positions == ()
    assert valuation.nav == Decimal("97005")
    assert valuation.realized_pnl == Decimal("-500")
    assert valuation.costs == Decimal("2495")
    assert valuation.reconciled

    physical_contract = _physical_contract()
    physical_policy = DeliveryPolicy(
        policy_id="delivery.physical.ledger.v1",
        version="v1",
        physical_delivery_enabled=True,
        close_before_notice_days=5,
        default_closeout_penalty_rate=Decimal("0.02"),
    )
    ctd = select_cheapest_to_deliver(
        physical_contract,
        _basket(),
        futures_settlement_price=Decimal("105"),
    )
    physical_ledger = UnifiedPortfolioLedger.open(
        ledger_id="futures.delivery.physical-ledger",
        base_currency="USD",
    ).publish_many(
        (
            funding_event(
                event_id="physical-ledger.funding",
                occurred_at="2026-03-20T15:00:00+00:00",
                cash_deltas=(CashDelta("USD", Decimal("300000")),),
            ),
            trade_event(
                event_id="physical-ledger.long",
                occurred_at="2026-03-20T16:00:00+00:00",
                asset_class=AssetClass.FUTURE,
                instrument_id=physical_contract.contract_id,
                currency="USD",
                quantity_delta=Decimal("2"),
                price=Decimal("104"),
                multiplier=physical_contract.contract_multiplier,
            ),
        )
    )
    delivery_posting = settle_futures_position(
        physical_contract,
        quantity=Decimal("2"),
        prior_settlement_price=Decimal("104"),
        final_settlement_price=Decimal("105"),
        occurred_at="2026-03-21T16:00:00+00:00",
        policy=physical_policy,
        ctd=ctd,
    )[0]
    delivery_drafts = adapt_futures_lifecycle_posting(
        delivery_posting,
        ledger=physical_ledger,
        contract=physical_contract,
        policy=physical_policy,
        ctd=ctd,
    )
    assert [item.event_type for item in delivery_drafts] == [
        PortfolioEventType.FUTURES_TRADE,
        PortfolioEventType.SPOT_TRADE,
        PortfolioEventType.DELIVERY,
    ]
    delivered_snapshot = physical_ledger.publish_many(delivery_drafts).replay()
    assert len(delivered_snapshot.positions) == 1
    delivered = delivered_snapshot.positions[0]
    assert delivered.asset_class is AssetClass.SPOT
    assert delivered.instrument_id == "bond.treasury.a"
    assert delivered.quantity == Decimal("2000")
    assert delivered.average_price == Decimal("111.25")
    delivered_valuation = delivered_snapshot.valuation(
        fx_rates={"USD": Decimal("1")}
    )
    assert delivered_valuation.nav == Decimal("302000")
    assert delivered_valuation.realized_pnl == Decimal("2000")
    assert delivered_valuation.reconciled


def test_common_ledger_margin_waterfall_projects_cash_capital_and_audits() -> None:
    result = evaluate_margin_waterfall(
        _margin_policy(funding=True),
        outright_contracts=Decimal("1"),
        spread_contract_pairs=Decimal("1"),
        variation_margin=Decimal("-1000"),
        collateral_holdings=(
            CollateralHolding(
                asset_id="collateral.cash.usd",
                market_value=Decimal("5000"),
            ),
            CollateralHolding(
                asset_id="collateral.treasury",
                market_value=Decimal("10000"),
            ),
        ),
        elapsed_days=Decimal("10"),
    )
    ledger = UnifiedPortfolioLedger.open(
        ledger_id="futures.margin.common-ledger",
        base_currency="USD",
    ).publish(
        funding_event(
            event_id="margin-ledger.funding",
            occurred_at="2026-03-20T15:00:00+00:00",
            cash_deltas=(CashDelta("USD", Decimal("10000")),),
        )
    )
    drafts = adapt_futures_margin_waterfall(
        result,
        event_id_prefix="margin.waterfall",
        occurred_at="2026-03-21T16:00:00+00:00",
        currency="USD",
        contract_id="future.index.202603",
    )
    assert {item.event_type for item in drafts} == {
        PortfolioEventType.VARIATION_MARGIN,
        PortfolioEventType.COLLATERAL_INCOME,
        PortfolioEventType.FUNDING,
        PortfolioEventType.MARGIN_REQUIREMENT,
        PortfolioEventType.MARGIN_CALL,
        PortfolioEventType.COLLATERAL_WATERFALL,
    }
    snapshot = ledger.publish_many(drafts).replay()
    valuation = snapshot.valuation(fx_rates={"USD": Decimal("1")})
    assert snapshot.cash[0].amount == Decimal("16000")
    assert snapshot.external_cash_flow[0].amount == Decimal("16986")
    assert valuation.realized_pnl == Decimal("-1000")
    assert valuation.income == Decimal("14")
    assert valuation.available_capital == Decimal("-4000")
    assert valuation.reconciled
    assert all(
        result.result_hash() in event.source_hashes
        for event in ledger.publish_many(drafts).events[-len(drafts) :]
    )


def test_roll_yield_is_explicitly_not_cash_pnl() -> None:
    policy = RollYieldPolicy(policy_id="roll-yield.v1", version="v1")
    assert policy.calculate(
        expiring_price=Decimal("100"),
        replacement_price=Decimal("102"),
        days=5,
    ) == Decimal("-0.02")
    with pytest.raises(FuturesDeliveryError, match="separate_from_cash_pnl"):
        RollYieldPolicy(
            policy_id="roll-yield.bad",
            version="v1",
            cash_pnl_is_separate=False,
        )
