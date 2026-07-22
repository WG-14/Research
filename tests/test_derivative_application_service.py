from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.derivatives.application import (
    DerivativeApplicationError,
    DerivativeResearchApplicationService,
    FuturesOrderCommand,
    FuturesSettlementCommand,
    FuturesStudyRequest,
    MultiLegStudyRequest,
    OptionLifecycleCommand,
    OptionOrderCommand,
    OptionStudyRequest,
    ReproductionStatus,
    ResearchPreregistration,
)
from market_research.research.derivatives.common import (
    AvailabilityTimes,
    DerivativeDatasetSnapshot,
    DerivativeExperimentSpec,
    DerivativeResearchError,
    InstrumentKind,
)
from market_research.research.derivatives.futures import (
    FuturesLifecycleEvent,
    FuturesOrderIntent,
    LifecycleEventType,
    MarginCallAction,
    OrderSide,
)
from market_research.research.derivatives.options import (
    BlackScholesModel,
    MultiLegExecutionPolicy,
    MultiLegOrder,
    OptionChainSnapshot,
    OptionLeg,
    OptionType,
    PositionSide,
    TransactionSide,
)
from market_research.research.derivatives.simulation_evidence import (
    OptionExecutionMode,
    OptionExecutionPolicy,
    SimulationProductKind,
    futures_fill_model_hash,
)
from market_research.research.research_standard import (
    CompetingHypothesis,
    ExpectedDirection,
    HypothesisRelation,
    HypothesisVersion,
    Mechanism,
    NullHypothesis,
    Observation,
    ResearchQuestion,
    ResearchStatus,
    ResearchTransition,
)
from tests.test_derivative_simulation_evidence import (
    _dataset,
    _option_lifecycle_dataset,
    _quality,
    _spec,
)
from tests.test_futures_derivative_research import (
    HASH_A,
    HASH_B,
    _market_fixture,
    _quote as futures_quote,
    _simulator,
)
from tests.test_options_derivative_research import (
    EXPIRY,
    NOW,
    _contract as option_contract,
    _hash,
    _inputs,
    _quote as option_quote,
    _settlement_input,
)


def _preregistration(kind: InstrumentKind) -> ResearchPreregistration:
    observation = Observation(
        observation_id=f"observation.{kind.value.lower()}.application",
        version=1,
        observed_at="2025-12-01T00:00:00Z",
        recorded_at="2025-12-02T00:00:00Z",
        target_ids=("FUT.ROOT",) if kind is InstrumentKind.FUTURE else ("asset_xyz",),
        dataset_snapshot_hashes=(_hash("1"),),
        available_information_hash=_hash("2"),
        statement="A reproducible derivative pricing pattern was observed.",
        researcher_interpretation="The pattern may reflect a risk premium.",
        uncertainty="The observation may be sample-specific.",
        attachment_hashes=(),
        linked_question_ids=(f"question.{kind.value.lower()}.application",),
        linked_hypothesis_ids=(f"hypothesis.{kind.value.lower()}.application",),
        created_by="researcher.application",
    )
    question = ResearchQuestion(
        research_question_id=f"question.{kind.value.lower()}.application",
        version=1,
        title="Does the derivative risk premium persist?",
        description="Test the mechanism with immutable point-in-time inputs.",
        target_market="FUT.ROOT" if kind is InstrumentKind.FUTURE else "exchange_x",
        target_instrument_types=(kind,),
        research_horizon="six_months",
        research_scope="confirmatory_derivative_study",
        created_by="researcher.application",
        created_at="2025-12-02T00:00:00Z",
        status=ResearchStatus.STRUCTURED,
        observation_hashes=(observation.content_hash,),
    )
    hypothesis_id = f"hypothesis.{kind.value.lower()}.application"
    transition = ResearchTransition(
        subject_id=hypothesis_id,
        from_status=ResearchStatus.EXPLORATORY,
        to_status=ResearchStatus.PREREGISTERED,
        evidence_hashes=(_hash("3"), _hash("4"), _hash("5")),
        recorded_at="2025-12-04T00:00:00Z",
        actor_id="researcher.application",
    )
    hypothesis = HypothesisVersion(
        hypothesis_id=hypothesis_id,
        version=1,
        relation=HypothesisRelation.ORIGINAL,
        parent_version_hashes=(),
        research_question_hash=question.content_hash,
        claim="The risk premium remains positive after realistic costs.",
        expected_direction=ExpectedDirection.POSITIVE,
        target_ids=("FUT.ROOT",) if kind is InstrumentKind.FUTURE else ("asset_xyz",),
        conditions=("normal_market_session",),
        outcome_variables=("net_expectancy",),
        prediction_horizon="six_months",
        mechanism=Mechanism(
            mechanism_id=f"mechanism.{kind.value.lower()}.application",
            version=1,
            causal_chain=("risk_transfer", "required_compensation"),
            assumptions=("quotes_are_point_in_time",),
            observable_implications=("positive_cost_adjusted_expectancy",),
        ),
        null_hypothesis=NullHypothesis(
            null_hypothesis_id=f"null.{kind.value.lower()}.application",
            statement="Net expectancy relative to cash is non-positive.",
            rejection_metric="net_expectancy",
            rejection_threshold="greater_than_zero",
        ),
        competing_hypotheses=(
            CompetingHypothesis(
                competing_hypothesis_id=(f"competing.{kind.value.lower()}.application"),
                statement="The apparent premium is a liquidity artifact.",
                differentiating_predictions=("spread_stress_eliminates_result",),
            ),
        ),
        confounders=("liquidity",),
        falsification_conditions=("non_positive_net_expectancy",),
        required_dataset_kinds=("point_in_time_chain",),
        created_by="researcher.application",
        created_at="2025-12-03T00:00:00Z",
        preregistration_hash=transition.content_hash,
    )
    return ResearchPreregistration(
        observations=(observation,),
        research_question=question,
        hypothesis_version=hypothesis,
        transition=transition,
    )


