from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_research.research.multi_asset.spot import (
    BorrowRecall,
    BorrowRecallReason,
    BorrowRecallRevisionStore,
    BorrowScenario,
    BorrowScenarioSet,
    BorrowSnapshot,
    CashBalance,
    CorporateAction,
    CorporateActionRevisionStore,
    CorporateActionType,
    PointInTimeSpotUniverse,
    SpotBook,
    SpotInstrument,
    SpotInstrumentKind,
    SpotPosition,
    SpotResearchError,
    UniverseMembership,
    accrue_borrow_cost,
    apply_borrow_recall,
    apply_corporate_action,
    validate_short_trade,
)


T0 = datetime(2025, 1, 2, 0, tzinfo=UTC)
HASH_A = "sha256:" + ("a" * 64)
HASH_B = "sha256:" + ("b" * 64)


def _book(quantity: str = "100") -> SpotBook:
    return SpotBook(
        positions=(
            SpotPosition(
                instrument_id="instrument:old",
                quantity=Decimal(quantity),
                total_cost_basis=Decimal("8000"),
                currency="USD",
            ),
        ),
        cash=(CashBalance(currency="USD", amount=Decimal("2000")),),
    )


def _action(
    action_type: CorporateActionType,
    *,
    ratio: str = "1",
    cash: str = "0",
    tax: str = "0",
    replacement: str | None = None,
    child: str | None = None,
    child_basis: str = "0",
) -> CorporateAction:
    is_dividend = action_type in {
        CorporateActionType.CASH_DIVIDEND,
        CorporateActionType.SPECIAL_DIVIDEND,
    }
    return CorporateAction(
        action_id=f"action:{action_type.value.lower()}",
        revision=1,
        action_type=action_type,
        instrument_id="instrument:old",
        announced_at=T0,
        known_at=T0 + timedelta(hours=1),
        record_at=T0 + timedelta(days=1) if is_dividend else None,
        ex_at=T0 + timedelta(days=1) if is_dividend else None,
        payment_at=T0 + timedelta(days=2) if is_dividend else None,
        effective_at=T0 + timedelta(days=2),
        source_id="source:exchange",
        source_record_hash=HASH_A,
        currency="USD" if is_dividend else None,
        cash_per_share=Decimal(cash),
        ratio=Decimal(ratio),
        tax_rate=Decimal(tax),
        replacement_instrument_id=replacement,
        child_instrument_id=child,
        child_cost_basis_fraction=Decimal(child_basis),
        affected_derivative_contract_ids=("option:adjusted",),
        derivative_adjustment_policy_id="policy:occ-adjustment",
    )


def test_two_for_one_split_preserves_cost_basis_and_economic_value() -> None:
    before = _book()
    action = _action(CorporateActionType.SPLIT, ratio="2")

    result = apply_corporate_action(
        before,
        action,
        applied_at=T0 + timedelta(days=2),
    )

    after_position = result.book_after.position("instrument:old")
    assert after_position is not None
    assert after_position.quantity == Decimal("200")
    assert after_position.total_cost_basis == Decimal("8000")
    before_value = before.value(
        prices={"instrument:old": Decimal("100")},
        fx_to_base={"USD": Decimal("1")},
    )
    after_value = result.book_after.value(
        prices={"instrument:old": Decimal("50")},
        fx_to_base={"USD": Decimal("1")},
    )
    assert before_value == after_value
    assert result.postings[0].related_derivative_contract_ids == ("option:adjusted",)


def test_dividend_cash_tax_and_short_compensation_are_economic_postings() -> None:
    dividend = _action(
        CorporateActionType.CASH_DIVIDEND,
        cash="2",
        tax="0.15",
    )
    long_result = apply_corporate_action(
        _book(),
        dividend,
        applied_at=T0 + timedelta(days=2),
        entitlement_book=_book(),
    )
    assert long_result.book_after.cash_amount("USD") == Decimal("2170")
    assert long_result.postings[0].cash_delta == Decimal("170")
    assert long_result.postings[0].tax_amount == Decimal("30")

    short_result = apply_corporate_action(
        _book("-10"),
        dividend,
        applied_at=T0 + timedelta(days=2),
        entitlement_book=_book("-10"),
    )
    assert short_result.book_after.cash_amount("USD") == Decimal("1980")
    assert short_result.postings[0].cash_delta == Decimal("-20")
    assert short_result.postings[0].tax_amount == Decimal("0")

    sold_after_record = apply_corporate_action(
        _book("25"),
        dividend,
        applied_at=T0 + timedelta(days=2),
        entitlement_book=_book("100"),
    )
    assert sold_after_record.postings[0].entitlement_quantity == Decimal("100")
    assert sold_after_record.book_after.position("instrument:old") == _book(
        "25"
    ).position("instrument:old")
    assert sold_after_record.postings[0].cash_delta == Decimal("170")


