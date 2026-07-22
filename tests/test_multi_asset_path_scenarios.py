from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from market_research.research.multi_asset.market_state import (
    MarketState,
    ObservationMetadata,
    SpotQuote,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    CashDelta,
    PortfolioEventDraft,
    PortfolioEventType,
    PortfolioSnapshot,
    UnifiedPortfolioLedger,
    funding_event,
    trade_event,
)
from market_research.research.multi_asset.scenarios import (
    JointMarketShock,
    PathRiskLimits,
    PathScenarioEngine,
    PathShockStep,
    PathStressScenario,
    ScenarioError,
)


_HASH_1 = "sha256:" + ("1" * 64)
_HASH_2 = "sha256:" + ("2" * 64)
_T0 = "2026-06-01T09:00:00+00:00"


def _snapshot() -> PortfolioSnapshot:
    ledger = UnifiedPortfolioLedger.open(
        ledger_id="portfolio.path.stress",
        base_currency="USD",
    ).publish_many(
        (
            funding_event(
                event_id="path.funding",
                occurred_at=_T0,
                cash_deltas=(CashDelta("USD", Decimal("10000")),),
            ),
            trade_event(
                event_id="path.spot",
                occurred_at="2026-06-01T09:30:00+00:00",
                asset_class=AssetClass.SPOT,
                instrument_id="AAPL",
                currency="USD",
                quantity_delta=Decimal("10"),
                price=Decimal("100"),
            ),
            trade_event(
                event_id="path.future",
                occurred_at="2026-06-01T09:31:00+00:00",
                asset_class=AssetClass.FUTURE,
                instrument_id="ESM6",
                currency="USD",
                quantity_delta=Decimal("1"),
                price=Decimal("4000"),
            ),
            PortfolioEventDraft(
                event_id="path.margin",
                event_type=PortfolioEventType.MARGIN_REQUIREMENT,
                occurred_at="2026-06-01T09:32:00+00:00",
                currency="USD",
                instrument_id="ESM6",
                asset_class=AssetClass.FUTURE,
                margin_requirement=Decimal("1000"),
            ),
        )
    )
    return ledger.replay()


def _market_state() -> MarketState:
    metadata = ObservationMetadata(
        observed_at="2026-06-01T11:00:00+00:00",
        knowledge_at="2026-06-01T11:00:00+00:00",
        source_hash=_HASH_1,
        calendar_id="XNYS",
        max_age_seconds=0,
    )
    return MarketState(
        state_id="market.state.path.stress",
        valuation_at="2026-06-01T11:00:00+00:00",
        base_currency="USD",
        calendar_ids=("XNYS",),
        spots=(
            SpotQuote(
                instrument_id="AAPL",
                price=Decimal("100"),
                currency="USD",
                unit="USD_per_share",
                metadata=metadata,
            ),
        ),
    )


def _scenario(
    state: MarketState,
    snapshot: PortfolioSnapshot,
    *,
    first_effective_at: str = "2026-06-02T11:00:00+00:00",
    second_effective_at: str = "2026-06-03T11:00:00+00:00",
    second_sequence: int = 2,
    second_predecessor: str | None = None,
) -> PathStressScenario:
    first = PathShockStep(
        sequence=1,
        step_id="initial.selloff",
        effective_at=first_effective_at,
        predecessor_hash=state.state_hash(),
        shock=JointMarketShock(
            scenario_id="path.increment.1",
            price_returns=(("AAPL", Decimal("-0.10")),),
            liquidity_haircuts=(("AAPL", Decimal("0.10")),),
            liquidity_cost_multiplier=Decimal("2"),
            margin_multiplier=Decimal("1.5"),
            source_hashes=(_HASH_1,),
        ),
    )
    second = PathShockStep(
        sequence=second_sequence,
        step_id="liquidity.spiral",
        effective_at=second_effective_at,
        predecessor_hash=(
            first.content_hash if second_predecessor is None else second_predecessor
        ),
        shock=JointMarketShock(
            scenario_id="path.increment.2",
            price_returns=(("AAPL", Decimal("-0.50")),),
            liquidity_haircuts=(("AAPL", Decimal("0.20")),),
            liquidity_cost_multiplier=Decimal("3"),
            margin_multiplier=Decimal("2"),
            source_hashes=(_HASH_2,),
        ),
    )
    return PathStressScenario(
        path_id="equity.margin.liquidity.path",
        expected_base_state_hash=state.state_hash(),
        expected_ledger_hash=snapshot.ledger_hash,
        steps=(first, second),
        risk_limits=PathRiskLimits(
            maximum_drawdown_fraction=Decimal("0.05"),
            minimum_margin_surplus=Decimal("6500"),
            minimum_liquidity_surplus=Decimal("5500"),
        ),
    )