def test_preregistration_uses_only_hash_bound_transition_chronology() -> None:
    preregistration = _preregistration(InstrumentKind.OPTION)
    tampered_transition = replace(
        preregistration.transition,
        recorded_at="2025-12-05T00:00:00Z",
    )
    with pytest.raises(
        DerivativeResearchError,
        match="research_preregistration_transition_hash_mismatch",
    ):
        replace(preregistration, transition=tampered_transition)

    before_hypothesis = replace(
        preregistration.transition,
        recorded_at="2025-12-02T12:00:00Z",
    )
    matching_hypothesis = replace(
        preregistration.hypothesis_version,
        preregistration_hash=before_hypothesis.content_hash,
    )
    with pytest.raises(
        DerivativeResearchError,
        match="research_preregistration_before_hypothesis",
    ):
        replace(
            preregistration,
            hypothesis_version=matching_hypothesis,
            transition=before_hypothesis,
        )


def test_futures_application_executes_real_domain_steps_and_binds_run() -> None:
    near, _deferred, chain, _later = _market_fixture()
    preregistration = _preregistration(InstrumentKind.FUTURE)
    simulator = _simulator((near,))
    dataset = _dataset(
        instrument=InstrumentKind.FUTURE,
        chain_hash=chain.content_hash,
        universe_ids=tuple(item.contract_id for item in chain.contracts),
    )
    spec = _spec(
        dataset,
        simulation_policy_hash=simulator.content_hash,
        cost_model_hash=simulator.cost_policy.content_hash,
        fill_model_hash=futures_fill_model_hash(simulator),
        hypothesis_hash=preregistration.hypothesis_version.content_hash,
    )
    quote = chain.quote_for(near.contract_id, chain.observed_at)
    order = FuturesOrderIntent(
        intent_id="application.future.open",
        contract_id=near.contract_id,
        side=OrderSide.BUY,
        quantity=1,
        decision_at=quote.observed_at,
    )

    request = FuturesStudyRequest(
        run_id="run.future.application",
        simulation_id="simulation.future.application",
        ledger_id="ledger.future.application",
        started_at=chain.observed_at,
        finished_at="2026-03-10T16:01:00Z",
        initial_cash=Decimal("100000"),
        preregistration=preregistration,
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        simulator=simulator,
        commands=(
            FuturesOrderCommand(
                intent=order,
                fill_id="application.future.fill",
                step_id="application.future.open.step",
            ),
            FuturesSettlementCommand(
                contract_id=near.contract_id,
                as_of=chain.observed_at,
                event_id="application.future.settle",
                step_id="application.future.settle.step",
            ),
        ),
    )
    service = DerivativeResearchApplicationService()
    execution = service.run_futures(request)

    assert execution.run.status == "SUCCEEDED"
    assert execution.simulation.product_kind is SimulationProductKind.FUTURE
    assert execution.preregistration_hash == preregistration.content_hash
    assert len(execution.simulation.simulation_payload["steps"]) == 2

    receipt = service.reproduce_futures(
        request,
        execution,
        reproduction_id="reproduction.future.application",
        verified_at="2026-03-10T16:02:00Z",
    )
    assert receipt.status is ReproductionStatus.PASS
    receipt.require_pass()

    mismatch = service.reproduce_futures(
        replace(request, initial_cash=Decimal("200000")),
        execution,
        reproduction_id="reproduction.future.application.mismatch",
        verified_at="2026-03-10T16:02:00Z",
    )
    assert mismatch.status is ReproductionStatus.FAIL
    assert "simulation_hash" in mismatch.mismatch_fields


