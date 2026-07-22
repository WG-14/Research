from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

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
    SettlementType,
)
from market_research.research.multi_asset.futures_path import (
    ContinuousPointProtocol,
    DeliverableTermsVersion,
    ExistingFuturesCostPolicyAdapter,
    ExpiryBucket,
    FUTURES_PATH_SCHEMA_VERSION,
    FuturesContractProtocol,
    FuturesPathError,
    FuturesReferenceHistory,
    ReferenceMetadata,
    RollPlanningPolicy,
    adapt_existing_futures_contract,
    adapt_existing_margin_policy,
    build_futures_curve_snapshot,
    plan_exposure_preserving_roll,
    reconcile_existing_futures_pnl,
    select_roll_target,
    trace_continuous_signal,
)


AS_OF = "2026-03-10T16:00:00Z"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _availability(at: str) -> AvailabilityTimes:
    return AvailabilityTimes(
        event_at=at,
        published_at=at,
        provider_received_at=at,
        system_received_at=at,
        processed_at=at,
    )


def _contract(
    contract_id: str,
    *,
    expiration: str,
    multiplier: str = "50",
    tick: str = "0.25",
    settlement_type: SettlementType = SettlementType.CASH_SETTLED,
    first_notice: str | None = None,
    knowledge_at: str = "2026-01-02T00:00:00Z",
) -> FuturesContract:
    notice = first_notice
    if settlement_type is SettlementType.PHYSICAL_SETTLED and notice is None:
        notice = expiration
    return FuturesContract(
        contract_id=contract_id,
        root_id="FUT.ROOT",
        listing_date="2026-01-01",
        first_trade_date="2026-01-02",
        last_trade_date=expiration,
        first_notice_date=notice,
        final_settlement_date=expiration,
        expiration_date=expiration,
        contract_multiplier=Decimal(multiplier),
        tick_size=Decimal(tick),
        settlement_type=settlement_type,
        spec_effective_at="2026-01-01T00:00:00Z",
        spec_version="v1",
        availability=_availability(knowledge_at),
    )


def _quote(
    contract: FuturesContract,
    price: str,
    *,
    at: str = AS_OF,
    sequence: int = 0,
) -> ContractQuote:
    value = Decimal(price)
    return ContractQuote(
        quote_id=f"quote.{contract.contract_id}.{sequence}",
        contract_id=contract.contract_id,
        root_id=contract.root_id,
        observed_at=at,
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
        availability=_availability(at),
        source_hash=HASH_A,
        bid_price=value - Decimal("0.25"),
        ask_price=value + Decimal("0.25"),
    )


def _margin_policy(version: str = "v1") -> MarginSimulationPolicy:
    return MarginSimulationPolicy(
        policy_id="margin.research",
        policy_version=version,
        initial_margin_per_contract=Decimal("5000"),
        maintenance_margin_per_contract=Decimal("4000"),
        collateral_fraction=Decimal("1"),
        margin_call_action=MarginCallAction.BLOCK_NEW_TRADES,
    )


def _cost_adapter() -> ExistingFuturesCostPolicyAdapter:
    return ExistingFuturesCostPolicyAdapter(
        FuturesCostPolicy(
            policy_id="cost.roll",
            policy_version="v1",
            commission_per_contract=Decimal("2"),
            execution_slippage_ticks=Decimal("1"),
            roll_slippage_ticks=Decimal("1"),
            spread_legging_ticks=Decimal("0"),
        )
    )


def _reference_metadata(
    *,
    knowledge_at: str,
    source_hash: str,
    source_version: str,
) -> ReferenceMetadata:
    return ReferenceMetadata(
        effective_from="2026-01-01T00:00:00Z",
        effective_to=None,
        knowledge_at=knowledge_at,
        source_id="reviewed.futures.master",
        source_version=source_version,
        source_hash=source_hash,
    )


