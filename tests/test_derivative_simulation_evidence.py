from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from market_research.research.derivatives.common import (
    DerivativeDatasetFilterContract,
    DerivativeDatasetSnapshot,
    DerivativeExperimentRun,
    DerivativeExperimentSpec,
    FuturesDatasetFilterContract,
    InstrumentKind,
    OptionDatasetFilterContract,
    QualityDecision,
    QualityResult,
    RunType,
)
from market_research.research.derivatives.futures import (
    FuturesLedger,
    FuturesOrderIntent,
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
    execute_multi_leg_order,
    mark_option_position,
    position_from_fill,
    simulate_option_fill,
    simulate_option_lifecycle,
)
from market_research.research.derivatives.simulation_evidence import (
    DerivativeSimulationEvidence,
    OptionExecutionMode,
    OptionExecutionPolicy,
    OptionOrderIntentEvidence,
    SimulationEvidenceError,
    SimulationProductKind,
    futures_fill_model_hash,
)
from tests.test_futures_derivative_research import _market_fixture, _simulator
from tests.test_options_derivative_research import (
    EXPIRY,
    NOW,
    _contract as option_contract,
    _hash,
    _inputs,
    _quote as option_quote,
)


def _quality() -> tuple[QualityResult, ...]:
    return (
        QualityResult(
            check_id="simulation.source.complete",
            check_version="v1",
            decision=QualityDecision.PASS,
        ),
    )


def _dataset(
    *,
    instrument: InstrumentKind,
    chain_hash: str,
    universe_ids: tuple[str, ...],
    feature_hash: str = _hash("3"),
) -> DerivativeDatasetSnapshot:
    filter_contract: DerivativeDatasetFilterContract
    if instrument is InstrumentKind.FUTURE:
        filter_contract = FuturesDatasetFilterContract(
            contract_selection_policy_hash=_hash("0"),
            missing_data_policy_hash=_hash("1"),
            liquidity_policy_hash=_hash("2"),
            exclusion_policy_hash=_hash("3"),
            availability_policy_hash=_hash("4"),
            revision_policy_hash=_hash("5"),
            roll_policy_hash=_hash("6"),
            settlement_policy_hash=_hash("7"),
            margin_policy_hash=_hash("8"),
            contract_spec_history_hash=_hash("9"),
            continuous_series_policy_hash=_hash("a"),
        )
    else:
        filter_contract = OptionDatasetFilterContract(
            chain_selection_policy_hash=_hash("0"),
            expiry_selection_policy_hash=_hash("1"),
            strike_selection_policy_hash=_hash("2"),
            quote_state_policy_hash=_hash("3"),
            missing_data_policy_hash=_hash("4"),
            liquidity_policy_hash=_hash("5"),
            exclusion_policy_hash=_hash("6"),
            availability_policy_hash=_hash("7"),
            revision_policy_hash=_hash("8"),
            rate_curve_policy_hash=_hash("9"),
            dividend_policy_hash=_hash("a"),
            valuation_policy_hash=_hash("b"),
            contract_adjustment_history_hash=_hash("c"),
            stale_threshold_seconds=Decimal("60"),
        )
    return DerivativeDatasetSnapshot(
        snapshot_id=f"dataset.{instrument.value.lower()}.simulation",
        instrument_kind=instrument,
        knowledge_time="2026-03-10T16:00:00Z"
        if instrument is InstrumentKind.FUTURE
        else NOW,
        raw_manifest_hashes=(_hash("1"),),
        normalized_dataset_hash=_hash("2"),
        chain_snapshot_hashes=(chain_hash,),
        feature_definition_hashes=(feature_hash,),
        calendar_hash=_hash("4"),
        policy_hashes=(filter_contract.content_hash,),
        quality_results=_quality(),
        universe_ids=universe_ids,
        period_start="2026-01-01T00:00:00Z",
        period_end="2026-03-10T15:00:00Z"
        if instrument is InstrumentKind.FUTURE
        else "2026-01-02T11:00:00Z",
        filter_contract=filter_contract,
    )


