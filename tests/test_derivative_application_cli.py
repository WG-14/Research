from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.derivatives.application import (
    DerivativeResearchApplicationService,
    DerivativeStudyExecution,
    FuturesOrderCommand,
    FuturesStudyRequest,
    MultiLegStudyRequest,
    OptionLifecycleCommand,
    OptionOrderCommand,
    OptionStudyRequest,
    ReproductionStatus,
)
from market_research.research.derivatives.application_codec import (
    DerivativeApplicationFailureArtifact,
    DerivativeApplicationCodecError,
    DerivativeApplicationTransport,
    EXECUTION_TYPES,
    FAILURE_TYPES,
    REPRODUCTION_TYPES,
    REQUEST_TYPES,
    load_derivative_application_transport,
    write_derivative_application_transport,
)
from market_research.research.derivatives.common import InstrumentKind
from market_research.research.derivatives.futures import FuturesOrderIntent, OrderSide
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
    futures_fill_model_hash,
)
from market_research.research_cli.context import ResearchAppContext
from market_research.research_cli.main import main
from market_research.settings import ResearchSettings
from tests.test_derivative_application_service import (
    _option_application_parts,
    _preregistration,
)
from tests.test_derivative_simulation_evidence import (
    _dataset,
    _option_lifecycle_dataset,
    _quality,
    _spec,
)
from tests.test_futures_derivative_research import _market_fixture, _simulator
from tests.test_options_derivative_research import (
    EXPIRY,
    NOW,
    _contract as option_contract,
    _hash,
    _inputs,
    _quote as option_quote,
    _settlement_input,
)


def _manager(
    tmp_path: Path, *, project_root: Path | None = None
) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "state" / "datasets",
            artifact_root=tmp_path / "state" / "artifacts",
            report_root=tmp_path / "state" / "reports",
            cache_root=tmp_path / "state" / "cache",
            db_path=None,
            max_workers=1,
            random_seed=17,
        ),
        project_root=project_root or Path.cwd(),
    )


def _futures_request() -> FuturesStudyRequest:
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
    return FuturesStudyRequest(
        run_id="run.future.cli",
        simulation_id="simulation.future.cli",
        ledger_id="ledger.future.cli",
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
                intent=FuturesOrderIntent(
                    intent_id="application.future.cli.open",
                    contract_id=near.contract_id,
                    side=OrderSide.BUY,
                    quantity=1,
                    decision_at=quote.observed_at,
                ),
                fill_id="application.future.cli.fill",
                step_id="application.future.cli.step",
            ),
        ),
    )


def _option_request() -> OptionStudyRequest:
    preregistration, chain, policy, dataset, spec = _option_application_parts()
    contract = chain.contracts[0]
    lifecycle_dataset = _option_lifecycle_dataset(dataset)
    return OptionStudyRequest(
        run_id="run.option.cli",
        simulation_id="simulation.option.cli",
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
                order_id="application.option.cli.order",
                position_id="application.option.cli.position",
                contract_id=contract.contract_id,
                side=TransactionSide.BUY,
                quantity=Decimal("1"),
                requested_at=NOW,
                valuation_input=_inputs(contract, chain.quotes[0]),
                lifecycle=OptionLifecycleCommand(
                    event_id="application.option.cli.expiry",
                    event_at=EXPIRY,
                    settlement_input=_settlement_input(
                        contract,
                        "110",
                        settlement_at=EXPIRY,
                    ),
                    observation_dataset_hash=lifecycle_dataset.content_hash,
                ),
            ),
        ),
        lifecycle_datasets=(lifecycle_dataset,),
    )