def test_reference_history_uses_effective_and_knowledge_time_for_all_terms() -> None:
    contract = _contract(
        "FUT.PHYSICAL",
        expiration="2026-06-08",
        settlement_type=SettlementType.PHYSICAL_SETTLED,
        first_notice="2026-06-01",
    )
    assert isinstance(contract, FuturesContractProtocol)
    specification_v1 = adapt_existing_futures_contract(contract, quote_currency="USD")
    specification_v2 = replace(
        specification_v1,
        record_id="FUT.PHYSICAL.spec.v2",
        contract_multiplier=Decimal("25"),
        metadata=_reference_metadata(
            knowledge_at="2026-02-01T00:00:00Z",
            source_hash=HASH_B,
            source_version="v2",
        ),
    )
    margin_v1 = adapt_existing_margin_policy(
        _margin_policy(),
        contract_id=contract.contract_id,
        currency="USD",
        effective_from="2026-01-01T00:00:00Z",
        effective_to=None,
        knowledge_at="2026-01-02T00:00:00Z",
    )
    margin_v2 = replace(
        margin_v1,
        record_id="FUT.PHYSICAL.margin.v2",
        initial_margin_per_contract=Decimal("6000"),
        maintenance_margin_per_contract=Decimal("5000"),
        metadata=_reference_metadata(
            knowledge_at="2026-02-01T00:00:00Z",
            source_hash=HASH_C,
            source_version="v2",
        ),
    )
    deliverable_v1 = DeliverableTermsVersion(
        record_id="FUT.PHYSICAL.deliverable.v1",
        contract_id=contract.contract_id,
        grades=("grade.a",),
        delivery_locations=("location.one",),
        grade_differentials=(("grade.a", Decimal("0")),),
        metadata=_reference_metadata(
            knowledge_at="2026-01-02T00:00:00Z",
            source_hash=HASH_A,
            source_version="v1",
        ),
    )
    deliverable_v2 = DeliverableTermsVersion(
        record_id="FUT.PHYSICAL.deliverable.v2",
        contract_id=contract.contract_id,
        grades=("grade.a", "grade.b"),
        delivery_locations=("location.one", "location.two"),
        grade_differentials=(
            ("grade.a", Decimal("0")),
            ("grade.b", Decimal("-0.5")),
        ),
        metadata=_reference_metadata(
            knowledge_at="2026-02-01T00:00:00Z",
            source_hash=HASH_B,
            source_version="v2",
        ),
    )
    history = FuturesReferenceHistory(
        history_id="history.FUT.PHYSICAL",
        contract_id=contract.contract_id,
        specifications=(specification_v2, specification_v1),
        margins=(margin_v2, margin_v1),
        deliverable_terms=(deliverable_v2, deliverable_v1),
    )

    january = history.as_of(
        valid_at="2026-03-01T00:00:00Z",
        known_at="2026-01-15T00:00:00Z",
    )
    february = history.as_of(
        valid_at="2026-03-01T00:00:00Z",
        known_at="2026-02-15T00:00:00Z",
    )

    assert january.specification.contract_multiplier == Decimal("50")
    assert january.margin.initial_margin_per_contract == Decimal("5000")
    assert january.deliverable_terms is not None
    assert january.deliverable_terms.grades == ("grade.a",)
    assert february.specification.contract_multiplier == Decimal("25")
    assert february.margin.initial_margin_per_contract == Decimal("6000")
    assert february.deliverable_terms is not None
    assert february.deliverable_terms.delivery_locations == (
        "location.one",
        "location.two",
    )
    assert february.history_hash == history.content_hash
    assert february.content_hash != january.content_hash
    assert FUTURES_PATH_SCHEMA_VERSION == 2
    assert february.as_dict()["schema_version"] == 2

    with pytest.raises(
        FuturesPathError,
        match="contract_specification_not_available_point_in_time",
    ):
        history.as_of(
            valid_at="2026-03-01T00:00:00Z",
            known_at="2025-12-31T00:00:00Z",
        )


def test_reference_history_rejects_ambiguous_same_knowledge_versions() -> None:
    contract = _contract("FUT.CASH", expiration="2026-06-08")
    first = adapt_existing_futures_contract(contract, quote_currency="USD")
    duplicate = replace(first, record_id="FUT.CASH.spec.duplicate")
    margin = adapt_existing_margin_policy(
        _margin_policy(),
        contract_id=contract.contract_id,
        currency="USD",
        effective_from="2026-01-01T00:00:00Z",
        effective_to=None,
        knowledge_at="2026-01-02T00:00:00Z",
    )

    with pytest.raises(
        FuturesPathError,
        match="contract_specification_ambiguous_knowledge_version",
    ):
        FuturesReferenceHistory(
            history_id="history.FUT.CASH",
            contract_id=contract.contract_id,
            specifications=(first, duplicate),
            margins=(margin,),
        )