def test_spinoff_and_merger_create_real_replacement_positions_and_move_basis() -> None:
    spin = _action(
        CorporateActionType.SPIN_OFF,
        ratio="0.5",
        child="instrument:child",
        child_basis="0.20",
    )
    spun = apply_corporate_action(
        _book(), spin, applied_at=T0 + timedelta(days=2)
    ).book_after
    parent = spun.position("instrument:old")
    child = spun.position("instrument:child")
    assert parent is not None and child is not None
    assert parent.total_cost_basis == Decimal("6400")
    assert child.quantity == Decimal("50")
    assert child.total_cost_basis == Decimal("1600")

    merger = _action(
        CorporateActionType.MERGER,
        ratio="1.25",
        replacement="instrument:new",
    )
    merged = apply_corporate_action(
        _book(), merger, applied_at=T0 + timedelta(days=2)
    ).book_after
    assert merged.position("instrument:old") is None
    replacement_position = merged.position("instrument:new")
    assert replacement_position is not None
    assert replacement_position.quantity == Decimal("125")
    assert replacement_position.total_cost_basis == Decimal("8000")


def test_corporate_action_revisions_are_append_only_and_point_in_time() -> None:
    first = _action(CorporateActionType.SPLIT, ratio="2")
    correction = replace(
        first,
        revision=2,
        known_at=T0 + timedelta(days=3),
        ratio=Decimal("3"),
        supersedes_hash=first.content_hash,
    )
    store = CorporateActionRevisionStore((first, correction))

    assert store.as_of(T0 + timedelta(days=2))[0].ratio == Decimal("2")
    assert store.as_of(T0 + timedelta(days=4))[0].ratio == Decimal("3")
    assert store.history == (first, correction)

    with pytest.raises(SpotResearchError, match="superseded"):
        CorporateActionRevisionStore((first, replace(correction, supersedes_hash=None)))


def test_universe_separates_announcement_implementation_and_keeps_delisted_history() -> (
    None
):
    membership = UniverseMembership(
        universe_id="universe:index",
        instrument_id="instrument:old",
        effective_from=T0 + timedelta(days=5),
        effective_to=T0 + timedelta(days=20),
        announcement_at=T0 + timedelta(days=1),
        implementation_at=T0 + timedelta(days=5),
        known_at=T0 + timedelta(days=1, hours=1),
        membership_source_hash=HASH_B,
    )
    universe = PointInTimeSpotUniverse((membership,))

    assert (
        universe.members(
            "universe:index",
            effective_at=T0 + timedelta(days=4),
            knowledge_at=T0 + timedelta(days=4),
        )
        == ()
    )
    assert universe.members(
        "universe:index",
        effective_at=T0 + timedelta(days=10),
        knowledge_at=T0 + timedelta(days=10),
    ) == ("instrument:old",)
    assert (
        universe.members(
            "universe:index",
            effective_at=T0 + timedelta(days=10),
            knowledge_at=T0,
        )
        == ()
    )

    instrument = SpotInstrument(
        instrument_id="instrument:old",
        economic_underlying_id="underlying:company",
        issuer_id="issuer:company",
        security_id="security:common",
        listing_id="listing:exchange",
        kind=SpotInstrumentKind.COMMON_STOCK,
        share_class="A",
        exchange="XNAS",
        currency="USD",
        listed_at=T0 - timedelta(days=100),
        delisted_at=T0 + timedelta(days=20),
    )
    assert instrument.tradeable_at(T0 + timedelta(days=10))
    assert not instrument.tradeable_at(T0 + timedelta(days=21))

    corrected = replace(
        membership,
        known_at=T0 + timedelta(days=2),
        membership_source_hash=HASH_A,
        trade_halted=True,
    )
    revised_universe = PointInTimeSpotUniverse((membership, corrected))
    assert revised_universe.members(
        "universe:index",
        effective_at=T0 + timedelta(days=10),
        knowledge_at=T0 + timedelta(days=1, hours=12),
    ) == ("instrument:old",)
    assert (
        revised_universe.members(
            "universe:index",
            effective_at=T0 + timedelta(days=10),
            knowledge_at=T0 + timedelta(days=10),
        )
        == ()
    )
    with pytest.raises(SpotResearchError, match="duplicate universe"):
        PointInTimeSpotUniverse((membership, membership))