def _futures_study_request() -> FuturesStudyRequest:
    near, _deferred, chain, _later = _market_fixture()
    preregistration = _preregistration(InstrumentKind.FUTURE)
    simulator = _simulator((near,))
    dataset = _dataset(
        instrument=InstrumentKind.FUTURE,
        chain_hash=chain.content_hash,
        universe_ids=tuple(item.contract_id for item in chain.contracts),
    )
    spec = _spec(
        dataset,
        simulation_policy_hash=simulator.content_hash,
        cost_model_hash=simulator.cost_policy.content_hash,
        fill_model_hash=futures_fill_model_hash(simulator),
        hypothesis_hash=preregistration.hypothesis_version.content_hash,
    )
    intent = FuturesOrderIntent(
        intent_id="application.future.lineage.open",
        contract_id=near.contract_id,
        side=OrderSide.BUY,
        quantity=1,
        decision_at=chain.observed_at,
    )
    return FuturesStudyRequest(
        run_id="run.future.lineage",
        simulation_id="simulation.future.lineage",
        ledger_id="ledger.future.lineage",
        started_at=chain.observed_at,
        finished_at="2026-03-10T16:01:00Z",
        initial_cash=Decimal("100000"),
        preregistration=preregistration,
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        simulator=simulator,
        commands=(
            FuturesOrderCommand(
                intent=intent,
                fill_id="application.future.lineage.fill",
                step_id="application.future.lineage.step",
            ),
        ),
    )


def test_futures_application_requires_chain_sources_in_admitted_dataset() -> None:
    request = _futures_study_request()
    dataset = replace(
        request.dataset,
        raw_manifest_hashes=tuple(
            value for value in request.dataset.raw_manifest_hashes if value != HASH_A
        ),
    )
    spec = replace(
        request.experiment_spec,
        dataset_snapshot_hash=dataset.content_hash,
    )

    with pytest.raises(
        DerivativeApplicationError, match="derivative_study_chain_source_unbound"
    ):
        DerivativeResearchApplicationService().run_futures(
            replace(request, dataset=dataset, experiment_spec=spec)
        )


def test_futures_study_lifecycle_requires_chain_and_dataset_lineage() -> None:
    request = _futures_study_request()
    event = FuturesLifecycleEvent(
        event_id="application.future.lifecycle.unbound",
        contract_id=request.chain.contracts[0].contract_id,
        event_type=LifecycleEventType.LISTED,
        event_at=request.chain.observed_at,
        availability=AvailabilityTimes(
            event_at=request.chain.observed_at,
            published_at=request.chain.observed_at,
            provider_received_at=request.chain.observed_at,
            system_received_at=request.chain.observed_at,
            processed_at=request.chain.observed_at,
        ),
        source_hash=HASH_B,
    )

    with pytest.raises(
        DerivativeResearchError,
        match="futures_study_lifecycle_source_not_in_chain",
    ):
        replace(request, lifecycle_events=(event,))


