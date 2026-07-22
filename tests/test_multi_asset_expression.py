from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

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
    LegRole,
    LegSelectionRule,
    ProductKind,
    ScenarioRange,
    StrategyTargets,
)


AS_OF = datetime(2025, 1, 2, 15, tzinfo=UTC)


def _hypothesis() -> EconomicHypothesis:
    return EconomicHypothesis(
        hypothesis_id="hyp-equity-upside",
        version="1.0.0",
        economic_underlying_id="underlying:example-equity",
        rationale="Positive earnings revisions should persist over the horizon.",
        expected_direction=Direction.LONG,
        distribution=ExpectedMarketDistribution(
            expected_return=Decimal("0.08"),
            annualized_volatility=Decimal("0.24"),
            downside_tail_return=Decimal("-0.20"),
            upside_return=Decimal("0.25"),
            horizon_days=60,
            risk_free_rate=Decimal("0.04"),
            dividend_yield=Decimal("0.01"),
            volatility_change=Decimal("0.02"),
            liquidity_change=Decimal("-0.05"),
            scenarios=(
                ScenarioRange(
                    name="bear",
                    probability=Decimal("0.25"),
                    lower_return=Decimal("-0.20"),
                    upper_return=Decimal("-0.05"),
                ),
                ScenarioRange(
                    name="base",
                    probability=Decimal("0.50"),
                    lower_return=Decimal("-0.05"),
                    upper_return=Decimal("0.12"),
                ),
                ScenarioRange(
                    name="bull",
                    probability=Decimal("0.25"),
                    lower_return=Decimal("0.12"),
                    upper_return=Decimal("0.30"),
                ),
            ),
        ),
        conditions=("earnings revisions remain positive",),
        failure_conditions=("revision breadth turns negative",),
        prediction_target="60-day total return",
        evaluation_metrics=("net return", "expected shortfall"),
        data_limitations=("borrow history is scenario based",),
        model_risks=("regime transition",),
    )


def _payoff() -> DesiredEconomicPayoff:
    return DesiredEconomicPayoff(
        underlying_id="underlying:example-equity",
        direction=Direction.LONG,
        horizon_days=60,
        target_notional=Decimal("100000"),
        target_delta=Decimal("100000"),
        target_vega=None,
        target_volatility=Decimal("0.15"),
        maximum_loss=Decimal("100000"),
        maximum_premium=Decimal("20000"),
        tail_protection_required=True,
        bounded_loss_required=True,
        allowed_expression_kinds=(
            ExpressionKind.SPOT,
            ExpressionKind.FUTURE,
            ExpressionKind.CALL_OR_PUT,
            ExpressionKind.OPTION_SPREAD,
            ExpressionKind.SPOT_OPTION,
            ExpressionKind.FUTURE_OPTION,
        ),
    )


def _choice(
    instrument_id: str,
    kind: ProductKind,
    *,
    known_at: datetime = AS_OF,
    option_right: str | None = None,
    strike: Decimal | None = None,
    delta: Decimal | None = None,
) -> InstrumentChoice:
    is_option = kind is ProductKind.OPTION
    return InstrumentChoice(
        instrument_id=instrument_id,
        economic_underlying_id="underlying:example-equity",
        product_kind=kind,
        currency="USD",
        known_at=known_at,
        unit_price=Decimal("5") if is_option else Decimal("100"),
        contract_multiplier=Decimal("100") if is_option else Decimal("1"),
        economic_notional_per_unit=(Decimal("10000") if is_option else Decimal("100")),
        liquidity_score=Decimal("0.80"),
        expected_return=Decimal("0.08"),
        expected_carry=Decimal("0.01"),
        expected_roll_cost=Decimal("0.002"),
        expected_time_value_decay=Decimal("0.01") if is_option else Decimal("0"),
        implied_volatility=Decimal("0.25") if is_option else None,
        transaction_cost=Decimal("20"),
        initial_margin=Decimal("2000"),
        tail_loss=Decimal("0.20"),
        model_sensitivity=Decimal("0.10"),
        data_confidence=Decimal("0.90"),
        expiry=AS_OF + timedelta(days=90) if is_option else None,
        strike=strike,
        delta=delta,
        vega=Decimal("0.15") if is_option else None,
        option_right=option_right,
    )


def _candidate(
    candidate_id: str,
    expression_kind: ExpressionKind,
    choices: tuple[InstrumentChoice, ...],
    *,
    expected_return: Decimal,
    liquidity_score: Decimal = Decimal("0.80"),
) -> ExpressionCandidate:
    directions = tuple(
        Direction.LONG if index == 0 else Direction.SHORT
        for index in range(len(choices))
    )
    return ExpressionCandidate(
        candidate_id=candidate_id,
        expression_kind=expression_kind,
        choices=choices,
        directions=directions,
        roles=tuple(
            LegRole.PRIMARY if index == 0 else LegRole.TAIL_PROTECTION
            for index in range(len(choices))
        ),
        leg_ratios=tuple(Decimal("1") for _ in choices),
        selection_rules=tuple(
            LegSelectionRule(
                product_kind=choice.product_kind,
                minimum_days_to_expiry=30
                if choice.product_kind is ProductKind.OPTION
                else None,
                maximum_days_to_expiry=120
                if choice.product_kind is ProductKind.OPTION
                else None,
                target_delta=choice.delta,
                minimum_liquidity_score=Decimal("0.50"),
                sizing_method="TARGET_NOTIONAL",
            )
            for choice in choices
        ),
        execution_mode=(
            ExecutionMode.SIMULTANEOUS_ATOMIC
            if len(choices) > 1
            else ExecutionMode.COMPLEX_CONSERVATIVE
        ),
        expected_return=expected_return,
        pnl_dispersion=Decimal("0.15"),
        maximum_loss=Decimal("15000"),
        carry=Decimal("0.01"),
        roll_cost=Decimal("0.002"),
        time_value_decay=Decimal("0.01"),
        implied_volatility_cost=Decimal("0.01"),
        liquidity_score=liquidity_score,
        transaction_cost=Decimal("200"),
        margin_required=Decimal("10000"),
        tail_risk=Decimal("0.10"),
        model_sensitivity=Decimal("0.05"),
        data_confidence=Decimal("0.90"),
        targets=StrategyTargets(
            net_delta=Decimal("1"),
            target_notional=Decimal("100000"),
            maximum_premium=Decimal("15000"),
            maximum_loss=Decimal("15000"),
            collateral_limit=Decimal("20000"),
        ),
        legging_risk_limit=Decimal("100"),
        maximum_leg_time_skew_seconds=0,
        allow_partial_fill=False,
    )