def _borrow_scenarios() -> BorrowScenarioSet:
    snapshots = []
    assumptions = {
        BorrowScenario.OPTIMISTIC: (True, "1000", "0.01"),
        BorrowScenario.BASE: (True, "500", "0.05"),
        BorrowScenario.CONSERVATIVE: (True, "100", "0.25"),
        BorrowScenario.UNAVAILABLE: (False, "0", "0.50"),
    }
    for scenario, (borrowable, capacity, fee) in assumptions.items():
        snapshots.append(
            BorrowSnapshot(
                snapshot_id=f"borrow:{scenario.value.lower()}",
                scenario=scenario,
                instrument_id="instrument:old",
                observed_at=T0,
                known_at=T0 + timedelta(minutes=1),
                effective_from=T0,
                effective_to=None,
                borrowable=borrowable,
                available_quantity=Decimal(capacity),
                annual_fee_rate=Decimal(fee),
                recall_probability=Decimal("0.01") if borrowable else Decimal("1"),
                short_sale_ban=not borrowable,
                uptick_restriction=scenario is BorrowScenario.CONSERVATIVE,
                trade_halted=False,
                hard_to_borrow=scenario
                in {BorrowScenario.CONSERVATIVE, BorrowScenario.UNAVAILABLE},
                maximum_holding_days=30 if borrowable else None,
                source_hash=HASH_A
                if scenario in {BorrowScenario.OPTIMISTIC, BorrowScenario.BASE}
                else HASH_B,
            )
        )
    return BorrowScenarioSet(tuple(snapshots))


def test_borrow_scenarios_enforce_capacity_and_post_time_varying_fee() -> None:
    scenarios = _borrow_scenarios()
    base = scenarios.snapshot(
        "instrument:old",
        BorrowScenario.BASE,
        effective_at=T0 + timedelta(days=1),
        knowledge_at=T0 + timedelta(days=1),
    )
    assert validate_short_trade(
        base,
        instrument_id="instrument:old",
        effective_at=T0 + timedelta(days=1),
        knowledge_at=T0 + timedelta(days=1),
        requested_quantity=Decimal("500"),
        price_is_uptick=False,
    ).permitted
    over_capacity = validate_short_trade(
        base,
        instrument_id="instrument:old",
        effective_at=T0 + timedelta(days=1),
        knowledge_at=T0 + timedelta(days=1),
        requested_quantity=Decimal("501"),
        price_is_uptick=True,
    )
    assert not over_capacity.permitted
    assert over_capacity.rejection_reasons == ("BORROW_CAPACITY_EXCEEDED",)

    unavailable = scenarios.snapshot(
        "instrument:old",
        BorrowScenario.UNAVAILABLE,
        effective_at=T0 + timedelta(days=1),
        knowledge_at=T0 + timedelta(days=1),
    )
    assert not validate_short_trade(
        unavailable,
        instrument_id="instrument:old",
        effective_at=T0 + timedelta(days=1),
        knowledge_at=T0 + timedelta(days=1),
        requested_quantity=Decimal("1"),
        price_is_uptick=True,
    ).permitted

    posting = accrue_borrow_cost(
        _book("-10").positions[0],
        base,
        price=Decimal("100"),
        elapsed_days=Decimal("30"),
        occurred_at=T0 + timedelta(days=30),
        knowledge_at=T0 + timedelta(days=30),
    )
    assert posting.cash_delta == -(Decimal("10") * 100 * Decimal("0.05") * 30 / 365)

    with pytest.raises(SpotResearchError, match="instrument mismatch"):
        validate_short_trade(
            base,
            instrument_id="instrument:other",
            effective_at=T0 + timedelta(days=1),
            knowledge_at=T0 + timedelta(days=1),
            requested_quantity=Decimal("1"),
            price_is_uptick=True,
        )
    with pytest.raises(SpotResearchError, match="not valid"):
        validate_short_trade(
            base,
            instrument_id="instrument:old",
            effective_at=T0,
            knowledge_at=T0,
            requested_quantity=Decimal("1"),
            price_is_uptick=True,
        )


def test_borrow_model_requires_all_four_missing_data_scenarios() -> None:
    base = _borrow_scenarios().snapshot(
        "instrument:old",
        BorrowScenario.BASE,
        effective_at=T0 + timedelta(days=1),
        knowledge_at=T0 + timedelta(days=1),
    )
    with pytest.raises(SpotResearchError, match="coverage incomplete"):
        BorrowScenarioSet((base,))