def test_futures_study_lifecycle_must_fit_dataset_period_and_knowledge() -> None:
    request = _futures_study_request()
    contract_id = request.chain.contracts[0].contract_id
    outside_period_at = "2025-12-31T16:00:00Z"
    outside_period = FuturesLifecycleEvent(
        event_id="application.future.lifecycle.outside.period",
        contract_id=contract_id,
        event_type=LifecycleEventType.LISTED,
        event_at=outside_period_at,
        availability=AvailabilityTimes(
            event_at=outside_period_at,
            published_at=outside_period_at,
            provider_received_at=outside_period_at,
            system_received_at=outside_period_at,
            processed_at=outside_period_at,
        ),
        source_hash=HASH_A,
    )
    chain_with_outside_period_lifecycle = replace(
        request.chain, lifecycle_events=(outside_period,)
    )
    with pytest.raises(
        DerivativeResearchError,
        match="futures_study_chain_lifecycle_outside_dataset_period",
    ):
        replace(request, chain=chain_with_outside_period_lifecycle)

    with pytest.raises(
        DerivativeResearchError,
        match="futures_study_lifecycle_outside_dataset_period",
    ):
        replace(request, lifecycle_events=(outside_period,))

    delayed_at = "2026-03-10T16:00:01Z"
    delayed = FuturesLifecycleEvent(
        event_id="application.future.lifecycle.delayed",
        contract_id=contract_id,
        event_type=LifecycleEventType.LISTED,
        event_at=request.chain.observed_at,
        availability=AvailabilityTimes(
            event_at=request.chain.observed_at,
            published_at=delayed_at,
            provider_received_at=delayed_at,
            system_received_at=delayed_at,
            processed_at=delayed_at,
        ),
        source_hash=HASH_A,
    )
    with pytest.raises(
        DerivativeResearchError,
        match="futures_study_lifecycle_unknown_at_dataset_knowledge_time",
    ):
        replace(request, lifecycle_events=(delayed,))


def test_futures_application_never_publishes_a_failed_ledger_as_success() -> None:
    near, _deferred, original_chain, _later = _market_fixture()
    stressed_quote = futures_quote(
        near.contract_id,
        original_chain.observed_at,
        "100",
        settlement="2",
    )
    chain = replace(
        original_chain,
        quotes=(stressed_quote, original_chain.quotes[1]),
    )
    preregistration = _preregistration(InstrumentKind.FUTURE)
    simulator = _simulator((near,), margin_action=MarginCallAction.FAIL_RESEARCH)
    dataset = _dataset(
        instrument=InstrumentKind.FUTURE,
        chain_hash=chain.content_hash,
        universe_ids=tuple(item.contract_id for item in chain.contracts),
    )
    spec = _spec(
        dataset,
        simulation_policy_hash=simulator.content_hash,
        cost_model_hash=simulator.cost_policy.content_hash,
        fill_model_hash=futures_fill_model_hash(simulator),
        hypothesis_hash=preregistration.hypothesis_version.content_hash,
    )
    intent = FuturesOrderIntent(
        intent_id="application.future.failed.open",
        contract_id=near.contract_id,
        side=OrderSide.BUY,
        quantity=1,
        decision_at=chain.observed_at,
    )
    request = FuturesStudyRequest(
        run_id="run.future.failed.ledger",
        simulation_id="simulation.future.failed.ledger",
        ledger_id="ledger.future.failed.ledger",
        started_at=chain.observed_at,
        finished_at="2026-03-10T16:01:00Z",
        initial_cash=Decimal("2000"),
        preregistration=preregistration,
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        simulator=simulator,
        commands=(
            FuturesOrderCommand(
                intent=intent,
                fill_id="application.future.failed.fill",
                step_id="application.future.failed.open.step",
            ),
            FuturesSettlementCommand(
                contract_id=near.contract_id,
                as_of=chain.observed_at,
                event_id="application.future.failed.settle",
                step_id="application.future.failed.settle.step",
            ),
        ),
    )

    with pytest.raises(DerivativeApplicationError) as captured:
        DerivativeResearchApplicationService().run_futures(request)

    assert captured.value.failed_run.status == "FAILED"
    assert captured.value.failed_run.failure_code == "futures_ledger_failed"


