from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_research.research.multi_asset.option_path import (
    CalculatedOptionDelta,
    CleanedOptionChain,
    DEFAULT_OPTION_CLEANING_POLICY,
    DeltaFallback,
    ForwardEstimate,
    ForwardMethod,
    OptionChainCleaner,
    OptionAttributionPolicy,
    OptionGreeks,
    OptionPathError,
    OptionPathMark,
    OptionRight,
    OptionSelectionPolicy,
    RawOptionObservation,
    attribute_option_path,
    select_option_contract,
)


AS_OF = datetime(2025, 2, 3, 15, tzinfo=UTC)
ATTRIBUTION_POLICY = OptionAttributionPolicy(
    policy_id="vanilla-path-residual-v1",
    version="1",
    maximum_absolute_residual=Decimal("1000"),
    maximum_relative_residual=Decimal("0.25"),
)


def _forward() -> ForwardEstimate:
    return ForwardEstimate(
        value=Decimal("101"),
        method=ForwardMethod.BORROW_ADJUSTED_CARRY,
        estimated_at=AS_OF,
        input_hashes=("sha256:spot", "sha256:curve", "sha256:borrow"),
        rate=Decimal("0.04"),
        dividend_yield=Decimal("0.01"),
        borrow_rate=Decimal("0.005"),
    )


def _observation(
    contract_id: str,
    *,
    delta: Decimal,
    strike: Decimal,
    adjusted: bool = False,
    known_at: datetime = AS_OF,
) -> RawOptionObservation:
    return RawOptionObservation(
        contract_id=contract_id,
        underlying_id="underlying:equity",
        right=OptionRight.PUT,
        strike=strike,
        expiry=AS_OF + timedelta(days=45),
        observed_at=AS_OF - timedelta(seconds=5),
        known_at=known_at,
        bid=Decimal("4.8"),
        ask=Decimal("5.2"),
        bid_size=Decimal("20"),
        ask_size=Decimal("25"),
        volume=100,
        open_interest=1000,
        bid_iv=Decimal("0.24"),
        ask_iv=Decimal("0.26"),
        delta=delta,
        source_quote_hash=f"sha256:{contract_id}",
        adjusted_contract=adjusted,
    )


def _cleaned_chain() -> CleanedOptionChain:
    return OptionChainCleaner(DEFAULT_OPTION_CLEANING_POLICY).clean(
        underlying_id="underlying:equity",
        decision_at=AS_OF,
        spot=Decimal("100"),
        forward=_forward(),
        observations=(
            _observation(
                "option:put-95",
                delta=Decimal("-0.28"),
                strike=Decimal("95"),
            ),
            _observation(
                "option:put-100",
                delta=Decimal("-0.45"),
                strike=Decimal("100"),
            ),
            _observation(
                "option:adjusted",
                delta=Decimal("-0.31"),
                strike=Decimal("97"),
                adjusted=True,
            ),
        ),
    )


def _calculated_deltas(
    *,
    put_95: Decimal = Decimal("-0.28"),
    put_100: Decimal = Decimal("-0.45"),
    calculated_at: datetime = AS_OF,
    known_at: datetime = AS_OF,
) -> tuple[CalculatedOptionDelta, ...]:
    return tuple(
        CalculatedOptionDelta(
            contract_id=contract_id,
            calculated_at=calculated_at,
            known_at=known_at,
            delta=delta,
            market_state_hash="sha256:decision-state",
            model_specification_hash="sha256:model-specification",
            valuation_input_hash=f"sha256:valuation-input:{contract_id}",
        )
        for contract_id, delta in (
            ("option:put-95", put_95),
            ("option:put-100", put_100),
        )
    )


def test_cleaning_retains_raw_bid_ask_iv_and_exclusion_evidence() -> None:
    chain = _cleaned_chain()

    assert len(chain.points) == 3
    adjusted = next(
        point for point in chain.points if point.contract_id == "option:adjusted"
    )
    assert adjusted.raw_bid_iv == Decimal("0.24")
    assert adjusted.raw_ask_iv == Decimal("0.26")
    assert adjusted.cleaned_iv is None
    assert adjusted.exclusion_reasons == ("ADJUSTED_CONTRACT",)
    included = chain.included_points[0]
    assert included.cleaned_iv == Decimal("0.25")
    assert included.liquidity_weight > Decimal("0")
    assert included.forward_hash == chain.forward.content_hash
    assert included.total_variance is not None