def test_rights_entitlement_subscription_and_fractional_cash_are_economic() -> None:
    rights_issue = CorporateAction(
        action_id="action:rights-issue",
        revision=1,
        action_type=CorporateActionType.RIGHTS_ISSUE,
        instrument_id="instrument:old",
        announced_at=T0,
        known_at=T0 + timedelta(hours=1),
        record_at=T0 + timedelta(days=1),
        ex_at=T0 + timedelta(days=2),
        payment_at=T0 + timedelta(days=3),
        effective_at=T0 + timedelta(days=3),
        source_id="source:exchange",
        source_record_hash=HASH_A,
        currency="USD",
        ratio=Decimal("0.1"),
        tax_rate=Decimal("0"),
        rights_instrument_id="instrument:right",
        subscription_price=Decimal("20"),
        whole_share_only=True,
        cash_in_lieu_price=Decimal("0.5"),
        terms_policy_hash=HASH_B,
    )
    entitlement_book = _book("105")
    issued = apply_corporate_action(
        entitlement_book,
        rights_issue,
        applied_at=T0 + timedelta(days=3),
        entitlement_book=entitlement_book,
    )
    rights = issued.book_after.position("instrument:right")
    assert rights is not None
    assert rights.quantity == Decimal("10")
    assert issued.book_after.cash_amount("USD") == Decimal("2000.25")
    assert [item.posting_type.value for item in issued.postings] == [
        "RIGHTS_ENTITLEMENT",
        "CASH_IN_LIEU",
    ]

    subscription = CorporateAction(
        action_id="action:rights-subscription",
        revision=1,
        action_type=CorporateActionType.RIGHTS_SUBSCRIPTION,
        instrument_id="instrument:right",
        announced_at=T0,
        known_at=T0 + timedelta(hours=1),
        record_at=None,
        ex_at=None,
        payment_at=T0 + timedelta(days=4),
        effective_at=T0 + timedelta(days=4),
        source_id="source:exchange",
        source_record_hash=HASH_B,
        currency="USD",
        ratio=Decimal("1"),
        replacement_instrument_id="instrument:old",
        subscription_price=Decimal("20"),
        subscription_fraction=Decimal("1"),
        terms_policy_hash=HASH_A,
    )
    subscribed = apply_corporate_action(
        issued.book_after,
        subscription,
        applied_at=T0 + timedelta(days=4),
    )
    assert subscribed.book_after.position("instrument:right") is None
    shares = subscribed.book_after.position("instrument:old")
    assert shares is not None
    assert shares.quantity == Decimal("115")
    assert shares.total_cost_basis == Decimal("8200")
    assert subscribed.book_after.cash_amount("USD") == Decimal("1800.25")


def test_cash_and_stock_merger_preserves_fractional_and_basis_evidence() -> None:
    merger = CorporateAction(
        action_id="action:mixed-merger",
        revision=1,
        action_type=CorporateActionType.MERGER,
        instrument_id="instrument:old",
        announced_at=T0,
        known_at=T0 + timedelta(hours=1),
        record_at=T0 + timedelta(days=1),
        ex_at=T0 + timedelta(days=2),
        payment_at=T0 + timedelta(days=3),
        effective_at=T0 + timedelta(days=3),
        source_id="source:exchange",
        source_record_hash=HASH_A,
        currency="USD",
        cash_per_share=Decimal("10"),
        ratio=Decimal("0.5"),
        tax_rate=Decimal("0.1"),
        replacement_instrument_id="instrument:new",
        cash_basis_fraction=Decimal("0.25"),
        whole_share_only=True,
        cash_in_lieu_price=Decimal("50"),
        terms_policy_hash=HASH_B,
    )
    result = apply_corporate_action(
        _book("3"),
        merger,
        applied_at=T0 + timedelta(days=3),
    )
    replacement = result.book_after.position("instrument:new")
    assert replacement is not None
    assert replacement.quantity == Decimal("1")
    assert replacement.total_cost_basis == Decimal("4000")
    assert result.book_after.position("instrument:old") is None
    assert result.book_after.cash_amount("USD") == Decimal("2055")
    assert {item.posting_type.value for item in result.postings} == {
        "REPLACEMENT_DELIVERY",
        "MERGER_CASH",
        "CASH_IN_LIEU",
    }