def _option_application_parts() -> tuple[
    ResearchPreregistration,
    OptionChainSnapshot,
    OptionExecutionPolicy,
    DerivativeDatasetSnapshot,
    DerivativeExperimentSpec,
]:
    contract = option_contract("option_application_call")
    quote = option_quote(contract)
    chain = OptionChainSnapshot(
        chain_snapshot_id="chain.option.application",
        underlying_id=contract.underlying_id,
        knowledge_time=NOW,
        underlying_price=Decimal("100"),
        contracts=(contract,),
        quotes=(quote,),
        source_manifest_hashes=(_hash("d"),),
        quality_results=_quality(),
    )
    policy = OptionExecutionPolicy(
        policy_id="option.execution.application",
        policy_version="v1",
        fill_model_version="recorded.quote.cross.v1",
        mode=OptionExecutionMode.SINGLE,
        fee_per_contract=Decimal("1"),
        slippage_ticks=0,
        allow_partial=False,
        allow_illiquid=False,
    )
    preregistration = _preregistration(InstrumentKind.OPTION)
    dataset = _dataset(
        instrument=InstrumentKind.OPTION,
        chain_hash=chain.content_hash,
        universe_ids=(contract.contract_id,),
    )
    spec = _spec(
        dataset,
        simulation_policy_hash=policy.content_hash,
        cost_model_hash=policy.cost_model_hash,
        fill_model_hash=policy.fill_model_hash,
        hypothesis_hash=preregistration.hypothesis_version.content_hash,
        valuation_model_hash=BlackScholesModel().content_hash,
    )
    return preregistration, chain, policy, dataset, spec


def _option_study_request() -> OptionStudyRequest:
    preregistration, chain, policy, dataset, spec = _option_application_parts()
    contract = chain.contracts[0]
    return OptionStudyRequest(
        run_id="run.option.application.helper",
        simulation_id="simulation.option.application.helper",
        started_at=NOW,
        finished_at="2026-07-03T00:00:00Z",
        preregistration=preregistration,
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        execution_policy=policy,
        valuation_model=BlackScholesModel(),
        orders=(
            OptionOrderCommand(
                order_id="application.option.helper.order",
                position_id="application.option.helper.position",
                contract_id=contract.contract_id,
                side=TransactionSide.BUY,
                quantity=Decimal("1"),
                requested_at=NOW,
                valuation_input=_inputs(contract, chain.quotes[0]),
            ),
        ),
    )


def test_option_application_executes_fill_valuation_and_expiry() -> None:
    preregistration, chain, policy, dataset, spec = _option_application_parts()
    contract = chain.contracts[0]
    quote = chain.quotes[0]
    valuation = _inputs(contract, quote)
    lifecycle_dataset = _option_lifecycle_dataset(dataset)

    execution = DerivativeResearchApplicationService().run_option(
        OptionStudyRequest(
            run_id="run.option.application",
            simulation_id="simulation.option.application",
            started_at=NOW,
            finished_at="2026-07-03T00:00:00Z",
            preregistration=preregistration,
            dataset=dataset,
            experiment_spec=spec,
            chain=chain,
            execution_policy=policy,
            valuation_model=BlackScholesModel(),
            orders=(
                OptionOrderCommand(
                    order_id="application.option.order",
                    position_id="application.option.position",
                    contract_id=contract.contract_id,
                    side=TransactionSide.BUY,
                    quantity=Decimal("1"),
                    requested_at=NOW,
                    valuation_input=valuation,
                    lifecycle=OptionLifecycleCommand(
                        event_id="application.option.expiry",
                        event_at=EXPIRY,
                        settlement_input=_settlement_input(
                            contract, "110", settlement_at=EXPIRY
                        ),
                        observation_dataset_hash=lifecycle_dataset.content_hash,
                    ),
                ),
            ),
            lifecycle_datasets=(lifecycle_dataset,),
        )
    )

    payload = execution.simulation.simulation_payload
    assert execution.simulation.product_kind is SimulationProductKind.OPTION
    assert payload["fills"][0]["price"] == "6"  # type: ignore[index]
    assert payload["lifecycle_events"][0]["event_type"] == "EXPIRY"  # type: ignore[index]
    assert execution.run.observation_dataset_snapshot_hashes == (
        lifecycle_dataset.content_hash,
    )