def test_delta_selection_uses_only_real_point_in_time_contracts() -> None:
    policy = OptionSelectionPolicy(
        policy_id="put-30-delta",
        version="1",
        right=OptionRight.PUT,
        target_days_to_expiry=45,
        minimum_days_to_expiry=30,
        maximum_days_to_expiry=60,
        target_delta=Decimal("-0.30"),
        maximum_delta_distance=Decimal("0.05"),
        minimum_liquidity_weight=Decimal("0.25"),
        fallback=DeltaFallback.REJECT,
    )

    decision = select_option_contract(_cleaned_chain(), policy, _calculated_deltas())

    assert decision.selected_contract_id == "option:put-95"
    assert decision.selected_delta == Decimal("-0.28")
    assert decision.selected_delta_evidence_hash is not None
    assert decision.delta_distance == Decimal("0.02")
    assert decision.exact_tolerance_match
    assert "option:adjusted" not in decision.eligible_contract_ids


def test_delta_selection_ignores_supplier_delta_and_uses_model_result() -> None:
    policy = OptionSelectionPolicy(
        policy_id="put-30-delta",
        version="2",
        right=OptionRight.PUT,
        target_days_to_expiry=45,
        minimum_days_to_expiry=30,
        maximum_days_to_expiry=60,
        target_delta=Decimal("-0.30"),
        maximum_delta_distance=Decimal("0.05"),
        minimum_liquidity_weight=Decimal("0.25"),
        fallback=DeltaFallback.REJECT,
    )

    decision = select_option_contract(
        _cleaned_chain(),
        policy,
        _calculated_deltas(put_95=Decimal("-0.60"), put_100=Decimal("-0.31")),
    )

    assert decision.selected_contract_id == "option:put-100"
    assert decision.selected_delta == Decimal("-0.31")


def test_delta_selection_rejects_future_or_unbound_model_results() -> None:
    policy = OptionSelectionPolicy(
        policy_id="put-30-delta",
        version="2",
        right=OptionRight.PUT,
        target_days_to_expiry=45,
        minimum_days_to_expiry=30,
        maximum_days_to_expiry=60,
        target_delta=Decimal("-0.30"),
        maximum_delta_distance=Decimal("0.05"),
        minimum_liquidity_weight=Decimal("0.25"),
        fallback=DeltaFallback.REJECT,
    )
    future = AS_OF + timedelta(microseconds=1)

    with pytest.raises(OptionPathError, match="future knowledge"):
        select_option_contract(
            _cleaned_chain(),
            policy,
            _calculated_deltas(calculated_at=future, known_at=future),
        )
    with pytest.raises(OptionPathError, match="required"):
        select_option_contract(_cleaned_chain(), policy, ())


def test_cleaning_fails_closed_if_snapshot_contains_future_knowledge() -> None:
    future = _observation(
        "option:future",
        delta=Decimal("-0.30"),
        strike=Decimal("95"),
        known_at=AS_OF + timedelta(seconds=1),
    )
    with pytest.raises(OptionPathError, match="future knowledge"):
        OptionChainCleaner(DEFAULT_OPTION_CLEANING_POLICY).clean(
            underlying_id="underlying:equity",
            decision_at=AS_OF,
            spot=Decimal("100"),
            forward=_forward(),
            observations=(future,),
        )


def _mark(
    day: int,
    *,
    market_price: str,
    spot: str,
    iv: str,
    hedge_pnl: str = "0",
    carry_pnl: str = "0",
    slippage: str = "0",
    cost: str = "0",
) -> OptionPathMark:
    return OptionPathMark(
        contract_id="option:put-95",
        marked_at=AS_OF + timedelta(days=day),
        market_state_hash=f"sha256:state-{day}",
        market_quote_hash=f"sha256:quote-{day}",
        model_specification_hash="sha256:model",
        market_price=Decimal(market_price),
        theoretical_price=Decimal(market_price) + Decimal("0.1"),
        spot_price=Decimal(spot),
        implied_volatility=Decimal(iv),
        rate=Decimal("0.04"),
        dividend_yield=Decimal("0.01"),
        skew=Decimal("-0.05"),
        greeks=OptionGreeks(
            delta=Decimal("-0.30") + Decimal(day) / Decimal("100"),
            gamma=Decimal("0.02"),
            vega_per_vol_point=Decimal("0.08"),
            theta_per_calendar_day=Decimal("-0.03"),
            rho_per_rate_point=Decimal("-0.01"),
        ),
        hedge_pnl_since_previous=Decimal(hedge_pnl),
        carry_pnl_since_previous=Decimal(carry_pnl),
        slippage_since_previous=Decimal(slippage),
        transaction_cost_since_previous=Decimal(cost),
    )