@pytest.mark.parametrize(
    ("action_type", "ratio", "expected_quantity"),
    [
        (CorporateActionType.REVERSE_SPLIT, "0.5", "50"),
        (CorporateActionType.STOCK_DIVIDEND, "1.1", "110"),
        (CorporateActionType.BONUS_ISSUE, "1.2", "120"),
    ],
)
def test_all_quantity_corporate_actions_preserve_total_basis(
    action_type: CorporateActionType,
    ratio: str,
    expected_quantity: str,
) -> None:
    result = apply_corporate_action(
        _book(),
        _action(action_type, ratio=ratio),
        applied_at=T0 + timedelta(days=2),
    )
    position = result.book_after.position("instrument:old")
    assert position is not None
    assert position.quantity == Decimal(expected_quantity)
    assert position.total_cost_basis == Decimal("8000")


def test_tender_liquidation_replacement_delisting_and_ex_rights_are_typed() -> None:
    tender = _action(
        CorporateActionType.TENDER_OFFER,
        ratio="0.25",
        cash="90",
        tax="0.1",
    )
    tender = replace(tender, currency="USD")
    tendered = apply_corporate_action(
        _book(), tender, applied_at=T0 + timedelta(days=2)
    ).book_after
    assert tendered.position("instrument:old") == SpotPosition(
        instrument_id="instrument:old",
        quantity=Decimal("75"),
        total_cost_basis=Decimal("6000"),
        currency="USD",
    )
    assert tendered.cash_amount("USD") == Decimal("4225")

    liquidation = replace(
        _action(CorporateActionType.LIQUIDATION, cash="70"),
        currency="USD",
    )
    liquidated = apply_corporate_action(
        _book(), liquidation, applied_at=T0 + timedelta(days=2)
    ).book_after
    assert liquidated.position("instrument:old") is None
    assert liquidated.cash_amount("USD") == Decimal("9000")

    replacement = _action(
        CorporateActionType.REPLACEMENT,
        ratio="1",
        replacement="instrument:new",
    )
    replaced = apply_corporate_action(
        _book(), replacement, applied_at=T0 + timedelta(days=2)
    ).book_after
    assert replaced.position("instrument:new") is not None
    assert replaced.position("instrument:old") is None

    for action_type in (
        CorporateActionType.DELISTING,
        CorporateActionType.EX_RIGHTS,
    ):
        action = _action(
            action_type,
            ratio="0.9" if action_type is CorporateActionType.EX_RIGHTS else "1",
        )
        result = apply_corporate_action(
            _book(), action, applied_at=T0 + timedelta(days=2)
        )
        assert result.book_after == _book()
        assert result.postings[0].quantity_delta == Decimal("0")


def test_borrow_recall_revision_and_forced_buy_in_reconcile_short_book() -> None:
    recall = BorrowRecall(
        recall_id="recall:lender:1",
        revision=1,
        instrument_id="instrument:old",
        reason=BorrowRecallReason.LENDER_RECALL,
        announced_at=T0,
        known_at=T0 + timedelta(minutes=1),
        effective_at=T0 + timedelta(days=1),
        cover_deadline_at=T0 + timedelta(days=2),
        recalled_quantity=Decimal("4"),
        penalty_per_unit=Decimal("1"),
        source_hash=HASH_A,
    )
    application = apply_borrow_recall(
        _book("-10"),
        recall,
        applied_at=T0 + timedelta(days=1),
        execution_price=Decimal("90"),
        execution_quote_hash=HASH_B,
        commission_per_unit=Decimal("0.5"),
    )
    remaining = application.book_after.position("instrument:old")
    assert remaining is not None
    assert remaining.quantity == Decimal("-6")
    assert remaining.total_cost_basis == Decimal("4800")
    assert application.covered_quantity == Decimal("4")
    assert application.book_after.cash_amount("USD") == Decimal("1634")
    assert [item.posting_type.value for item in application.postings] == [
        "BORROW_RECALL",
        "FORCED_BUY_IN",
    ]

    correction = replace(
        recall,
        revision=2,
        known_at=T0 + timedelta(hours=2),
        effective_at=T0 + timedelta(days=1, hours=1),
        recalled_quantity=Decimal("10"),
        supersedes_hash=recall.content_hash,
        correction_reason="lender expanded recall",
    )
    store = BorrowRecallRevisionStore((recall, correction))
    assert store.as_of(T0 + timedelta(hours=1)) == (recall,)
    assert store.as_of(T0 + timedelta(hours=3)) == (correction,)
    with pytest.raises(SpotResearchError, match="revision chain"):
        BorrowRecallRevisionStore((recall, replace(correction, supersedes_hash=HASH_B)))

    with pytest.raises(SpotResearchError, match="open short"):
        apply_borrow_recall(
            _book("10"),
            recall,
            applied_at=T0 + timedelta(days=1),
            execution_price=Decimal("90"),
            execution_quote_hash=HASH_B,
        )