def test_multileg_application_executes_atomic_legs_and_net_evidence() -> None:
    call = option_contract("option_application_multileg_call")
    put = option_contract("option_application_multileg_put", option_type=OptionType.PUT)
    call_quote = option_quote(call)
    put_quote = option_quote(put)
    chain = OptionChainSnapshot(
        chain_snapshot_id="chain.option.application.multileg",
        underlying_id=call.underlying_id,
        knowledge_time=NOW,
        underlying_price=Decimal("100"),
        contracts=(call, put),
        quotes=(call_quote, put_quote),
        source_manifest_hashes=(_hash("e"),),
        quality_results=_quality(),
    )
    policy = OptionExecutionPolicy(
        policy_id="option.execution.application.multileg",
        policy_version="v1",
        fill_model_version="recorded.quote.atomic.v1",
        mode=OptionExecutionMode.SIMULTANEOUS,
        fee_per_contract=Decimal("0"),
        slippage_ticks=0,
        allow_partial=False,
        allow_illiquid=False,
        maximum_leg_time_skew_seconds=1,
    )
    order = MultiLegOrder(
        group_id="application.multileg.group",
        legs=(
            OptionLeg("application.call.leg", call, PositionSide.LONG, Decimal("1")),
            OptionLeg("application.put.leg", put, PositionSide.SHORT, Decimal("1")),
        ),
        policy=MultiLegExecutionPolicy.SIMULTANEOUS,
        requested_at=NOW,
        maximum_leg_time_skew_seconds=1,
        allow_partial=False,
        execution_policy_hash=policy.content_hash,
    )
    preregistration = _preregistration(InstrumentKind.OPTION)
    dataset = _dataset(
        instrument=InstrumentKind.OPTION,
        chain_hash=chain.content_hash,
        universe_ids=(call.contract_id, put.contract_id),
    )
    spec = _spec(
        dataset,
        simulation_policy_hash=policy.content_hash,
        cost_model_hash=policy.cost_model_hash,
        fill_model_hash=policy.fill_model_hash,
        hypothesis_hash=preregistration.hypothesis_version.content_hash,
        valuation_model_hash=BlackScholesModel().content_hash,
    )

    execution = DerivativeResearchApplicationService().run_multi_leg(
        MultiLegStudyRequest(
            run_id="run.multileg.application",
            simulation_id="simulation.multileg.application",
            started_at=NOW,
            finished_at="2026-07-03T00:00:00Z",
            preregistration=preregistration,
            dataset=dataset,
            experiment_spec=spec,
            chain=chain,
            execution_policy=policy,
            valuation_model=BlackScholesModel(),
            order=order,
            valuation_inputs=(_inputs(call, call_quote), _inputs(put, put_quote)),
            fill_times=(
                ("application.call.leg", NOW),
                ("application.put.leg", NOW),
            ),
        )
    )

    payload = execution.simulation.simulation_payload
    assert execution.simulation.product_kind is SimulationProductKind.MULTI_LEG
    assert payload["multi_leg_execution"]["state"] == "FILLED"  # type: ignore[index]
    assert len(payload["positions"]) == 2  # type: ignore[arg-type]