def test_curve_snapshot_matches_existing_basis_curve_and_expiry_semantics() -> None:
    front = _contract("FUT.202604", expiration="2026-04-09")
    middle = _contract("FUT.202605", expiration="2026-05-09")
    back = _contract("FUT.202606", expiration="2026-06-08")
    quotes = (
        _quote(front, "101"),
        _quote(middle, "103"),
        _quote(back, "106"),
    )
    chain = ContractChainSnapshot(
        snapshot_id="chain.curve",
        root_id="FUT.ROOT",
        observed_at=AS_OF,
        availability=_availability(AS_OF),
        contracts=(front, middle, back),
        quotes=quotes,
        lifecycle_events=(),
        quality_results=(),
        source_manifest_hashes=(HASH_A,),
    )

    curve = build_futures_curve_snapshot(
        chain,
        snapshot_id="snapshot.curve",
        feature_version="v1",
        as_of=AS_OF,
        spot_price=Decimal("100"),
        spot_availability=_availability(AS_OF),
        spot_source_hash=HASH_B,
    )

    expected_slope = Decimal("2") / Decimal("101") * Decimal("365") / Decimal("30")
    assert curve.basis == Decimal("1")
    assert curve.basis_ratio == Decimal("0.01")
    assert curve.implied_annualized_carry == Decimal("0.01") * Decimal("365") / Decimal(
        "30"
    )
    assert curve.front_back_slope == expected_slope
    assert curve.annualized_roll_yield == -expected_slope
    assert curve.curvature == Decimal("1")
    assert [item.contract_id for item in curve.points] == [
        front.contract_id,
        middle.contract_id,
        back.contract_id,
    ]
    assert curve.expiry_buckets[0].bucket is ExpiryBucket.DAYS_0_30
    assert curve.expiry_buckets[1].bucket is ExpiryBucket.DAYS_31_90
    assert curve.chain_snapshot_hash == chain.content_hash
    assert curve.basis_feature_hash != curve.curve_feature_hash


def test_continuous_trace_is_signal_only_and_keeps_actual_contract_mapping() -> None:
    first = ContinuousFuturesPoint(
        point_id="continuous.point.1",
        series_id="continuous.FUT",
        root_id="FUT.ROOT",
        observed_at="2026-03-09T16:00:00Z",
        source_contract_id="FUT.202604",
        source_quote_hash=HASH_A,
        source_price=Decimal("100"),
        continuous_price=Decimal("100"),
        additive_adjustment=Decimal("0"),
        multiplicative_adjustment=Decimal("1"),
        roll_gap=Decimal("0"),
        policy_hash=HASH_A,
        roll_decision_hash=HASH_B,
        chain_snapshot_hash=HASH_C,
        previous_point_hash=None,
    )
    second = ContinuousFuturesPoint(
        point_id="continuous.point.2",
        series_id="continuous.FUT",
        root_id="FUT.ROOT",
        observed_at=AS_OF,
        source_contract_id="FUT.202605",
        source_quote_hash=HASH_B,
        source_price=Decimal("103"),
        continuous_price=Decimal("101"),
        additive_adjustment=Decimal("-2"),
        multiplicative_adjustment=Decimal("1"),
        roll_gap=Decimal("2"),
        policy_hash=HASH_A,
        roll_decision_hash=HASH_C,
        chain_snapshot_hash=HASH_C,
        previous_point_hash=first.content_hash,
    )
    assert isinstance(first, ContinuousPointProtocol)

    trace = trace_continuous_signal((first, second), trace_id="trace.FUT")

    assert trace.signal_only is True
    assert trace.source_contract_for(first.point_id) == "FUT.202604"
    assert trace.source_contract_for(second.point_id) == "FUT.202605"
    assert trace.require_executable_contract("FUT.202605") == "FUT.202605"
    with pytest.raises(
        FuturesPathError, match="continuous_signal_identifier_not_executable"
    ):
        trace.require_executable_contract(trace.series_id)

    broken = replace(second, previous_point_hash=HASH_A)
    with pytest.raises(FuturesPathError, match="continuous_trace_hash_chain_broken"):
        trace_continuous_signal((first, broken), trace_id="trace.broken")


