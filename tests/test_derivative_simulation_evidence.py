from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from decimal import Decimal

import pytest

from market_research.research.derivatives.common import (
    AvailabilityTimes,
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
    FuturesLifecycleEvent,
    FuturesOrderIntent,
    LifecycleEventType,
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
from market_research.research.hashing import sha256_prefixed
from tests.test_futures_derivative_research import HASH_A, _market_fixture, _simulator
from tests.test_options_derivative_research import (
    EXPIRY,
    NOW,
    _contract as option_contract,
    _hash,
    _inputs,
    _quote as option_quote,
    _settlement_input,
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
        raw_manifest_hashes=(
            _hash("1"),
            _hash("a"),
            _hash("b"),
            _hash("d"),
            _hash("e"),
        ),
        normalized_dataset_hash=_hash("2"),
        chain_snapshot_hashes=(chain_hash,),
        feature_definition_hashes=(feature_hash,),
        calendar_hash=_hash("4"),
        policy_hashes=(filter_contract.content_hash,),
        quality_results=_quality(),
        universe_ids=universe_ids,
        period_start="2026-01-01T00:00:00Z",
        period_end="2026-03-10T16:00:00Z"
        if instrument is InstrumentKind.FUTURE
        else NOW,
        filter_contract=filter_contract,
    )


def _option_lifecycle_dataset(
    dataset: DerivativeDatasetSnapshot,
) -> DerivativeDatasetSnapshot:
    return replace(
        dataset,
        snapshot_id=f"{dataset.snapshot_id}.lifecycle",
        knowledge_time="2026-07-02T00:00:01+00:00",
        normalized_dataset_hash=_hash("f"),
        period_start="2026-07-01T00:00:00+00:00",
        period_end=EXPIRY,
    )


def _spec(
    dataset: DerivativeDatasetSnapshot,
    *,
    simulation_policy_hash: str,
    cost_model_hash: str,
    fill_model_hash: str,
    hypothesis_hash: str = _hash("6"),
    valuation_model_hash: str | None = None,
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
        valuation_model_hash=valuation_model_hash,
    )


def _run(evidence: DerivativeSimulationEvidence) -> DerivativeExperimentRun:
    payload = evidence.simulation_payload
    observation_datasets = payload.get("lifecycle_dataset_snapshots", [])
    assert isinstance(observation_datasets, list)
    return DerivativeExperimentRun(
        run_id=f"run.{evidence.product_kind.value.lower()}.simulation",
        experiment_spec_hash=evidence.experiment_spec_hash,
        dataset_snapshot_hash=evidence.dataset_snapshot_hash,
        started_at="2026-03-10T15:59:00Z",
        finished_at="2026-07-03T00:00:00Z",
        status="SUCCEEDED",
        event_stream_hash=evidence.event_stream_hash,
        result_artifact_hash=evidence.content_hash,
        observation_dataset_snapshot_hashes=tuple(
            str(item["content_hash"])
            for item in observation_datasets
            if isinstance(item, dict)
        ),
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
    *,
    hypothesis_hash: str = _hash("6"),
    feature_hash: str = _hash("3"),
    with_lifecycle: bool = False,
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
    model = BlackScholesModel()
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
        valuation_model_hash=model.content_hash,
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
    iv = model.implied_volatility(valuation)
    assert iv.volatility is not None
    greek = model.greeks(valuation, iv.volatility)
    mark = mark_option_position(
        position,
        quote=quote,
        theoretical_price=greek.price,
        theoretical_input_hash=valuation.content_hash,
        marked_at=NOW,
    )
    lifecycle_events = ()
    lifecycle_datasets = ()
    lifecycle_observation_hashes = ()
    if with_lifecycle:
        lifecycle = simulate_option_lifecycle(
            position,
            event_id="lifecycle.option.single.expiry",
            event_at=EXPIRY,
            settlement_input=_settlement_input(contract, "120", settlement_at=EXPIRY),
        )
        lifecycle_dataset = _option_lifecycle_dataset(dataset)
        lifecycle_events = (lifecycle,)
        lifecycle_datasets = (lifecycle_dataset,)
        lifecycle_observation_hashes = (lifecycle_dataset.content_hash,)
    return DerivativeSimulationEvidence.from_option(
        simulation_id="simulation.option.representative",
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        execution_policy=policy,
        valuation_model=model,
        orders=(order,),
        fills=(fill,),
        positions=(position,),
        valuation_inputs=(valuation,),
        implied_volatilities=(iv,),
        greeks=(greek,),
        marks=(mark,),
        lifecycle_events=lifecycle_events,
        lifecycle_datasets=lifecycle_datasets,
        lifecycle_observation_dataset_hashes=lifecycle_observation_hashes,
    )


def _multi_leg_evidence(
    *,
    hypothesis_hash: str = _hash("6"),
    feature_hash: str = _hash("3"),
    partial: bool = False,
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
        mode=(
            OptionExecutionMode.SEQUENTIAL
            if partial
            else OptionExecutionMode.SIMULTANEOUS
        ),
        fee_per_contract=Decimal("0"),
        slippage_ticks=0,
        allow_partial=partial,
        allow_illiquid=False,
        maximum_leg_time_skew_seconds=1,
    )
    model = BlackScholesModel()
    order = MultiLegOrder(
        group_id="group.simulation.evidence",
        legs=(
            OptionLeg("call.leg", call, PositionSide.LONG, Decimal("1")),
            OptionLeg("put.leg", put, PositionSide.SHORT, Decimal("2")),
        ),
        policy=(
            MultiLegExecutionPolicy.SEQUENTIAL
            if partial
            else MultiLegExecutionPolicy.SIMULTANEOUS
        ),
        requested_at=NOW,
        maximum_leg_time_skew_seconds=1,
        allow_partial=partial,
        execution_policy_hash=policy.content_hash,
    )
    result = execute_multi_leg_order(
        order,
        quotes={call.contract_id: call_quote, put.contract_id: put_quote},
        fill_times={"call.leg": NOW, "put.leg": NOW},
        participation_rates={"put.leg": Decimal("0.01")} if partial else None,
        fee_per_contract=policy.fee_per_contract,
    )
    positions = tuple(
        position_from_fill(fill, position_id=f"position.{fill.fill_id}")
        for fill in result.committed_fills
    )
    valuation_by_contract = {
        call.contract_id: _inputs(call, call_quote),
        put.contract_id: _inputs(put, put_quote),
    }
    valuations = tuple(
        valuation_by_contract[item.contract.contract_id]
        for item in result.committed_fills
    )
    ivs = tuple(model.implied_volatility(item) for item in valuations)
    assert all(item.volatility is not None for item in ivs)
    greeks = tuple(
        model.greeks(valuation, iv.volatility)  # type: ignore[arg-type]
        for valuation, iv in zip(valuations, ivs, strict=True)
    )
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
        valuation_model_hash=model.content_hash,
    )
    return DerivativeSimulationEvidence.from_multi_leg(
        simulation_id="simulation.multileg.representative",
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        execution_policy=policy,
        valuation_model=model,
        order=order,
        execution_result=result,
        positions=positions,
        valuation_inputs=valuations,
        implied_volatilities=ivs,
        greeks=greeks,
        marks=marks,
        participation_rates=((("put.leg", Decimal("0.01")),) if partial else ()),
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
            lambda payload: payload["simulation_payload"]["steps"][0][
                "ledger"
            ].__setitem__("cash_balance", "999999999"),
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


def _from_simulation_payload(
    evidence: DerivativeSimulationEvidence, payload: dict[str, object]
) -> DerivativeSimulationEvidence:
    return DerivativeSimulationEvidence(
        simulation_id=evidence.simulation_id,
        product_kind=evidence.product_kind,
        simulation_payload_json=json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )


def _rehash(payload: dict[str, object], label: str) -> None:
    payload["content_hash"] = sha256_prefixed(
        {key: value for key, value in payload.items() if key != "content_hash"},
        label=label,
    )


def _rebind_rehashed_chain(payload: dict[str, object]) -> None:
    chain = payload["product_chain"]
    dataset = payload["dataset_snapshot"]
    spec = payload["experiment_spec"]
    assert isinstance(chain, dict)
    assert isinstance(dataset, dict)
    assert isinstance(spec, dict)
    _rehash(chain, "futures_contract_chain")
    dataset["chain_snapshot_hashes"] = [chain["content_hash"]]
    _rehash(dataset, "derivative_dataset_snapshot")
    spec["dataset_snapshot_hash"] = dataset["content_hash"]
    _rehash(spec, "derivative_experiment_spec")


def test_persisted_futures_evidence_rejects_rehashed_failed_ledger() -> None:
    evidence = _futures_evidence()
    payload = deepcopy(evidence.simulation_payload)
    steps = payload["steps"]
    assert isinstance(steps, list)
    step = steps[-1]
    assert isinstance(step, dict)
    ledger = step["ledger"]
    assert isinstance(ledger, dict)
    ledger["failed"] = True
    _rehash(ledger, "futures_ledger")
    _rehash(step, "futures_simulation_step")

    with pytest.raises(SimulationEvidenceError, match="failed_ledger_not_publishable"):
        _from_simulation_payload(evidence, payload)


def test_persisted_futures_evidence_rejects_quote_source_outside_chain() -> None:
    evidence = _futures_evidence()
    payload = deepcopy(evidence.simulation_payload)
    chain = payload["product_chain"]
    assert isinstance(chain, dict)
    quotes = chain["quotes"]
    assert isinstance(quotes, list)
    quote = quotes[0]
    assert isinstance(quote, dict)
    quote["source_hash"] = _hash("f")
    _rehash(quote, "futures_contract_quote")
    _rebind_rehashed_chain(payload)

    with pytest.raises(
        SimulationEvidenceError, match="futures_chain_quote_source_unbound"
    ):
        _from_simulation_payload(evidence, payload)


def test_persisted_futures_settlement_must_reference_a_chain_quote() -> None:
    evidence = _futures_evidence()
    payload = deepcopy(evidence.simulation_payload)
    steps = payload["steps"]
    assert isinstance(steps, list)
    step = steps[-1]
    assert isinstance(step, dict)
    settlements = step["settlement_events"]
    ledger = step["ledger"]
    assert isinstance(settlements, list) and isinstance(ledger, dict)
    settlement = settlements[0]
    assert isinstance(settlement, dict)
    old_hash = settlement["content_hash"]
    settlement["quote_hash"] = _hash("f")
    _rehash(settlement, "futures_settlement_event")
    event_hashes = ledger["event_hashes"]
    assert isinstance(event_hashes, list)
    ledger["event_hashes"] = [
        settlement["content_hash"] if value == old_hash else value
        for value in event_hashes
    ]
    _rehash(ledger, "futures_ledger")
    _rehash(step, "futures_simulation_step")

    with pytest.raises(
        SimulationEvidenceError, match="futures_settlement_quote_not_in_chain"
    ):
        _from_simulation_payload(evidence, payload)


def test_persisted_futures_lifecycle_must_be_known_at_chain_snapshot() -> None:
    evidence = _futures_evidence()
    payload = deepcopy(evidence.simulation_payload)
    chain = payload["product_chain"]
    assert isinstance(chain, dict)
    future_at = "2026-03-11T16:00:00Z"
    lifecycle = FuturesLifecycleEvent(
        event_id="persisted.lifecycle.future",
        contract_id="FUT.202603",
        event_type=LifecycleEventType.FINAL_SETTLEMENT,
        event_at=future_at,
        availability=AvailabilityTimes(
            event_at=future_at,
            published_at=future_at,
            provider_received_at=future_at,
            system_received_at=future_at,
            processed_at=future_at,
        ),
        source_hash=HASH_A,
    ).as_dict()
    chain["lifecycle_events"] = [lifecycle]
    payload["lifecycle_events"] = [lifecycle]
    _rebind_rehashed_chain(payload)

    with pytest.raises(
        SimulationEvidenceError,
        match="futures_chain_lifecycle_not_known_at_snapshot",
    ):
        _from_simulation_payload(evidence, payload)


def test_persisted_option_evidence_requires_complete_successful_iv_coverage() -> None:
    evidence = _multi_leg_evidence()
    payload = deepcopy(evidence.simulation_payload)
    ivs = payload["implied_volatilities"]
    assert isinstance(ivs, list)
    ivs.pop()

    with pytest.raises(SimulationEvidenceError, match="iv_input_coverage_mismatch"):
        _from_simulation_payload(evidence, payload)


def test_persisted_option_evidence_cross_binds_iv_greeks_and_model_version() -> None:
    evidence = _single_option_evidence()
    payload = deepcopy(evidence.simulation_payload)
    ivs = payload["implied_volatilities"]
    assert isinstance(ivs, list)
    iv = ivs[0]
    assert isinstance(iv, dict)
    iv["model_version"] = "unbound_model_version"
    _rehash(iv, "option_implied_volatility")

    with pytest.raises(SimulationEvidenceError, match="iv_model_version_mismatch"):
        _from_simulation_payload(evidence, payload)


def test_persisted_option_evidence_rejects_rehashed_forged_iv_solution() -> None:
    evidence = _single_option_evidence()
    payload = deepcopy(evidence.simulation_payload)
    ivs = payload["implied_volatilities"]
    assert isinstance(ivs, list)
    iv = ivs[0]
    assert isinstance(iv, dict)
    iv["volatility"] = "0.5"
    _rehash(iv, "option_implied_volatility")

    with pytest.raises(SimulationEvidenceError, match="iv_solver_mismatch:volatility"):
        _from_simulation_payload(evidence, payload)


def test_persisted_option_evidence_recomputes_greeks_numerically() -> None:
    evidence = _single_option_evidence()
    payload = deepcopy(evidence.simulation_payload)
    greeks = payload["greeks"]
    assert isinstance(greeks, list)
    greek = greeks[0]
    assert isinstance(greek, dict)
    greek["delta"] = "0.123456789"
    _rehash(greek, "option_greeks")

    with pytest.raises(SimulationEvidenceError, match="greeks_numerical_mismatch"):
        _from_simulation_payload(evidence, payload)


def test_partial_multileg_evidence_preserves_unfilled_attempt() -> None:
    evidence = _multi_leg_evidence(partial=True)
    payload = evidence.simulation_payload
    fills = payload["fills"]
    result = payload["multi_leg_execution"]
    assert isinstance(fills, list) and isinstance(result, dict)

    assert [item["status"] for item in fills if isinstance(item, dict)] == [
        "FILLED",
        "UNFILLED",
    ]
    assert len(result["attempted_fill_hashes"]) == 2
    assert len(result["committed_fill_hashes"]) == 1
    assert result["state"] == "PARTIAL"


def test_partial_multileg_evidence_rejects_erased_unfilled_attempt() -> None:
    evidence = _multi_leg_evidence(partial=True)
    payload = deepcopy(evidence.simulation_payload)
    fills = payload["fills"]
    result = payload["multi_leg_execution"]
    assert isinstance(fills, list) and isinstance(result, dict)
    fills.pop()
    attempted = result["attempted_fill_hashes"]
    assert isinstance(attempted, list)
    attempted.pop()
    _rehash(result, "option_multileg_result")

    with pytest.raises(SimulationEvidenceError, match="attempt_coverage_mismatch"):
        _from_simulation_payload(evidence, payload)


def test_partial_multileg_evidence_rejects_participation_replay() -> None:
    evidence = _multi_leg_evidence(partial=True)
    payload = deepcopy(evidence.simulation_payload)
    participation = payload["multi_leg_participation_rates"]
    assert isinstance(participation, dict)
    participation["put.leg"] = "1"

    with pytest.raises(SimulationEvidenceError, match="attempt_semantics_mismatch"):
        _from_simulation_payload(evidence, payload)


@pytest.mark.parametrize(
    ("hash_field", "error"),
    [
        ("attempted_fill_hashes", "attempted_fill_binding_mismatch"),
        ("committed_fill_hashes", "committed_fill_binding_mismatch"),
    ],
)
def test_multileg_evidence_rejects_reordered_fill_hashes(
    hash_field: str, error: str
) -> None:
    evidence = _multi_leg_evidence()
    payload = deepcopy(evidence.simulation_payload)
    result = payload["multi_leg_execution"]
    assert isinstance(result, dict)
    fill_hashes = result[hash_field]
    assert isinstance(fill_hashes, list)
    fill_hashes.reverse()
    _rehash(result, "option_multileg_result")

    with pytest.raises(SimulationEvidenceError, match=error):
        _from_simulation_payload(evidence, payload)


@pytest.mark.parametrize(
    ("field_name", "forged_value", "error"),
    [
        ("committed_fill_hashes", [], "committed_fill_binding_mismatch"),
        ("state", "FILLED", "execution_state_failure_mismatch"),
        ("failure_code", "forged_partial_failure", "execution_state_failure_mismatch"),
        ("legging_exposure_contract_ids", [], "legging_exposure_mismatch"),
        ("opened_at", "2026-01-02T12:00:11+00:00", "execution_time_mismatch"),
    ],
)
def test_partial_multileg_evidence_rejects_rehashed_execution_semantic_tampering(
    field_name: str,
    forged_value: object,
    error: str,
) -> None:
    evidence = _multi_leg_evidence(partial=True)
    payload = deepcopy(evidence.simulation_payload)
    result = payload["multi_leg_execution"]
    assert isinstance(result, dict)
    result[field_name] = forged_value
    _rehash(result, "option_multileg_result")

    with pytest.raises(SimulationEvidenceError, match=error):
        _from_simulation_payload(evidence, payload)


def test_persisted_option_valuation_sources_must_belong_to_dataset() -> None:
    evidence = _single_option_evidence()
    payload = deepcopy(evidence.simulation_payload)
    dataset = payload["dataset_snapshot"]
    spec = payload["experiment_spec"]
    assert isinstance(dataset, dict) and isinstance(spec, dict)
    chain = payload["product_chain"]
    assert isinstance(chain, dict)
    dataset["raw_manifest_hashes"] = list(chain["source_manifest_hashes"])
    _rehash(dataset, "derivative_dataset_snapshot")
    spec["dataset_snapshot_hash"] = dataset["content_hash"]
    _rehash(spec, "derivative_experiment_spec")

    with pytest.raises(
        SimulationEvidenceError, match="valuation_source_not_in_dataset"
    ):
        _from_simulation_payload(evidence, payload)


def test_option_lifecycle_observation_dataset_is_run_bound_not_preregistered_by_hash() -> (
    None
):
    evidence = _single_option_evidence(with_lifecycle=True)
    payload = evidence.simulation_payload
    spec = payload["experiment_spec"]
    assert isinstance(spec, dict)
    assert "observation_dataset_snapshot_hashes" not in spec

    run = _run(evidence)
    assert run.observation_dataset_snapshot_hashes
    evidence.validate_run(run)


def test_persisted_option_lifecycle_rejects_observation_outside_dataset_period() -> (
    None
):
    evidence = _single_option_evidence(with_lifecycle=True)
    payload = deepcopy(evidence.simulation_payload)
    datasets = payload["lifecycle_dataset_snapshots"]
    bindings = payload["lifecycle_observation_bindings"]
    assert isinstance(datasets, list) and isinstance(bindings, list)
    dataset = datasets[0]
    binding = bindings[0]
    assert isinstance(dataset, dict) and isinstance(binding, dict)
    dataset["period_end"] = "2026-07-01T12:00:00+00:00"
    _rehash(dataset, "derivative_dataset_snapshot")
    binding["dataset_snapshot_hash"] = dataset["content_hash"]

    with pytest.raises(
        SimulationEvidenceError,
        match="settlement_input_outside_observation_dataset_period",
    ):
        _from_simulation_payload(evidence, payload)


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
        observation_dataset_snapshot_hashes=(run.observation_dataset_snapshot_hashes),
    )

    with pytest.raises(SimulationEvidenceError, match="run_result_artifact_mismatch"):
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