def _spec(
    dataset: DerivativeDatasetSnapshot,
    *,
    simulation_policy_hash: str,
    cost_model_hash: str,
    fill_model_hash: str,
    hypothesis_hash: str = _hash("6"),
) -> DerivativeExperimentSpec:
    return DerivativeExperimentSpec(
        experiment_id=f"experiment.{dataset.instrument_kind.value.lower()}.simulation",
        hypothesis_version_hash=hypothesis_hash,
        dataset_snapshot_hash=dataset.content_hash,
        feature_version_hashes=dataset.feature_definition_hashes,
        run_type=RunType.CONFIRMATORY,
        signal_policy_hash=_hash("7"),
        simulation_policy_hash=simulation_policy_hash,
        cost_model_hash=cost_model_hash,
        fill_model_hash=fill_model_hash,
        position_sizing_hash=_hash("8"),
        metric_policy_hash=_hash("9"),
        acceptance_policy_hash=_hash("a"),
        robustness_policy_hash=_hash("b"),
        random_seed=1729,
        frozen_at="2026-01-01T00:00:00Z",
        code_version="test.v1",
        environment_hash=_hash("c"),
        dirty_worktree=False,
    )


def _run(evidence: DerivativeSimulationEvidence) -> DerivativeExperimentRun:
    return DerivativeExperimentRun(
        run_id=f"run.{evidence.product_kind.value.lower()}.simulation",
        experiment_spec_hash=evidence.experiment_spec_hash,
        dataset_snapshot_hash=evidence.dataset_snapshot_hash,
        started_at="2026-03-10T15:59:00Z",
        finished_at="2026-07-03T00:00:00Z",
        status="SUCCEEDED",
        event_stream_hash=evidence.event_stream_hash,
        result_artifact_hash=evidence.content_hash,
    )


def _futures_evidence(
    *, hypothesis_hash: str = _hash("6"), feature_hash: str = _hash("3")
) -> DerivativeSimulationEvidence:
    near, _deferred, chain, _later = _market_fixture()
    simulator = _simulator((near,))
    dataset = _dataset(
        instrument=InstrumentKind.FUTURE,
        chain_hash=chain.content_hash,
        universe_ids=tuple(item.contract_id for item in chain.contracts),
        feature_hash=feature_hash,
    )
    spec = _spec(
        dataset,
        simulation_policy_hash=simulator.content_hash,
        cost_model_hash=simulator.cost_policy.content_hash,
        fill_model_hash=futures_fill_model_hash(simulator),
        hypothesis_hash=hypothesis_hash,
    )
    quote = chain.quote_for(near.contract_id, chain.observed_at)
    order = FuturesOrderIntent(
        intent_id="intent.simulation.evidence",
        contract_id=near.contract_id,
        side=OrderSide.BUY,
        quantity=1,
        decision_at=quote.observed_at,
    )
    opened = simulator.execute(
        FuturesLedger.open("ledger.simulation.evidence", Decimal("100000")),
        order,
        quote,
        fill_id="fill.simulation.evidence",
        step_id="step.simulation.open",
    )
    settled = simulator.settle_daily(
        opened.ledger,
        quote,
        event_id="settlement.simulation.evidence",
        step_id="step.simulation.settle",
    )
    return DerivativeSimulationEvidence.from_futures(
        simulation_id="simulation.future.representative",
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        simulator=simulator,
        orders=(order,),
        steps=(opened, settled),
    )


