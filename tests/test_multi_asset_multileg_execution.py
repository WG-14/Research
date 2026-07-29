from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import pytest

from market_research.research.derivatives.common import AvailabilityTimes
from market_research.research.derivatives.options import (
    ExerciseStyle,
    MultiLegExecutionPolicy,
    MultiLegOrder,
    MultiLegState,
    OptionContract,
    OptionLeg,
    OptionQuote,
    OptionSettlementInput,
    OptionType,
    PhysicalSettlementConvention,
    PositionSide,
    SettlementType,
    simulate_option_lifecycle,
)
from market_research.research.multi_asset.multileg_execution import (
    DynamicExecutionAttempt,
    DynamicExecutionPolicy,
    DynamicMultiLegExecutionPlan,
    DynamicMultiLegExecutionService,
    DynamicTimeInForce,
    ExpressionCompilePolicy,
    ExpressionExecutionCompiler,
    MultiLegDisposition,
    MultiLegLedgerCommand,
    MultiLegLedgerExecutionService,
    SequentialPartialAction,
)
from market_research.research.multi_asset.expression import (
    CandidateEvaluation,
    Direction,
    ExpressionDecision,
    ExpressionLeg,
    LegRole,
    LegSelectionRule,
    ProductKind,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    CashDelta,
    UnifiedPortfolioLedger,
    funding_event,
)


NOW = "2026-01-02T12:00:10+00:00"
UNWIND_AT = "2026-01-02T12:00:11+00:00"
EXPIRY = "2026-07-02T00:00:00+00:00"


def _hash(token: str) -> str:
    return "sha256:" + token * 64


def _availability(
    *,
    event_at: str = "2026-01-02T12:00:00+00:00",
    processed_at: str = "2026-01-02T12:00:04+00:00",
) -> AvailabilityTimes:
    return AvailabilityTimes(
        event_at=event_at,
        published_at=event_at,
        provider_received_at=event_at,
        system_received_at=event_at,
        processed_at=processed_at,
    )


def _contract(
    contract_id: str,
    *,
    option_type: OptionType = OptionType.CALL,
    settlement_type: SettlementType = SettlementType.CASH,
    deliverable_asset_id: str | None = None,
    multiplier: str = "100",
    physical_settlement_convention: PhysicalSettlementConvention | None = None,
    deliverable_quantity_per_contract: str | None = None,
    deliverable_contract_multiplier: str | None = None,
) -> OptionContract:
    return OptionContract(
        contract_id=contract_id,
        underlying_id="asset.xyz",
        option_type=option_type,
        strike=Decimal("100"),
        expiration_at=EXPIRY,
        exercise_style=ExerciseStyle.EUROPEAN,
        settlement_type=settlement_type,
        multiplier=Decimal(multiplier),
        currency="USD",
        exchange="exchange.x",
        listing_at="2026-01-01T00:00:00+00:00",
        last_trade_at=EXPIRY,
        settlement_at="2026-07-02T01:00:00+00:00",
        price_tick=Decimal("0.01"),
        deliverable_asset_id=deliverable_asset_id,
        physical_settlement_convention=physical_settlement_convention,
        deliverable_quantity_per_contract=(
            None
            if deliverable_quantity_per_contract is None
            else Decimal(deliverable_quantity_per_contract)
        ),
        deliverable_contract_multiplier=(
            None
            if deliverable_contract_multiplier is None
            else Decimal(deliverable_contract_multiplier)
        ),
    )


def _quote(
    contract: OptionContract,
    *,
    bid: str = "5.8",
    ask: str = "6",
    bid_size: str = "10",
    ask_size: str = "10",
) -> OptionQuote:
    return OptionQuote(
        quote_id=f"quote.{contract.contract_id}",
        contract_id=contract.contract_id,
        availability=_availability(),
        as_of=NOW,
        bid=Decimal(bid),
        ask=Decimal(ask),
        last=(Decimal(bid) + Decimal(ask)) / Decimal("2"),
        bid_size=Decimal(bid_size),
        ask_size=Decimal(ask_size),
        volume=100,
        open_interest=500,
    )