def test_path_scenario_compounds_shocks_and_records_breach_evidence() -> None:
    state = _market_state()
    snapshot = _snapshot()
    scenario = _scenario(state, snapshot)
    original_state_hash = state.state_hash()
    engine = PathScenarioEngine(max_steps=2)

    result = engine.evaluate(
        snapshot,
        market_state=state,
        scenario=scenario,
        base_liquidation_costs={"AAPL": Decimal("100")},
    )

    first, second = result.steps
    assert first.predecessor_result_hash == result.chain_root_hash
    assert second.predecessor_result_hash == first.content_hash
    assert first.prior_state_hash == original_state_hash
    assert second.prior_state_hash == first.scenario_result.shocked_state_hash
    assert first.scenario_result.shocked_state.valuation_at == (
        "2026-06-02T11:00:00+00:00"
    )
    assert second.scenario_result.shocked_state.valuation_at == (
        "2026-06-03T11:00:00+00:00"
    )

    # Incremental returns compound (100 * .9 * .5), while cumulative
    # liquidation haircuts compound as 1 - (.9 * .8) = .28.
    assert second.scenario_result.shocked_state.price_for("AAPL") == Decimal("45")
    by_instrument = {
        item.instrument_id: item for item in second.scenario_result.position_results
    }
    assert by_instrument["AAPL"].shocked_mark == Decimal("32.40")
    assert first.scenario_result.liquidity_reserve == Decimal("200")
    assert second.scenario_result.liquidity_reserve == Decimal("600")
    assert first.scenario_result.shocked_valuation.nav == Decimal("9810")
    assert second.scenario_result.shocked_valuation.nav == Decimal("9324.00")
    assert first.period_nav_change == Decimal("-190")
    assert second.period_nav_change == Decimal("-486.00")
    assert second.cumulative_nav_change == Decimal("-676.00")

    assert not first.risk_evidence.any_breach
    assert second.risk_evidence.drawdown_amount == Decimal("676.00")
    assert second.risk_evidence.drawdown_fraction == Decimal("0.0676")
    assert second.risk_evidence.margin_surplus == Decimal("6000")
    assert second.risk_evidence.margin_headroom == Decimal("-500")
    assert second.risk_evidence.liquidity_surplus == Decimal("5400")
    assert second.risk_evidence.liquidity_headroom == Decimal("-100")
    assert second.risk_evidence.funding_requirement == Decimal("500")
    assert second.risk_evidence.any_breach
    assert result.maximum_drawdown_fraction == Decimal("0.0676")
    assert result.maximum_funding_requirement == Decimal("500")
    assert result.first_drawdown_breach_step_id == "liquidity.spiral"
    assert result.first_margin_breach_step_id == "liquidity.spiral"
    assert result.first_liquidity_breach_step_id == "liquidity.spiral"
    assert result.any_breach
    assert result.original_state_unchanged
    assert state.state_hash() == original_state_hash

    repeated = engine.evaluate(
        snapshot,
        market_state=state,
        scenario=scenario,
        base_liquidation_costs={"AAPL": Decimal("100")},
    )
    assert repeated.content_hash == result.content_hash
    with pytest.raises(FrozenInstanceError):
        result.steps[0].step_id = "mutation"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("second_sequence", "second_predecessor", "error"),
    (
        (3, None, "path_step_sequence_gap"),
        (2, _HASH_2, "path_step_hash_chain_broken"),
    ),
)
def test_path_definition_rejects_sequence_or_hash_chain_discontinuity(
    second_sequence: int,
    second_predecessor: str | None,
    error: str,
) -> None:
    with pytest.raises(ScenarioError, match=error):
        _scenario(
            _market_state(),
            _snapshot(),
            second_sequence=second_sequence,
            second_predecessor=second_predecessor,
        )


def test_path_definition_and_engine_fail_closed_on_chronology_and_bound() -> None:
    state = _market_state()
    snapshot = _snapshot()
    with pytest.raises(ScenarioError, match="path_step_chronology_not_strict"):
        _scenario(
            state,
            snapshot,
            first_effective_at="2026-06-02T11:00:00+00:00",
            second_effective_at="2026-06-02T11:00:00+00:00",
        )

    before_market = _scenario(
        state,
        snapshot,
        first_effective_at="2026-06-01T10:00:00+00:00",
    )
    with pytest.raises(ScenarioError, match="path_engine_step_before_market_state"):
        PathScenarioEngine().evaluate(
            snapshot,
            market_state=state,
            scenario=before_market,
        )

    with pytest.raises(ScenarioError, match="path_engine_step_limit_exceeded"):
        PathScenarioEngine(max_steps=1).evaluate(
            snapshot,
            market_state=state,
            scenario=_scenario(state, snapshot),
        )
    with pytest.raises(ScenarioError, match="path_engine_max_steps_invalid"):
        PathScenarioEngine(max_steps=1025)


def test_path_result_rejects_tampered_state_chain() -> None:
    state = _market_state()
    snapshot = _snapshot()
    result = PathScenarioEngine().evaluate(
        snapshot,
        market_state=state,
        scenario=_scenario(state, snapshot),
        base_liquidation_costs={"AAPL": Decimal("100")},
    )
    tampered_second = replace(result.steps[1], prior_state_hash=_HASH_2)

    with pytest.raises(ScenarioError, match="path_result_state_chain_broken"):
        replace(result, steps=(result.steps[0], tampered_second))


def test_path_shock_rejects_unheld_target_without_partial_evidence() -> None:
    state = _market_state()
    snapshot = _snapshot()
    first = PathShockStep(
        sequence=1,
        step_id="bad.target",
        effective_at="2026-06-02T11:00:00+00:00",
        predecessor_hash=state.state_hash(),
        shock=JointMarketShock(
            scenario_id="bad.target.increment",
            price_returns=(("NOT.HELD", Decimal("-0.1")),),
        ),
    )
    scenario = PathStressScenario(
        path_id="bad.target.path",
        expected_base_state_hash=state.state_hash(),
        expected_ledger_hash=snapshot.ledger_hash,
        steps=(first,),
        risk_limits=PathRiskLimits(maximum_drawdown_fraction=Decimal("0.1")),
    )

    with pytest.raises(ScenarioError, match="path_price_target_not_held"):
        PathScenarioEngine().evaluate(
            snapshot,
            market_state=state,
            scenario=scenario,
        )