def test_application_rejects_hypothesis_substitution_before_dataset_use() -> None:
    preregistration, chain, policy, dataset, spec = _option_application_parts()
    contract = chain.contracts[0]
    bad_spec = replace(spec, hypothesis_version_hash=_hash("f"))
    request = OptionStudyRequest(
        run_id="run.option.substitution",
        simulation_id="simulation.option.substitution",
        started_at=NOW,
        finished_at="2026-07-03T00:00:00Z",
        preregistration=preregistration,
        dataset=dataset,
        experiment_spec=bad_spec,
        chain=chain,
        execution_policy=policy,
        valuation_model=BlackScholesModel(),
        orders=(
            OptionOrderCommand(
                order_id="application.option.substitution.order",
                position_id="application.option.substitution.position",
                contract_id=contract.contract_id,
                side=TransactionSide.BUY,
                quantity=Decimal("1"),
                requested_at=NOW,
                valuation_input=_inputs(contract, chain.quotes[0]),
            ),
        ),
    )

    with pytest.raises(
        DerivativeApplicationError,
        match="experiment_hypothesis_version_mismatch",
    ) as captured:
        DerivativeResearchApplicationService().run_option(request)
    assert captured.value.failed_run.status == "FAILED"
    assert (
        captured.value.failed_run.failure_code
        == "experiment_hypothesis_version_mismatch"
    )


def test_execution_failure_is_preserved_as_structured_immutable_run() -> None:
    request = _option_study_request()
    contract = request.chain.contracts[0]
    quote = option_quote(contract, bid=None, ask=None, bid_size="0", ask_size="0")
    bad_chain = replace(request.chain, quotes=(quote,))
    bad_dataset = replace(
        request.dataset, chain_snapshot_hashes=(bad_chain.content_hash,)
    )
    bad_spec = replace(
        request.experiment_spec, dataset_snapshot_hash=bad_dataset.content_hash
    )
    failed_request = replace(
        request,
        run_id="run.option.failed",
        simulation_id="simulation.option.failed",
        dataset=bad_dataset,
        experiment_spec=bad_spec,
        chain=bad_chain,
        orders=(
            replace(
                request.orders[0],
                order_id="application.option.failed.order",
                position_id="application.option.failed.position",
                valuation_input=_inputs(contract, quote),
            ),
        ),
    )
    service = DerivativeResearchApplicationService()
    expected = service.run_option(request)

    with pytest.raises(DerivativeApplicationError) as captured:
        service.run_option(failed_request)

    failed = captured.value.failed_run
    assert failed.status == "FAILED"
    assert failed.failure_code == "option_fill_not_executed"
    assert failed.result_artifact_hash == captured.value.failure_result.content_hash
    assert captured.value.failure_result.run_id == failed.run_id
    assert captured.value.failure_result.event_stream_hash == failed.event_stream_hash
    assert captured.value.failure_result.failure_code == failed.failure_code

    receipt = service.reproduce_option(
        failed_request,
        expected,
        reproduction_id="reproduction.option.failed.application",
        verified_at="2026-07-04T00:00:00Z",
    )
    assert receipt.status is ReproductionStatus.FAIL
    assert receipt.mismatch_fields == ("reproduced_run_failed",)
    assert receipt.reproduced_simulation_hash is None
    assert (
        receipt.reproduced_failure_result_hash
        == captured.value.failure_result.content_hash
    )
    assert receipt.reproduced_run_hash == failed.content_hash


def test_option_application_rejects_unbound_chain_and_valuation_sources() -> None:
    service = DerivativeResearchApplicationService()
    request = _option_study_request()
    bad_chain = replace(request.chain, source_manifest_hashes=(_hash("f"),))
    bad_chain_dataset = replace(
        request.dataset,
        chain_snapshot_hashes=(bad_chain.content_hash,),
    )
    bad_chain_spec = replace(
        request.experiment_spec,
        dataset_snapshot_hash=bad_chain_dataset.content_hash,
    )

    with pytest.raises(
        DerivativeApplicationError,
        match="derivative_study_chain_source_unbound",
    ):
        service.run_option(
            replace(
                request,
                dataset=bad_chain_dataset,
                experiment_spec=bad_chain_spec,
                chain=bad_chain,
            )
        )

    bad_valuation = replace(
        request.orders[0].valuation_input,
        source_manifest_hashes=(_hash("f"),),
    )
    with pytest.raises(
        DerivativeApplicationError,
        match="option_valuation_source_not_in_dataset",
    ):
        service.run_option(
            replace(
                request,
                orders=(replace(request.orders[0], valuation_input=bad_valuation),),
            )
        )