def _order(
    call: OptionContract,
    put: OptionContract,
    *,
    policy: MultiLegExecutionPolicy,
    quantity: str = "1",
    allow_partial: bool = False,
    maximum_skew: int = 1,
) -> MultiLegOrder:
    return MultiLegOrder(
        group_id=f"group.{policy.value.lower()}.{quantity.replace('.', '_')}",
        legs=(
            OptionLeg(
                "call.leg",
                call,
                PositionSide.LONG,
                Decimal(quantity),
            ),
            OptionLeg(
                "put.leg",
                put,
                PositionSide.SHORT,
                Decimal(quantity),
            ),
        ),
        policy=policy,
        requested_at=NOW,
        maximum_leg_time_skew_seconds=maximum_skew,
        allow_partial=allow_partial,
        execution_policy_hash=_hash("e"),
    )


def _ledger(ledger_id: str) -> UnifiedPortfolioLedger:
    return UnifiedPortfolioLedger.open(
        ledger_id=ledger_id,
        base_currency="USD",
    ).publish(
        funding_event(
            event_id=f"{ledger_id}.funding",
            occurred_at="2026-01-02T11:59:00+00:00",
            cash_deltas=(CashDelta("USD", Decimal("100000")),),
        )
    )


def test_simultaneous_execution_projects_only_authoritative_committed_fills() -> None:
    call = _contract("option.call.atomic")
    put = _contract("option.put.atomic", option_type=OptionType.PUT)
    order = _order(
        call,
        put,
        policy=MultiLegExecutionPolicy.SIMULTANEOUS,
    )
    service = MultiLegLedgerExecutionService()
    opening = _ledger("ledger.multileg.atomic")
    command = MultiLegLedgerCommand(
        execution_id="execution.multileg.atomic",
        order=order,
        quotes=(_quote(call), _quote(put, bid="4", ask="4.2")),
        fill_times=(("call.leg", NOW), ("put.leg", NOW)),
        fee_per_contract=Decimal("1"),
    )

    first = service.execute(command, ledger=opening, fx_rates={"USD": Decimal("1")})
    second = service.execute(command, ledger=opening, fx_rates={"USD": Decimal("1")})

    assert first.authoritative_result.state is MultiLegState.FILLED
    assert first.disposition is MultiLegDisposition.FILLED
    assert first.execution_mode == "SIMULTANEOUS"
    assert len(first.authoritative_result.committed_fills) == 2
    assert len(first.ledger_after.replay().option_positions) == 2
    assert all(item.committed for item in first.fill_evidence)
    assert {item.attempted_fill_hash for item in first.fill_evidence} == {
        item.content_hash for item in first.authoritative_result.committed_fills
    }
    assert first.total_fees == Decimal("2")
    assert first.economic_pnl_delta == Decimal("-2")
    assert first.economic_pnl_delta == first.attributed_pnl_delta
    assert first.execution_context_hash == first.authoritative_result.content_hash
    assert {
        item.execution_context_hash
        for item in first.ledger_after.events
        if item.event_id.startswith(order.group_id)
    } == {first.authoritative_result.content_hash}
    assert first.as_dict() == second.as_dict()

    skewed = MultiLegLedgerCommand(
        execution_id="execution.multileg.atomic.skewed",
        order=order,
        quotes=command.quotes,
        fill_times=(
            ("call.leg", NOW),
            ("put.leg", "2026-01-02T12:00:12+00:00"),
        ),
        fee_per_contract=Decimal("1"),
    )
    rejected = service.execute(
        skewed,
        ledger=opening,
        fx_rates={"USD": Decimal("1")},
    )
    assert rejected.disposition is MultiLegDisposition.ATOMIC_REJECTED
    assert rejected.authoritative_result.attempted_fills
    assert rejected.authoritative_result.committed_fills == ()
    assert rejected.ledger_after.content_hash == opening.content_hash
    assert rejected.ledger_event_hashes == ()