def _single_option_evidence(
    *, hypothesis_hash: str = _hash("6"), feature_hash: str = _hash("3")
) -> DerivativeSimulationEvidence:
    contract = option_contract("option_simulation_call")
    quote = option_quote(contract)
    chain = OptionChainSnapshot(
        chain_snapshot_id="chain.option.simulation",
        underlying_id=contract.underlying_id,
        knowledge_time=NOW,
        underlying_price=Decimal("100"),
        contracts=(contract,),
        quotes=(quote,),
        source_manifest_hashes=(_hash("d"),),
        quality_results=_quality(),
    )
    policy = OptionExecutionPolicy(
        policy_id="option.execution.single",
        policy_version="v1",
        fill_model_version="recorded.quote.cross.v1",
        mode=OptionExecutionMode.SINGLE,
        fee_per_contract=Decimal("1"),
        slippage_ticks=0,
        allow_partial=False,
        allow_illiquid=False,
    )
    dataset = _dataset(
        instrument=InstrumentKind.OPTION,
        chain_hash=chain.content_hash,
        universe_ids=(contract.contract_id,),
        feature_hash=feature_hash,
    )
    spec = _spec(
        dataset,
        simulation_policy_hash=policy.content_hash,
        cost_model_hash=policy.cost_model_hash,
        fill_model_hash=policy.fill_model_hash,
        hypothesis_hash=hypothesis_hash,
    )
    fill = simulate_option_fill(
        fill_id="option.order.single",
        contract=contract,
        quote=quote,
        side=TransactionSide.BUY,
        quantity=Decimal("1"),
        filled_at=NOW,
        fee_per_contract=policy.fee_per_contract,
        slippage_ticks=policy.slippage_ticks,
        allow_partial=policy.allow_partial,
        allow_illiquid=policy.allow_illiquid,
    )
    order = OptionOrderIntentEvidence(
        order_id=fill.fill_id,
        contract_id=contract.contract_id,
        side=fill.side,
        quantity=fill.requested_quantity,
        requested_at=NOW,
        quote_hash=quote.content_hash,
        execution_policy_hash=policy.content_hash,
    )
    position = position_from_fill(fill, position_id="position.option.single")
    valuation = _inputs(contract, quote)
    model = BlackScholesModel()
    iv = model.implied_volatility(valuation)
    greek = model.greeks(valuation, Decimal("0.2"))
    mark = mark_option_position(
        position,
        quote=quote,
        theoretical_price=greek.price,
        theoretical_input_hash=valuation.content_hash,
        marked_at=NOW,
    )
    lifecycle = simulate_option_lifecycle(
        position,
        event_id="lifecycle.option.single.expiry",
        event_at=EXPIRY,
        settlement_spot=Decimal("120"),
    )
    return DerivativeSimulationEvidence.from_option(
        simulation_id="simulation.option.representative",
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        execution_policy=policy,
        orders=(order,),
        fills=(fill,),
        positions=(position,),
        valuation_inputs=(valuation,),
        implied_volatilities=(iv,),
        greeks=(greek,),
        marks=(mark,),
        lifecycle_events=(lifecycle,),
    )


def _multi_leg_evidence(
    *, hypothesis_hash: str = _hash("6"), feature_hash: str = _hash("3")
) -> DerivativeSimulationEvidence:
    call = option_contract("option_multileg_call")
    put = option_contract("option_multileg_put", option_type=OptionType.PUT)
    call_quote = option_quote(call)
    put_quote = option_quote(put)
    chain = OptionChainSnapshot(
        chain_snapshot_id="chain.option.multileg.simulation",
        underlying_id=call.underlying_id,
        knowledge_time=NOW,
        underlying_price=Decimal("100"),
        contracts=(call, put),
        quotes=(call_quote, put_quote),
        source_manifest_hashes=(_hash("e"),),
        quality_results=_quality(),
    )
    policy = OptionExecutionPolicy(
        policy_id="option.execution.multileg",
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
        group_id="group.simulation.evidence",
        legs=(
            OptionLeg("call.leg", call, PositionSide.LONG, Decimal("1")),
            OptionLeg("put.leg", put, PositionSide.SHORT, Decimal("1")),
        ),
        policy=MultiLegExecutionPolicy.SIMULTANEOUS,
        requested_at=NOW,
        maximum_leg_time_skew_seconds=1,
        allow_partial=False,
        execution_policy_hash=policy.content_hash,
    )
    result = execute_multi_leg_order(
        order,
        quotes={call.contract_id: call_quote, put.contract_id: put_quote},
        fill_times={"call.leg": NOW, "put.leg": NOW},
        fee_per_contract=policy.fee_per_contract,
    )
    positions = tuple(
        position_from_fill(fill, position_id=f"position.{fill.fill_id}")
        for fill in result.committed_fills
    )
    valuations = (_inputs(call, call_quote), _inputs(put, put_quote))
    model = BlackScholesModel()
    ivs = tuple(model.implied_volatility(item) for item in valuations)
    greeks = tuple(model.greeks(item, Decimal("0.2")) for item in valuations)
    greek_by_contract = {item.contract_id: item for item in greeks}
    input_by_contract = {item.contract.contract_id: item for item in valuations}
    quote_by_contract = {call.contract_id: call_quote, put.contract_id: put_quote}
    marks = tuple(
        mark_option_position(
            position,
            quote=quote_by_contract[position.contract.contract_id],
            theoretical_price=greek_by_contract[position.contract.contract_id].price,
            theoretical_input_hash=input_by_contract[
                position.contract.contract_id
            ].content_hash,
            marked_at=NOW,
        )
        for position in positions
    )
    dataset = _dataset(
        instrument=InstrumentKind.OPTION,
        chain_hash=chain.content_hash,
        universe_ids=(call.contract_id, put.contract_id),
        feature_hash=feature_hash,
    )
    spec = _spec(
        dataset,
        simulation_policy_hash=policy.content_hash,
        cost_model_hash=policy.cost_model_hash,
        fill_model_hash=policy.fill_model_hash,
        hypothesis_hash=hypothesis_hash,
    )
    return DerivativeSimulationEvidence.from_multi_leg(
        simulation_id="simulation.multileg.representative",
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        execution_policy=policy,
        order=order,
        execution_result=result,
        positions=positions,
        valuation_inputs=valuations,
        implied_volatilities=ivs,
        greeks=greeks,
        marks=marks,
    )