def test_option_application_rejects_chain_and_valuation_outside_dataset_period() -> (
    None
):
    service = DerivativeResearchApplicationService()
    request = _option_study_request()
    shortened_dataset = replace(
        request.dataset,
        period_end="2026-01-02T12:00:09+00:00",
    )
    shortened_spec = replace(
        request.experiment_spec,
        dataset_snapshot_hash=shortened_dataset.content_hash,
    )
    with pytest.raises(
        DerivativeApplicationError,
        match="derivative_study_chain_outside_period",
    ):
        service.run_option(
            replace(
                request,
                dataset=shortened_dataset,
                experiment_spec=shortened_spec,
            )
        )

    later_dataset = replace(
        request.dataset,
        knowledge_time="2026-01-02T12:00:12+00:00",
        period_start="2026-01-02T12:00:11+00:00",
        period_end="2026-01-02T12:00:12+00:00",
    )
    with pytest.raises(
        DerivativeResearchError,
        match="option_valuation_outside_dataset_period",
    ):
        service._validate_valuation_input(
            request.orders[0].valuation_input,
            request.chain.contracts[0].content_hash,
            request.chain.quotes[0].content_hash,
            later_dataset,
        )


def test_option_lifecycle_rejects_settlement_source_outside_dataset() -> None:
    preregistration, chain, policy, dataset, spec = _option_application_parts()
    contract = chain.contracts[0]
    lifecycle_dataset = _option_lifecycle_dataset(dataset)
    request = OptionStudyRequest(
        run_id="run.option.bad.settlement.source",
        simulation_id="simulation.option.bad.settlement.source",
        started_at=NOW,
        finished_at="2026-07-03T00:00:00Z",
        preregistration=preregistration,
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        execution_policy=policy,
        valuation_model=BlackScholesModel(),
        orders=(
            OptionOrderCommand(
                order_id="application.option.bad.settlement.order",
                position_id="application.option.bad.settlement.position",
                contract_id=contract.contract_id,
                side=TransactionSide.BUY,
                quantity=Decimal("1"),
                requested_at=NOW,
                valuation_input=_inputs(contract, chain.quotes[0]),
                lifecycle=OptionLifecycleCommand(
                    event_id="application.option.bad.settlement.expiry",
                    event_at=EXPIRY,
                    settlement_input=_settlement_input(
                        contract,
                        "110",
                        settlement_at=EXPIRY,
                        source_manifest_hash=_hash("f"),
                    ),
                    observation_dataset_hash=lifecycle_dataset.content_hash,
                ),
            ),
        ),
        lifecycle_datasets=(lifecycle_dataset,),
    )

    with pytest.raises(
        DerivativeApplicationError,
        match="option_settlement_input_source_not_in_observation_dataset",
    ):
        DerivativeResearchApplicationService().run_option(request)


def test_option_lifecycle_rejects_settlement_outside_observation_dataset_period() -> (
    None
):
    request = _option_study_request()
    contract = request.chain.contracts[0]
    lifecycle_dataset = replace(
        _option_lifecycle_dataset(request.dataset),
        period_end="2026-07-01T12:00:00+00:00",
    )
    lifecycle = OptionLifecycleCommand(
        event_id="application.option.bad.settlement.period",
        event_at=EXPIRY,
        settlement_input=_settlement_input(
            contract,
            "110",
            settlement_at=EXPIRY,
        ),
        observation_dataset_hash=lifecycle_dataset.content_hash,
    )
    lifecycle_request = replace(
        request,
        run_id="run.option.bad.settlement.period",
        simulation_id="simulation.option.bad.settlement.period",
        orders=(replace(request.orders[0], lifecycle=lifecycle),),
        lifecycle_datasets=(lifecycle_dataset,),
    )

    with pytest.raises(
        DerivativeApplicationError,
        match="option_settlement_input_outside_observation_dataset_period",
    ):
        DerivativeResearchApplicationService().run_option(lifecycle_request)