def test_sequential_partial_fill_is_unwound_or_explicitly_retained() -> None:
    call = _contract("option.call.sequential")
    put = _contract("option.put.sequential", option_type=OptionType.PUT)
    order = _order(
        call,
        put,
        policy=MultiLegExecutionPolicy.SEQUENTIAL,
        quantity="2",
        allow_partial=True,
    )
    quotes = (
        _quote(call, bid_size="10", ask_size="1"),
        _quote(put, bid="4", ask="4.2"),
    )
    fill_times = (("call.leg", NOW), ("put.leg", NOW))
    service = MultiLegLedgerExecutionService()
    opening = _ledger("ledger.multileg.sequential")

    retained = service.execute(
        MultiLegLedgerCommand(
            execution_id="execution.multileg.retained",
            order=order,
            quotes=quotes,
            fill_times=fill_times,
            sequential_partial_action=(SequentialPartialAction.RETAIN_EXPOSURE),
        ),
        ledger=opening,
        fx_rates={"USD": Decimal("1")},
    )
    retained_positions = {
        item.instrument_id: item.quantity
        for item in retained.ledger_after.replay().option_positions
    }
    assert retained.authoritative_result.state is MultiLegState.PARTIAL
    assert retained.disposition is MultiLegDisposition.RETAINED_EXPOSURE
    assert retained.retained_exposure_contract_ids == tuple(
        sorted(retained.authoritative_result.legging_exposure_contract_ids)
    )
    assert retained_positions == {
        call.contract_id: Decimal("1"),
        put.contract_id: Decimal("-2"),
    }

    unwound = service.execute(
        MultiLegLedgerCommand(
            execution_id="execution.multileg.unwound",
            order=order,
            quotes=quotes,
            fill_times=fill_times,
            fee_per_contract=Decimal("0.5"),
            sequential_partial_action=SequentialPartialAction.UNWIND,
            unwind_at=UNWIND_AT,
            unwind_fee_per_contract=Decimal("0.5"),
        ),
        ledger=opening,
        fx_rates={"USD": Decimal("1")},
    )
    assert unwound.authoritative_result.state is MultiLegState.PARTIAL
    assert unwound.unwind_result is not None
    assert unwound.unwind_result.state is MultiLegState.UNWOUND
    assert unwound.disposition is MultiLegDisposition.UNWOUND
    assert unwound.retained_exposure_contract_ids == ()
    assert unwound.ledger_after.replay().option_positions == ()
    assert unwound.total_fees == Decimal("3")
    assert unwound.economic_pnl_delta == unwound.attributed_pnl_delta
    assert unwound.economic_pnl_delta < -unwound.total_fees

    partially_unwound = service.execute(
        MultiLegLedgerCommand(
            execution_id="execution.multileg.partial.unwind",
            order=order,
            quotes=(
                _quote(
                    call,
                    bid_size="0.5",
                    ask_size="1",
                ),
                _quote(put, bid="4", ask="4.2"),
            ),
            fill_times=fill_times,
            sequential_partial_action=SequentialPartialAction.UNWIND,
            unwind_at=UNWIND_AT,
        ),
        ledger=opening,
        fx_rates={"USD": Decimal("1")},
    )
    assert partially_unwound.unwind_result is not None
    assert partially_unwound.unwind_result.state is MultiLegState.PARTIAL
    assert partially_unwound.disposition is MultiLegDisposition.UNWIND_FAILED_RETAINED
    assert partially_unwound.retained_exposure_contract_ids == (call.contract_id,)
    assert {item.contract.contract_id for item in partially_unwound.positions} == {
        call.contract_id
    }
    assert {
        item.instrument_id
        for item in partially_unwound.ledger_after.replay().option_positions
    } == {call.contract_id}