def test_candidate_generation_is_point_in_time_and_cross_product() -> None:
    engine = InstrumentExpressionEngine(DEFAULT_EXPRESSION_POLICY)
    spot = _choice("listing:spot", ProductKind.SPOT)
    future = _choice("contract:future", ProductKind.FUTURE)
    call_100 = _choice(
        "contract:call-100",
        ProductKind.OPTION,
        option_right="CALL",
        strike=Decimal("100"),
        delta=Decimal("0.55"),
    )
    call_110 = _choice(
        "contract:call-110",
        ProductKind.OPTION,
        option_right="CALL",
        strike=Decimal("110"),
        delta=Decimal("0.30"),
    )
    future_known_tomorrow = _choice(
        "contract:future-leak",
        ProductKind.FUTURE,
        known_at=AS_OF + timedelta(days=1),
    )

    groups = engine.generate_candidate_groups(
        payoff=_payoff(),
        instruments=(spot, future, call_100, call_110, future_known_tomorrow),
        as_of=AS_OF,
    )

    kinds = {kind for kind, _ in groups}
    assert kinds == {
        ExpressionKind.SPOT,
        ExpressionKind.FUTURE,
        ExpressionKind.CALL_OR_PUT,
        ExpressionKind.OPTION_SPREAD,
        ExpressionKind.SPOT_OPTION,
        ExpressionKind.FUTURE_OPTION,
    }
    assert all(
        choice.instrument_id != "contract:future-leak"
        for _, choices in groups
        for choice in choices
    )


def test_expression_selection_records_every_dimension_and_sizes_real_legs() -> None:
    spot = _choice("listing:spot", ProductKind.SPOT)
    put = _choice(
        "contract:put-90",
        ProductKind.OPTION,
        option_right="PUT",
        strike=Decimal("90"),
        delta=Decimal("-0.25"),
    )
    protected = _candidate(
        "candidate:protected-spot",
        ExpressionKind.SPOT_OPTION,
        (spot, put),
        expected_return=Decimal("0.12"),
    )
    plain = _candidate(
        "candidate:spot",
        ExpressionKind.SPOT,
        (spot,),
        expected_return=Decimal("0.05"),
    )

    decision = InstrumentExpressionEngine(DEFAULT_EXPRESSION_POLICY).select(
        hypothesis=_hypothesis(),
        payoff=_payoff(),
        candidates=(plain, protected),
        as_of=AS_OF,
    )

    assert decision.selected_candidate_id == "candidate:protected-spot"
    assert [leg.instrument_id for leg in decision.selected_legs] == [
        "listing:spot",
        "contract:put-90",
    ]
    assert decision.selected_legs[0].quantity == Decimal("1000")
    assert decision.selected_legs[1].quantity == Decimal("10")
    dimensions = {
        name
        for evaluation in decision.candidate_evaluations
        for name, _ in evaluation.comparison_values
    }
    assert dimensions == {
        "expected_return",
        "pnl_dispersion",
        "maximum_loss",
        "carry",
        "roll_cost",
        "time_value_decay",
        "implied_volatility_cost",
        "liquidity_score",
        "transaction_cost",
        "margin_required",
        "tail_risk",
        "model_sensitivity",
        "data_confidence",
    }
    assert decision.content_hash.startswith("sha256:")


def test_future_knowledge_and_liquidity_fail_closed_with_evidence() -> None:
    leaked = _choice(
        "listing:future-known-spot",
        ProductKind.SPOT,
        known_at=AS_OF + timedelta(seconds=1),
    )
    candidate = _candidate(
        "candidate:invalid",
        ExpressionKind.SPOT,
        (leaked,),
        expected_return=Decimal("1"),
        liquidity_score=Decimal("0.10"),
    )

    decision = InstrumentExpressionEngine(DEFAULT_EXPRESSION_POLICY).select(
        hypothesis=_hypothesis(),
        payoff=replace(_payoff(), tail_protection_required=False),
        candidates=(candidate,),
        as_of=AS_OF,
    )

    assert decision.selected_candidate_id is None
    assert decision.selected_legs == ()
    assert set(decision.failure_evidence) == {
        "FUTURE_KNOWLEDGE",
        "INSUFFICIENT_LIQUIDITY",
    }


def test_hypothesis_is_product_independent_and_probability_is_validated() -> None:
    payload = _hypothesis()
    assert "ticker" not in payload.__dataclass_fields__
    assert "instrument_id" not in payload.__dataclass_fields__

    with pytest.raises(ValueError, match="sum to one"):
        replace(
            payload.distribution,
            scenarios=(
                ScenarioRange(
                    name="only",
                    probability=Decimal("0.9"),
                    lower_return=Decimal("-0.1"),
                    upper_return=Decimal("0.1"),
                ),
            ),
        )