def _multi_leg_request() -> MultiLegStudyRequest:
    call = option_contract("option_cli_multileg_call")
    put = option_contract("option_cli_multileg_put", option_type=OptionType.PUT)
    call_quote = option_quote(call)
    put_quote = option_quote(put)
    chain = OptionChainSnapshot(
        chain_snapshot_id="chain.option.cli.multileg",
        underlying_id=call.underlying_id,
        knowledge_time=NOW,
        underlying_price=Decimal("100"),
        contracts=(call, put),
        quotes=(call_quote, put_quote),
        source_manifest_hashes=(_hash("e"),),
        quality_results=_quality(),
    )
    policy = OptionExecutionPolicy(
        policy_id="option.execution.cli.multileg",
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
        group_id="application.cli.multileg.group",
        legs=(
            OptionLeg(
                "application.cli.call.leg", call, PositionSide.LONG, Decimal("1")
            ),
            OptionLeg("application.cli.put.leg", put, PositionSide.SHORT, Decimal("1")),
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
    return MultiLegStudyRequest(
        run_id="run.multileg.cli",
        simulation_id="simulation.multileg.cli",
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
            ("application.cli.call.leg", NOW),
            ("application.cli.put.leg", NOW),
        ),
    )


def _failed_option_request() -> OptionStudyRequest:
    preregistration, chain, policy, dataset, spec = _option_application_parts()
    contract = chain.contracts[0]
    quote = option_quote(contract, bid=None, ask=None, bid_size="0", ask_size="0")
    bad_chain = replace(chain, quotes=(quote,))
    bad_dataset = replace(dataset, chain_snapshot_hashes=(bad_chain.content_hash,))
    bad_spec = replace(spec, dataset_snapshot_hash=bad_dataset.content_hash)
    return OptionStudyRequest(
        run_id="run.option.cli.failed",
        simulation_id="simulation.option.cli.failed",
        started_at=NOW,
        finished_at="2026-07-03T00:00:00Z",
        preregistration=preregistration,
        dataset=bad_dataset,
        experiment_spec=bad_spec,
        chain=bad_chain,
        execution_policy=policy,
        valuation_model=BlackScholesModel(),
        orders=(
            OptionOrderCommand(
                order_id="application.option.cli.failed.order",
                position_id="application.option.cli.failed.position",
                contract_id=contract.contract_id,
                side=TransactionSide.BUY,
                quantity=Decimal("1"),
                requested_at=NOW,
                valuation_input=_inputs(contract, quote),
            ),
        ),
    )


def _failed_admission_option_request() -> OptionStudyRequest:
    request = _option_request()
    return replace(
        request,
        run_id="run.option.cli.failed.admission",
        simulation_id="simulation.option.cli.failed.admission",
        experiment_spec=replace(
            request.experiment_spec,
            hypothesis_version_hash=_hash("f"),
        ),
    )


@pytest.mark.parametrize(
    "study_request",
    (_futures_request(), _option_request(), _multi_leg_request()),
)
def test_application_transport_roundtrips_every_typed_request(
    study_request: FuturesStudyRequest | OptionStudyRequest | MultiLegStudyRequest,
) -> None:
    transport = DerivativeApplicationTransport(study_request)
    assert DerivativeApplicationTransport.from_dict(transport.as_dict()) == transport


def test_application_transport_roundtrips_allowlisted_request_and_rejects_unsafe_nodes(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    path = tmp_path / "future-request.json"
    request = _futures_request()
    transport = write_derivative_application_transport(manager, path, request)

    loaded = load_derivative_application_transport(
        manager, path, expected_types=REQUEST_TYPES
    )
    assert loaded == transport
    assert loaded.payload == request

    unknown_type = transport.as_dict()
    encoded = unknown_type["payload"]
    assert isinstance(encoded, dict)
    encoded["type_name"] = "untrusted.module.ArbitraryType"
    with pytest.raises(
        DerivativeApplicationCodecError,
        match="dataclass_type_unknown",
    ):
        DerivativeApplicationTransport.from_dict(unknown_type)

    unknown_field = transport.as_dict()
    unknown_field["unexpected"] = True
    with pytest.raises(DerivativeApplicationCodecError, match="fields_invalid"):
        DerivativeApplicationTransport.from_dict(unknown_field)

    float_value = transport.as_dict()
    float_value["schema_version"] = 1.0
    with pytest.raises(DerivativeApplicationCodecError, match="float_forbidden"):
        DerivativeApplicationTransport.from_dict(float_value)

    forbidden = transport.as_dict()
    forbidden["brokerAPIKey"] = "forbidden"
    with pytest.raises(
        DerivativeApplicationCodecError,
        match="live_field_forbidden",
    ):
        DerivativeApplicationTransport.from_dict(forbidden)

    missing_transition = transport.as_dict()
    request_node = missing_transition["payload"]
    assert isinstance(request_node, dict)
    request_fields = request_node["fields"]
    assert isinstance(request_fields, dict)
    preregistration_node = request_fields["preregistration"]
    assert isinstance(preregistration_node, dict)
    preregistration_fields = preregistration_node["fields"]
    assert isinstance(preregistration_fields, dict)
    del preregistration_fields["transition"]
    with pytest.raises(
        DerivativeApplicationCodecError,
        match=r"fields_invalid:missing=transition",
    ):
        DerivativeApplicationTransport.from_dict(missing_transition)

    transition_drift = transport.as_dict()
    request_node = transition_drift["payload"]
    assert isinstance(request_node, dict)
    request_fields = request_node["fields"]
    assert isinstance(request_fields, dict)
    preregistration_node = request_fields["preregistration"]
    assert isinstance(preregistration_node, dict)
    preregistration_fields = preregistration_node["fields"]
    assert isinstance(preregistration_fields, dict)
    transition_node = preregistration_fields["transition"]
    assert isinstance(transition_node, dict)
    transition_fields = transition_node["fields"]
    assert isinstance(transition_fields, dict)
    transition_fields["recorded_at"] = "2025-12-05T00:00:00Z"
    with pytest.raises(
        DerivativeApplicationCodecError,
        match="derivative_application_transport_dataclass_invalid",
    ):
        DerivativeApplicationTransport.from_dict(transition_drift)


def test_application_transport_paths_must_be_absolute_and_repository_external(
    tmp_path: Path,
) -> None:
    fake_repo = tmp_path / "repository"
    fake_repo.mkdir()
    manager = _manager(tmp_path, project_root=fake_repo)
    request = _futures_request()

    with pytest.raises(DerivativeApplicationCodecError, match="absolute path"):
        write_derivative_application_transport(manager, "relative.json", request)
    with pytest.raises(
        DerivativeApplicationCodecError,
        match="outside the repository",
    ):
        write_derivative_application_transport(
            manager, fake_repo / "inside.json", request
        )

    inside = fake_repo / "input.json"
    inside.write_text(
        json.dumps(DerivativeApplicationTransport(request).as_dict()),
        encoding="utf-8",
    )
    with pytest.raises(
        DerivativeApplicationCodecError,
        match="repository_external",
    ):
        load_derivative_application_transport(
            manager,
            inside,
            expected_types=REQUEST_TYPES,
        )


def test_application_transport_rejects_duplicate_keys_and_output_collisions(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version":1,"schema_version":1}',
        encoding="utf-8",
    )
    with pytest.raises(DerivativeApplicationCodecError, match="duplicate_json_key"):
        load_derivative_application_transport(
            manager,
            duplicate,
            expected_types=REQUEST_TYPES,
        )

    target = tmp_path / "immutable-request.json"
    write_derivative_application_transport(manager, target, _futures_request())
    with pytest.raises(
        DerivativeApplicationCodecError,
        match="atomic_json_target_conflict",
    ):
        write_derivative_application_transport(manager, target, _option_request())


@pytest.mark.parametrize(
    ("slug", "study_request"),
    (
        ("future", _futures_request()),
        ("option", _option_request()),
        ("multi_leg", _multi_leg_request()),
    ),
)
def test_cli_executes_and_reproduces_typed_request_under_external_root(
    tmp_path: Path,
    slug: str,
    study_request: FuturesStudyRequest | OptionStudyRequest | MultiLegStudyRequest,
) -> None:
    manager = _manager(tmp_path)
    request_path = tmp_path / f"{slug}-request.json"
    execution_path = tmp_path / f"{slug}-execution.json"
    receipt_path = tmp_path / f"{slug}-reproduction.json"
    request_transport = write_derivative_application_transport(
        manager, request_path, study_request
    )
    output: list[str] = []
    context = ResearchAppContext(
        settings=manager.settings,
        paths=manager,
        printer=output.append,
    )

    assert (
        main(
            [
                "research-derivative-execute",
                "--request",
                str(request_path),
                "--out",
                str(execution_path),
            ],
            context,
        )
        == 0
    )
    execution_transport = load_derivative_application_transport(
        manager,
        execution_path,
        expected_types=EXECUTION_TYPES,
    )
    assert isinstance(execution_transport.payload, DerivativeStudyExecution)
    assert execution_transport.bindings == (
        ("request_transport_hash", request_transport.content_hash),
    )
    assert execution_transport.payload.run.status == "SUCCEEDED"

    assert (
        main(
            [
                "research-derivative-reproduce",
                "--request",
                str(request_path),
                "--expected",
                str(execution_path),
                "--reproduction-id",
                f"reproduction.{slug}.cli",
                "--verified-at",
                "2026-07-04T00:00:00Z",
                "--out",
                str(receipt_path),
            ],
            context,
        )
        == 0
    )
    receipt_transport = load_derivative_application_transport(
        manager,
        receipt_path,
        expected_types=REPRODUCTION_TYPES,
    )
    receipt = receipt_transport.payload
    assert receipt.status is ReproductionStatus.PASS
    assert receipt_transport.bindings == (
        (
            "expected_execution_transport_hash",
            execution_transport.content_hash,
        ),
        ("request_transport_hash", request_transport.content_hash),
    )
    assert context.run_result_hash == receipt_transport.content_hash
    assert request_path.is_file()
    assert execution_path.is_file()
    assert receipt_path.is_file()
    assert '"status": "PASS"' in output[-1]


def test_cli_publishes_fail_reproduction_receipt_when_rerun_fails(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    failed_request = _failed_option_request()
    request_path = tmp_path / "failed-reproduction-request.json"
    expected_path = tmp_path / "failed-reproduction-expected.json"
    receipt_path = tmp_path / "failed-reproduction-receipt.json"
    request_transport = write_derivative_application_transport(
        manager, request_path, failed_request
    )
    expected = DerivativeResearchApplicationService().run_option(_option_request())
    expected_transport = write_derivative_application_transport(
        manager,
        expected_path,
        expected,
        bindings={"request_transport_hash": request_transport.content_hash},
    )
    output: list[str] = []
    context = ResearchAppContext(
        settings=manager.settings,
        paths=manager,
        printer=output.append,
    )

    assert (
        main(
            [
                "research-derivative-reproduce",
                "--request",
                str(request_path),
                "--expected",
                str(expected_path),
                "--reproduction-id",
                "reproduction.option.failed.cli",
                "--verified-at",
                "2026-07-04T00:00:00Z",
                "--out",
                str(receipt_path),
            ],
            context,
        )
        == 1
    )
    receipt_transport = load_derivative_application_transport(
        manager,
        receipt_path,
        expected_types=REPRODUCTION_TYPES,
    )
    receipt = receipt_transport.payload
    assert receipt.status is ReproductionStatus.FAIL
    assert receipt.reproduced_simulation_hash is None
    assert receipt.reproduced_failure_result_hash is not None
    assert receipt.mismatch_fields == ("reproduced_run_failed",)
    assert receipt_transport.bindings == (
        ("expected_execution_transport_hash", expected_transport.content_hash),
        ("request_transport_hash", request_transport.content_hash),
    )
    assert context.run_result_hash == receipt_transport.content_hash
    assert '"status": "FAIL"' in output[-1]


@pytest.mark.parametrize(
    ("study_request", "expected_failure_code"),
    (
        (_failed_option_request(), "option_fill_not_executed"),
        (
            _failed_admission_option_request(),
            "experiment_hypothesis_version_mismatch",
        ),
    ),
)
def test_cli_atomically_publishes_bounded_structured_failure_and_returns_one(
    tmp_path: Path,
    study_request: OptionStudyRequest,
    expected_failure_code: str,
) -> None:
    manager = _manager(tmp_path)
    request_path = tmp_path / "failed-option-request.json"
    failure_path = tmp_path / "failed-option-result.json"
    request_transport = write_derivative_application_transport(
        manager,
        request_path,
        study_request,
    )
    output: list[str] = []
    context = ResearchAppContext(
        settings=manager.settings,
        paths=manager,
        printer=output.append,
    )

    arguments = [
        "research-derivative-execute",
        "--request",
        str(request_path),
        "--out",
        str(failure_path),
    ]
    assert main(arguments, context) == 1
    failure_transport = load_derivative_application_transport(
        manager,
        failure_path,
        expected_types=FAILURE_TYPES,
    )
    failure = failure_transport.payload
    assert isinstance(failure, DerivativeApplicationFailureArtifact)
    assert failure.failed_run.status == "FAILED"
    assert failure.failure_code == expected_failure_code
    assert failure.request_transport_hash == request_transport.content_hash
    assert failure_transport.bindings == (
        ("request_transport_hash", request_transport.content_hash),
    )
    assert failure.message_sha256.startswith("sha256:")
    assert context.run_result_hash == failure_transport.content_hash
    assert '"status": "FAILED"' in output[-1]

    # The same failure is idempotent and verifies the existing immutable file.
    assert main(arguments, context) == 1
    assert (
        load_derivative_application_transport(
            manager,
            failure_path,
            expected_types=FAILURE_TYPES,
        )
        == failure_transport
    )