def test_physical_future_option_lifecycle_creates_future_in_same_ledger() -> None:
    future_id = "future.asset.xyz.sep26"
    future_option = _contract(
        "option.on.future.call",
        settlement_type=SettlementType.PHYSICAL,
        deliverable_asset_id=future_id,
        multiplier="100",
        physical_settlement_convention=(
            PhysicalSettlementConvention.FUTURE_POSITION_NO_PRINCIPAL
        ),
        deliverable_quantity_per_contract="2",
        deliverable_contract_multiplier="50",
    )
    hedge_option = _contract(
        "option.cash.hedge.put",
        option_type=OptionType.PUT,
        multiplier="1",
    )
    order = _order(
        future_option,
        hedge_option,
        policy=MultiLegExecutionPolicy.SIMULTANEOUS,
    )
    service = MultiLegLedgerExecutionService()
    execution = service.execute(
        MultiLegLedgerCommand(
            execution_id="execution.future.option",
            order=order,
            quotes=(_quote(future_option), _quote(hedge_option)),
            fill_times=(("call.leg", NOW), ("put.leg", NOW)),
        ),
        ledger=_ledger("ledger.future.option"),
        fx_rates={"USD": Decimal("1")},
    )
    position = execution.position_for_contract(future_option.contract_id)
    settlement_input = OptionSettlementInput(
        settlement_input_id="settlement.future.option",
        contract_id=future_option.contract_id,
        settlement_at=EXPIRY,
        availability=_availability(
            event_at=EXPIRY,
            processed_at=EXPIRY,
        ),
        spot_price=Decimal("120"),
        source_manifest_hash=_hash("a"),
    )
    lifecycle = simulate_option_lifecycle(
        position,
        event_id="lifecycle.future.option",
        event_at=EXPIRY,
        settlement_input=settlement_input,
    )

    projected = service.project_lifecycle(
        execution,
        events=(lifecycle,),
        deliverable_asset_classes={future_id: AssetClass.FUTURE},
    )
    snapshot = projected.ledger_after.replay()

    assert future_option.contract_id not in {
        item.instrument_id for item in snapshot.option_positions
    }
    assert {
        (item.instrument_id, item.asset_class, item.quantity)
        for item in snapshot.futures_positions
    } == {(future_id, AssetClass.FUTURE, Decimal("2"))}
    delivered_future = snapshot.futures_positions[0]
    assert delivered_future.average_price == Decimal("100")
    assert delivered_future.mark_price == Decimal("120")
    assert delivered_future.multiplier == Decimal("50")
    assert delivered_future.unrealized_pnl == Decimal("2000")
    assert lifecycle.cash_delta == Decimal("0")
    assert projected.future_position_ids == (future_id,)
    assert projected.ledger_before_hash == execution.ledger_after.content_hash
    assert projected.lifecycle_events == (lifecycle,)
    lifecycle_ledger_event = projected.ledger_after.events[-1]
    assert lifecycle_ledger_event.deliverable_asset_class is AssetClass.FUTURE
    assert lifecycle_ledger_event.deliverable_multiplier == Decimal("50")
    assert lifecycle_ledger_event.source_hashes == (lifecycle.content_hash,)