def test_fixed_maturity_target_selection_avoids_notice_and_expiry() -> None:
    current = _contract("FUT.202604", expiration="2026-04-09")
    too_near = _contract("FUT.NEAR", expiration="2026-03-14")
    target = _contract("FUT.202605", expiration="2026-05-09")
    wrong_maturity = _contract("FUT.202606", expiration="2026-06-08")
    near_notice = _contract(
        "FUT.PHYSICAL.NEAR",
        expiration="2026-05-09",
        settlement_type=SettlementType.PHYSICAL_SETTLED,
        first_notice="2026-03-12",
    )
    policy = RollPlanningPolicy(
        policy_id="roll.fixed.60",
        policy_version="v1",
        fixed_maturity_days=60,
        fixed_maturity_tolerance_days=0,
        minimum_days_to_notice=5,
        minimum_days_to_expiration=5,
        minimum_days_to_last_trade=1,
    )

    selected = select_roll_target(
        current,
        (too_near, wrong_maturity, near_notice, target),
        as_of=AS_OF,
        policy=policy,
    )

    assert selected is target

    with pytest.raises(FuturesPathError, match="no_safe_roll_target"):
        select_roll_target(
            current,
            (too_near, near_notice),
            as_of=AS_OF,
            policy=policy,
        )


def test_roll_planner_preserves_exposure_with_multiplier_change_and_costed_legs() -> (
    None
):
    old = _contract("FUT.OLD", expiration="2026-04-09", multiplier="50")
    new = _contract("FUT.NEW", expiration="2026-05-09", multiplier="25")
    old_quote = _quote(old, "100")
    new_quote = _quote(new, "125")
    policy = RollPlanningPolicy(
        policy_id="roll.full",
        policy_version="v1",
        fixed_maturity_days=60,
        fixed_maturity_tolerance_days=0,
    )

    plan = plan_exposure_preserving_roll(
        plan_id="plan.full",
        as_of=AS_OF,
        old_contract=old,
        new_contract=new,
        old_quote=old_quote,
        new_quote=new_quote,
        current_old_quantity=10,
        target_exposure=Decimal("50000"),
        policy=policy,
        cost_model=_cost_adapter(),
    )

    close_leg, open_leg = plan.legs
    assert close_leg.side is OrderSide.SELL
    assert close_leg.quantity == 10
    assert close_leg.cost.expected_fill_price == Decimal("99.5")
    assert close_leg.cost.commission == Decimal("20")
    assert close_leg.cost.slippage_cost == Decimal("250")
    assert open_leg.side is OrderSide.BUY
    assert open_leg.quantity == 16
    assert open_leg.cost.expected_fill_price == Decimal("125.5")
    assert open_leg.cost.commission == Decimal("32")
    assert open_leg.cost.slippage_cost == Decimal("200")
    assert plan.remaining_old_quantity == 0
    assert plan.resulting_new_quantity == 16
    assert plan.achieved_exposure == Decimal("50000")
    assert plan.rounding_residual == 0
    assert plan.total_cost == Decimal("502")


def test_split_roll_uses_original_quantity_and_records_rounding_residual() -> None:
    old = _contract("FUT.OLD", expiration="2026-04-09", multiplier="50")
    new = _contract("FUT.NEW", expiration="2026-05-09", multiplier="25")
    old_quote = _quote(old, "100")
    new_quote = _quote(new, "125")
    policy = RollPlanningPolicy(
        policy_id="roll.split",
        policy_version="v1",
        split_fractions=(Decimal("0.5"), Decimal("0.5")),
        fixed_maturity_days=60,
        fixed_maturity_tolerance_days=0,
    )
    cost_model = _cost_adapter()

    first = plan_exposure_preserving_roll(
        plan_id="plan.split.1",
        as_of=AS_OF,
        old_contract=old,
        new_contract=new,
        old_quote=old_quote,
        new_quote=new_quote,
        current_old_quantity=10,
        original_old_quantity=10,
        existing_new_quantity=0,
        target_exposure=Decimal("50000"),
        policy=policy,
        cost_model=cost_model,
        tranche_index=0,
    )
    second = plan_exposure_preserving_roll(
        plan_id="plan.split.2",
        as_of=AS_OF,
        old_contract=old,
        new_contract=new,
        old_quote=old_quote,
        new_quote=new_quote,
        current_old_quantity=first.remaining_old_quantity,
        original_old_quantity=10,
        existing_new_quantity=first.resulting_new_quantity,
        target_exposure=Decimal("51000"),
        policy=policy,
        cost_model=cost_model,
        tranche_index=1,
    )

    assert first.legs[0].quantity == 5
    assert first.legs[1].quantity == 8
    assert first.remaining_old_quantity == 5
    assert first.resulting_new_quantity == 8
    assert first.rounding_residual == 0
    assert second.legs[0].quantity == 5
    assert second.legs[1].quantity == 8
    assert second.remaining_old_quantity == 0
    assert second.resulting_new_quantity == 16
    assert second.achieved_exposure == Decimal("50000")
    assert second.rounding_residual == Decimal("1000")