@pytest.mark.parametrize(
    ("factory", "kind"),
    [
        (_futures_evidence, SimulationProductKind.FUTURE),
        (_single_option_evidence, SimulationProductKind.OPTION),
        (_multi_leg_evidence, SimulationProductKind.MULTI_LEG),
    ],
)
def test_actual_simulation_evidence_round_trips_and_binds_successful_run(
    factory: object,
    kind: SimulationProductKind,
) -> None:
    assert callable(factory)
    evidence = factory()
    assert isinstance(evidence, DerivativeSimulationEvidence)

    restored = DerivativeSimulationEvidence.from_dict(evidence.as_dict())

    assert restored == evidence
    assert evidence.product_kind is kind
    assert evidence.event_stream_hash.startswith("sha256:")
    evidence.validate_run(_run(evidence))


@pytest.mark.parametrize(
    ("factory", "mutate"),
    [
        (
            _futures_evidence,
            lambda payload: payload["simulation_payload"]["steps"][0]["ledger"].__setitem__(
                "cash_balance", "999999999"
            ),
        ),
        (
            _single_option_evidence,
            lambda payload: payload["simulation_payload"]["fills"][0].__setitem__(
                "price", "999"
            ),
        ),
        (
            _multi_leg_evidence,
            lambda payload: payload["simulation_payload"][
                "multi_leg_execution"
            ].__setitem__("net_cash_flow", "0"),
        ),
    ],
)
def test_nested_domain_result_tampering_fails_closed(
    factory: object, mutate: object
) -> None:
    assert callable(factory) and callable(mutate)
    evidence = factory()
    assert isinstance(evidence, DerivativeSimulationEvidence)
    payload = deepcopy(evidence.as_dict())

    mutate(payload)

    with pytest.raises(SimulationEvidenceError, match="mismatch"):
        DerivativeSimulationEvidence.from_dict(payload)


def test_run_cannot_replace_typed_result_with_arbitrary_hash() -> None:
    evidence = _single_option_evidence()
    run = _run(evidence)
    arbitrary = DerivativeExperimentRun(
        run_id=run.run_id,
        experiment_spec_hash=run.experiment_spec_hash,
        dataset_snapshot_hash=run.dataset_snapshot_hash,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status,
        event_stream_hash=run.event_stream_hash,
        result_artifact_hash=_hash("f"),
    )

    with pytest.raises(
        SimulationEvidenceError, match="run_result_artifact_mismatch"
    ):
        evidence.validate_run(arbitrary)


def test_futures_spec_must_bind_real_simulator_models() -> None:
    near, _deferred, chain, _later = _market_fixture()
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
        fill_model_hash=_hash("0"),
    )
    quote = chain.quote_for(near.contract_id, chain.observed_at)
    order = FuturesOrderIntent(
        intent_id="intent.bad.fill.model",
        contract_id=near.contract_id,
        side=OrderSide.BUY,
        quantity=1,
        decision_at=quote.observed_at,
    )
    step = simulator.execute(
        FuturesLedger.open("ledger.bad.fill.model", Decimal("100000")),
        order,
        quote,
        fill_id="fill.bad.fill.model",
        step_id="step.bad.fill.model",
    )

    with pytest.raises(SimulationEvidenceError, match="spec_fill_model_mismatch"):
        DerivativeSimulationEvidence.from_futures(
            simulation_id="simulation.future.bad.fill.model",
            dataset=dataset,
            experiment_spec=spec,
            chain=chain,
            simulator=simulator,
            orders=(order,),
            steps=(step,),
        )