def test_expression_compiler_prevents_leg_bypass_and_dynamic_aon_retries() -> None:
    call = _contract("option.call.compiled")
    put = _contract("option.put.compiled", option_type=OptionType.PUT)
    rule = LegSelectionRule(
        product_kind=ProductKind.OPTION,
        minimum_days_to_expiry=1,
        maximum_days_to_expiry=365,
        minimum_liquidity_score=Decimal("0.5"),
    )
    decision = ExpressionDecision(
        hypothesis_hash=_hash("h"),
        payoff_hash=_hash("p"),
        policy_hash=_hash("r"),
        as_of=datetime.fromisoformat(NOW),
        candidate_evaluations=(
            CandidateEvaluation(
                candidate_id="candidate.compiled",
                feasible=True,
                rejection_reasons=(),
                comparison_values=(),
                score=Decimal("1"),
            ),
        ),
        selected_candidate_id="candidate.compiled",
        selected_legs=(
            ExpressionLeg(
                selection_rule=rule,
                instrument_id=call.contract_id,
                direction=Direction.LONG,
                quantity=Decimal("1"),
                ratio=Decimal("1"),
                currency="USD",
                role=LegRole.PRIMARY,
            ),
            ExpressionLeg(
                selection_rule=rule,
                instrument_id=put.contract_id,
                direction=Direction.SHORT,
                quantity=Decimal("1"),
                ratio=Decimal("1"),
                currency="USD",
                role=LegRole.HEDGE,
            ),
        ),
        failure_evidence=(),
    )
    compiler = ExpressionExecutionCompiler(
        ExpressionCompilePolicy(
            compiler_id="compiler.option",
            version="1",
            execution_policy=MultiLegExecutionPolicy.SIMULTANEOUS,
            maximum_leg_time_skew_seconds=1,
            allow_partial=False,
            sequential_partial_action=SequentialPartialAction.UNWIND,
        )
    )

    def compile_attempt(
        execution_id: str,
        *,
        fill_at: str,
        ask_size: str,
    ):
        quotes = {
            call.contract_id: _quote(call, ask_size=ask_size),
            put.contract_id: _quote(
                put, bid="4", ask="4.2", ask_size=ask_size, bid_size=ask_size
            ),
        }
        return compiler.compile(
            execution_id=execution_id,
            decision=decision,
            contracts={call.contract_id: call, put.contract_id: put},
            quotes=quotes,
            fill_times={
                f"{execution_id}.leg.1": fill_at,
                f"{execution_id}.leg.2": fill_at,
            },
        )

    first = compile_attempt(
        "execution.compiler.first",
        fill_at=NOW,
        ask_size="0.5",
    )
    second_time = "2026-01-02T12:00:12+00:00"
    second = compile_attempt(
        "execution.compiler.second",
        fill_at=second_time,
        ask_size="10",
    )
    assert tuple(item.quantity for item in first.command.order.legs) == tuple(
        item.quantity for item in decision.selected_legs
    )
    assert first.decision_hash == decision.content_hash

    with pytest.raises(ValueError, match="contract_coverage_mismatch"):
        compiler.compile(
            execution_id="execution.compiler.bypass",
            decision=decision,
            contracts={call.contract_id: call},
            quotes={
                call.contract_id: _quote(call),
                put.contract_id: _quote(put),
            },
            fill_times={
                "execution.compiler.bypass.leg.1": NOW,
                "execution.compiler.bypass.leg.2": NOW,
            },
        )

    policy = DynamicExecutionPolicy(
        policy_id="dynamic.aon.retry",
        version="1",
        time_in_force=DynamicTimeInForce.ALL_OR_NONE,
        maximum_attempts=2,
        timeout_seconds=10,
        retry_delay_seconds=1,
        accept_partial=False,
        maximum_interleg_move_bps=Decimal("100"),
    )
    plan = DynamicMultiLegExecutionPlan(
        plan_id="plan.compiler.retry",
        decision_hash=decision.content_hash,
        policy=policy,
        attempts=(
            DynamicExecutionAttempt(
                attempt_number=1,
                compiled=first,
                market_state_hash=_hash("a"),
                quote_state_hash=_hash("b"),
            ),
            DynamicExecutionAttempt(
                attempt_number=2,
                compiled=second,
                market_state_hash=_hash("c"),
                quote_state_hash=_hash("d"),
            ),
        ),
    )
    rebound_plan = replace(
        plan,
        attempts=(
            replace(plan.attempts[0], market_state_hash=_hash("e")),
            plan.attempts[1],
        ),
    )
    assert rebound_plan.content_hash != plan.content_hash
    result = DynamicMultiLegExecutionService().execute(
        plan,
        ledger=_ledger("ledger.compiler.retry"),
        fx_rates={"USD": Decimal("1")},
    )
    assert len(result.attempts) == 2
    assert result.attempts[0].disposition is MultiLegDisposition.ATOMIC_REJECTED
    assert result.final_execution.disposition is MultiLegDisposition.FILLED
    assert result.attempts[0].ledger_before_hash == (
        result.attempts[0].ledger_after_hash
    )
    assert (
        result.content_hash
        == DynamicMultiLegExecutionService()
        .execute(
            plan,
            ledger=_ledger("ledger.compiler.retry"),
            fx_rates={"USD": Decimal("1")},
        )
        .content_hash
    )

    changed_decision = replace(
        decision,
        selected_legs=(
            replace(decision.selected_legs[0], quantity=Decimal("2")),
            decision.selected_legs[1],
        ),
    )
    changed = compiler.compile(
        execution_id="execution.compiler.changed",
        decision=changed_decision,
        contracts={call.contract_id: call, put.contract_id: put},
        quotes={
            call.contract_id: _quote(call),
            put.contract_id: _quote(put),
        },
        fill_times={
            "execution.compiler.changed.leg.1": second_time,
            "execution.compiler.changed.leg.2": second_time,
        },
    )
    with pytest.raises(ValueError, match="decision_binding_mismatch"):
        DynamicMultiLegExecutionPlan(
            plan_id="plan.compiler.bypass",
            decision_hash=decision.content_hash,
            policy=policy,
            attempts=(
                DynamicExecutionAttempt(
                    attempt_number=1,
                    compiled=first,
                    market_state_hash=_hash("a"),
                    quote_state_hash=_hash("b"),
                ),
                DynamicExecutionAttempt(
                    attempt_number=2,
                    compiled=changed,
                    market_state_hash=_hash("c"),
                    quote_state_hash=_hash("d"),
                ),
            ),
        )