def test_roll_planner_rejects_target_inside_notice_avoidance_window() -> None:
    old = _contract("FUT.OLD", expiration="2026-04-09")
    unsafe = _contract(
        "FUT.UNSAFE",
        expiration="2026-05-09",
        settlement_type=SettlementType.PHYSICAL_SETTLED,
        first_notice="2026-03-12",
    )
    policy = RollPlanningPolicy(
        policy_id="roll.safe",
        policy_version="v1",
        minimum_days_to_notice=5,
    )

    with pytest.raises(
        FuturesPathError,
        match="roll_target_violates_notice_expiry_policy",
    ):
        plan_exposure_preserving_roll(
            plan_id="plan.unsafe",
            as_of=AS_OF,
            old_contract=old,
            new_contract=unsafe,
            old_quote=_quote(old, "100"),
            new_quote=_quote(unsafe, "100"),
            current_old_quantity=2,
            target_exposure=Decimal("10000"),
            policy=policy,
            cost_model=_cost_adapter(),
        )


def test_existing_settlement_and_roll_events_reconcile_to_cash_evidence() -> None:
    old = _contract("FUT.OLD", expiration="2026-04-09", multiplier="25")
    new = _contract("FUT.NEW", expiration="2026-05-09", multiplier="25")
    old_quote = _quote(old, "100")
    new_quote = _quote(new, "100")
    plan = plan_exposure_preserving_roll(
        plan_id="plan.reconcile",
        as_of=AS_OF,
        old_contract=old,
        new_contract=new,
        old_quote=old_quote,
        new_quote=new_quote,
        current_old_quantity=2,
        target_exposure=Decimal("5000"),
        policy=RollPlanningPolicy(
            policy_id="roll.reconcile",
            policy_version="v1",
        ),
        cost_model=_cost_adapter(),
    )
    close_leg, open_leg = plan.legs
    close_fill = FuturesFill(
        fill_id="fill.roll.close",
        intent_hash=plan.content_hash,
        contract_id=old.contract_id,
        quote_hash=old_quote.content_hash,
        filled_at=AS_OF,
        trading_date="2026-03-10",
        session=SessionType.COMBINED,
        side=close_leg.side,
        quantity=close_leg.quantity,
        reference_price=close_leg.reference_price,
        fill_price=close_leg.cost.expected_fill_price,
        multiplier=old.contract_multiplier,
        commission=close_leg.cost.commission,
        slippage_cost=close_leg.cost.slippage_cost,
        realized_trade_pnl=Decimal("200"),
        is_roll_leg=True,
    )
    open_fill = FuturesFill(
        fill_id="fill.roll.open",
        intent_hash=plan.content_hash,
        contract_id=new.contract_id,
        quote_hash=new_quote.content_hash,
        filled_at=AS_OF,
        trading_date="2026-03-10",
        session=SessionType.COMBINED,
        side=open_leg.side,
        quantity=open_leg.quantity,
        reference_price=open_leg.reference_price,
        fill_price=open_leg.cost.expected_fill_price,
        multiplier=new.contract_multiplier,
        commission=open_leg.cost.commission,
        slippage_cost=open_leg.cost.slippage_cost,
        realized_trade_pnl=Decimal("0"),
        is_roll_leg=True,
    )
    execution = RollExecution(
        execution_id="execution.roll",
        decision_hash=plan.content_hash,
        executed_at=AS_OF,
        from_contract_id=old.contract_id,
        to_contract_id=new.contract_id,
        close_fill_hash=close_fill.content_hash,
        open_fill_hash=open_fill.content_hash,
        close_cost=close_fill.total_cost,
        open_cost=open_fill.total_cost,
        price_gap=Decimal("0"),
        roll_yield=Decimal("0"),
    )
    settlement = SettlementEvent(
        event_id="settlement.old",
        contract_id=old.contract_id,
        quote_hash=old_quote.content_hash,
        settled_at=AS_OF,
        previous_settlement_price=Decimal("98"),
        settlement_price=Decimal("100"),
        quantity=2,
        multiplier=Decimal("25"),
        variation_margin=Decimal("100"),
    )
    opening_cash = Decimal("1000")
    closing_cash = (
        opening_cash
        + settlement.variation_margin
        + close_fill.realized_trade_pnl
        - execution.total_roll_cost
    )

    evidence = reconcile_existing_futures_pnl(
        evidence_id="evidence.reconciled",
        observed_at=AS_OF,
        opening_cash=opening_cash,
        closing_cash=closing_cash,
        settlement_events=(settlement,),
        roll_execution=execution,
        roll_fills=(open_fill, close_fill),
        roll_plan=plan,
    )

    assert evidence.settlement_pnl == Decimal("100")
    assert evidence.roll_trade_pnl == Decimal("200")
    assert evidence.roll_cost == Decimal("58")
    assert evidence.expected_cash_delta == Decimal("242")
    assert evidence.actual_cash_delta == Decimal("242")
    assert evidence.residual == 0
    assert evidence.reconciled is True
    evidence.require_reconciled()

    unreconciled = reconcile_existing_futures_pnl(
        evidence_id="evidence.unreconciled",
        observed_at=AS_OF,
        opening_cash=opening_cash,
        closing_cash=closing_cash + Decimal("1"),
        settlement_events=(settlement,),
        roll_execution=execution,
        roll_fills=(close_fill, open_fill),
        roll_plan=plan,
    )
    assert unreconciled.reconciled is False
    with pytest.raises(FuturesPathError, match="futures_pnl_not_reconciled"):
        unreconciled.require_reconciled()

    unrelated_settlement = replace(
        settlement,
        event_id="settlement.unrelated",
        contract_id=new.contract_id,
        quote_hash=new_quote.content_hash,
    )
    with pytest.raises(FuturesPathError, match="contract_unbound"):
        reconcile_existing_futures_pnl(
            evidence_id="evidence.unrelated",
            observed_at=AS_OF,
            opening_cash=opening_cash,
            closing_cash=closing_cash,
            settlement_events=(unrelated_settlement,),
            roll_execution=execution,
            roll_fills=(close_fill, open_fill),
            roll_plan=plan,
        )

    with pytest.raises(FuturesPathError, match="plan_hash_unbound"):
        reconcile_existing_futures_pnl(
            evidence_id="evidence.unbound-plan",
            observed_at=AS_OF,
            opening_cash=opening_cash,
            closing_cash=closing_cash,
            settlement_events=(settlement,),
            roll_execution=replace(execution, decision_hash=HASH_C),
            roll_fills=(close_fill, open_fill),
            roll_plan=plan,
        )

    forged_settlement = replace(
        settlement,
        event_id="settlement.forged-price",
        previous_settlement_price=Decimal("97"),
        settlement_price=Decimal("99"),
        variation_margin=Decimal("100"),
    )
    with pytest.raises(FuturesPathError, match="price_unbound"):
        reconcile_existing_futures_pnl(
            evidence_id="evidence.forged-settlement-price",
            observed_at=AS_OF,
            opening_cash=opening_cash,
            closing_cash=closing_cash,
            settlement_events=(forged_settlement,),
            roll_execution=execution,
            roll_fills=(close_fill, open_fill),
            roll_plan=plan,
        )

    wrong_multiplier_fill = replace(close_fill, multiplier=Decimal("50"))
    wrong_multiplier_execution = replace(
        execution,
        close_fill_hash=wrong_multiplier_fill.content_hash,
    )
    with pytest.raises(FuturesPathError, match="fill_multiplier_mismatch"):
        reconcile_existing_futures_pnl(
            evidence_id="evidence.wrong-fill-multiplier",
            observed_at=AS_OF,
            opening_cash=opening_cash,
            closing_cash=closing_cash,
            settlement_events=(settlement,),
            roll_execution=wrong_multiplier_execution,
            roll_fills=(wrong_multiplier_fill, open_fill),
            roll_plan=plan,
        )

    wrong_reference_fill = replace(close_fill, reference_price=Decimal("99"))
    wrong_reference_execution = replace(
        execution,
        close_fill_hash=wrong_reference_fill.content_hash,
    )
    with pytest.raises(FuturesPathError, match="fill_reference_price_mismatch"):
        reconcile_existing_futures_pnl(
            evidence_id="evidence.wrong-fill-reference",
            observed_at=AS_OF,
            opening_cash=opening_cash,
            closing_cash=closing_cash,
            settlement_events=(settlement,),
            roll_execution=wrong_reference_execution,
            roll_fills=(wrong_reference_fill, open_fill),
            roll_plan=plan,
        )

    with pytest.raises(FuturesPathError, match="leg_reference_price_mismatch"):
        replace(plan, old_price=Decimal("99"))
    with pytest.raises(FuturesPathError, match="original_old_quantity_inconsistent"):
        replace(plan, original_old_quantity=-2)