def test_intermediate_path_and_greek_attribution_reconcile_exactly() -> None:
    marks = (
        _mark(0, market_price="5", spot="100", iv="0.25"),
        _mark(
            1,
            market_price="6.2",
            spot="96",
            iv="0.30",
            hedge_pnl="15",
            carry_pnl="2",
            slippage="1",
            cost="3",
        ),
        _mark(
            3,
            market_price="4.5",
            spot="101",
            iv="0.27",
            hedge_pnl="-8",
            carry_pnl="3",
            slippage="2",
            cost="4",
        ),
    )

    attribution = attribute_option_path(
        marks,
        signed_quantity=Decimal("2"),
        multiplier=Decimal("100"),
        policy=ATTRIBUTION_POLICY,
    )

    assert len(attribution.intervals) == 2
    assert attribution.reconciled
    assert attribution.actual_pnl == attribution.attributed_pnl
    assert all(
        interval.actual_pnl == interval.attributed_pnl
        for interval in attribution.intervals
    )
    assert any(interval.model_residual != 0 for interval in attribution.intervals)
    assert attribution.content_hash.startswith("sha256:")


def test_path_rejects_non_chronological_marks() -> None:
    first = _mark(0, market_price="5", spot="100", iv="0.25")
    duplicate_time = replace(first, market_price=Decimal("5.1"))
    with pytest.raises(OptionPathError, match="strictly chronological"):
        attribute_option_path(
            (first, duplicate_time),
            signed_quantity=Decimal("1"),
            multiplier=Decimal("100"),
            policy=ATTRIBUTION_POLICY,
        )


def test_path_rejects_model_lineage_change_and_excess_residual() -> None:
    first = _mark(0, market_price="5", spot="100", iv="0.25")
    later = _mark(1, market_price="8", spot="100", iv="0.25")
    with pytest.raises(OptionPathError, match="residual exceeds policy"):
        attribute_option_path(
            (first, later),
            signed_quantity=Decimal("1"),
            multiplier=Decimal("100"),
            policy=OptionAttributionPolicy(
                policy_id="exact-only",
                version="1",
                maximum_absolute_residual=Decimal("0"),
                maximum_relative_residual=Decimal("0"),
            ),
        )
    with pytest.raises(OptionPathError, match="changed pricing model"):
        attribute_option_path(
            (
                first,
                replace(later, model_specification_hash="sha256:model-v2"),
            ),
            signed_quantity=Decimal("1"),
            multiplier=Decimal("100"),
            policy=ATTRIBUTION_POLICY,
        )


def test_path_residual_limit_cannot_be_inflated_by_auxiliary_cash_flows() -> None:
    first = _mark(0, market_price="5", spot="100", iv="0.25")
    later = _mark(
        1,
        market_price="8",
        spot="100",
        iv="0.25",
        hedge_pnl="10000",
    )

    with pytest.raises(OptionPathError, match="residual exceeds policy"):
        attribute_option_path(
            (first, later),
            signed_quantity=Decimal("1"),
            multiplier=Decimal("100"),
            policy=OptionAttributionPolicy(
                policy_id="relative-option-leg-only",
                version="1",
                maximum_absolute_residual=Decimal("0"),
                maximum_relative_residual=Decimal("0.25"),
            ),
        )


def test_path_rejects_reused_quote_evidence_across_marks() -> None:
    first = _mark(0, market_price="5", spot="100", iv="0.25")
    later = replace(
        _mark(1, market_price="5.1", spot="100", iv="0.25"),
        market_quote_hash=first.market_quote_hash,
    )

    with pytest.raises(OptionPathError, match="duplicate market quotes"):
        attribute_option_path(
            (first, later),
            signed_quantity=Decimal("1"),
            multiplier=Decimal("100"),
            policy=ATTRIBUTION_POLICY,
        )
