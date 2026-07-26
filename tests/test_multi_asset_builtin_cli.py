from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from market_research.research.derivatives.application import (
    FuturesOrderCommand,
    FuturesRollCommand,
    FuturesSettlementCommand,
    FuturesStudyRequest,
    MultiLegStudyRequest,
    OptionLifecycleCommand,
    OptionOrderCommand,
    OptionStudyRequest,
)
from market_research.research.derivatives.application_codec import (
    DerivativeApplicationTransport,
)
from market_research.research.derivatives.common import (
    AvailabilityTimes,
    InstrumentKind,
)
from market_research.research.derivatives.futures import (
    ContinuousFuturesPoint,
    FuturesOrderIntent,
    OrderSide,
    RollDecision,
)
from market_research.research.derivatives.options import (
    BlackScholesModel,
    MultiLegExecutionPolicy,
    MultiLegOrder,
    OptionChainSnapshot,
    OptionLeg,
    PositionSide,
    TransactionSide,
)
from market_research.research.derivatives.simulation_evidence import (
    OptionExecutionMode,
    OptionExecutionPolicy,
    futures_fill_model_hash,
)
from market_research.research.multi_asset.application import (
    DataRange,
    UniverseDefinition,
)
from market_research.research.multi_asset.builtin_runner import (
    BUILTIN_RUNNER_ID,
    BUILTIN_RUNNER_VERSION,
    BuiltinEconomicScenarioPolicy,
    BuiltinMultiAssetCodecError,
    BuiltinMultiAssetRequest,
    BuiltinReproductionRecord,
    BuiltinReproductionStatus,
    BuiltinScenarioInputs,
    BuiltinSpotScenarioInput,
    load_builtin_execution_record,
    write_builtin_multi_asset_request,
)
from market_research.research_cli.context import ResearchAppContext
from market_research.research_cli.registry import command_registry
from market_research.settings import ResearchSettings
from tests.test_derivative_application_service import (
    _preregistration,
)
from tests.test_derivative_simulation_evidence import (
    _dataset,
    _option_lifecycle_dataset,
    _quality,
    _spec as derivative_spec,
)
from tests.test_futures_derivative_research import (
    HASH_A,
    HASH_B,
    _market_fixture,
    _quote as futures_quote,
    _simulator,
)
from tests.test_multi_asset_application_service import (
    _paths,
    _references,
    _spec as outer_spec,
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


SERIES_ID = "FUT.ROOT.builtin.continuous"


def _futures_request_and_points() -> tuple[
    FuturesStudyRequest,
    tuple[ContinuousFuturesPoint, ...],
]:
    near, deferred, first_chain, later_chain = _market_fixture()
    historical_at = "2026-03-09T16:00:00Z"
    historical_quote = futures_quote(
        near.contract_id,
        historical_at,
        "99",
    )
    chain = replace(
        later_chain,
        snapshot_id="chain.builtin.complete",
        quotes=(
            historical_quote,
            *first_chain.quotes,
            *later_chain.quotes,
        ),
    )
    simulator = _simulator((near, deferred))
    preregistration = _preregistration(InstrumentKind.FUTURE)
    dataset = replace(
        _dataset(
            instrument=InstrumentKind.FUTURE,
            chain_hash=chain.content_hash,
            universe_ids=(near.contract_id, deferred.contract_id),
        ),
        knowledge_time=later_chain.observed_at,
        period_end=later_chain.observed_at,
    )
    spec = derivative_spec(
        dataset,
        simulation_policy_hash=simulator.content_hash,
        cost_model_hash=simulator.cost_policy.content_hash,
        fill_model_hash=futures_fill_model_hash(simulator),
        hypothesis_hash=preregistration.hypothesis_version.content_hash,
    )
    entry_quote = chain.quote_for(
        near.contract_id,
        first_chain.observed_at,
    )
    roll_from_quote = chain.quote_for(
        near.contract_id,
        later_chain.observed_at,
    )
    roll_to_quote = chain.quote_for(
        deferred.contract_id,
        later_chain.observed_at,
    )
    roll = RollDecision(
        decision_id="builtin.future.roll.decision",
        decision_at=later_chain.observed_at,
        root_id=chain.root_id,
        from_contract_id=near.contract_id,
        to_contract_id=deferred.contract_id,
        should_roll=True,
        reason="EXPOSURE_PRESERVING_SIGNAL_ROLL",
        policy_hash=HASH_A,
        chain_snapshot_hash=chain.content_hash,
        input_quote_hashes=(
            roll_from_quote.content_hash,
            roll_to_quote.content_hash,
        ),
    )
    first_point = ContinuousFuturesPoint(
        point_id="builtin.future.signal.history",
        series_id=SERIES_ID,
        root_id=chain.root_id,
        observed_at=historical_at,
        source_contract_id=near.contract_id,
        source_quote_hash=historical_quote.content_hash,
        source_price=historical_quote.close_price,
        continuous_price=historical_quote.close_price,
        additive_adjustment=Decimal("0"),
        multiplicative_adjustment=Decimal("1"),
        roll_gap=Decimal("0"),
        policy_hash=HASH_A,
        roll_decision_hash=HASH_B,
        chain_snapshot_hash=chain.content_hash,
        previous_point_hash=None,
    )
    entry_point = ContinuousFuturesPoint(
        point_id="builtin.future.signal.entry",
        series_id=SERIES_ID,
        root_id=chain.root_id,
        observed_at=first_chain.observed_at,
        source_contract_id=near.contract_id,
        source_quote_hash=entry_quote.content_hash,
        source_price=entry_quote.close_price,
        continuous_price=entry_quote.close_price,
        additive_adjustment=Decimal("0"),
        multiplicative_adjustment=Decimal("1"),
        roll_gap=Decimal("0"),
        policy_hash=HASH_A,
        roll_decision_hash=_hash("c"),
        chain_snapshot_hash=chain.content_hash,
        previous_point_hash=first_point.content_hash,
    )
    roll_point = ContinuousFuturesPoint(
        point_id="builtin.future.signal.roll",
        series_id=SERIES_ID,
        root_id=chain.root_id,
        observed_at=later_chain.observed_at,
        source_contract_id=deferred.contract_id,
        source_quote_hash=roll_to_quote.content_hash,
        source_price=roll_to_quote.close_price,
        continuous_price=Decimal("101"),
        additive_adjustment=Decimal("-2"),
        multiplicative_adjustment=Decimal("1"),
        roll_gap=(roll_to_quote.close_price - roll_from_quote.close_price),
        policy_hash=HASH_A,
        roll_decision_hash=roll.content_hash,
        chain_snapshot_hash=chain.content_hash,
        previous_point_hash=entry_point.content_hash,
    )
    request = FuturesStudyRequest(
        run_id="run.future.builtin",
        simulation_id="simulation.future.builtin",
        ledger_id="ledger.future.builtin",
        started_at=later_chain.observed_at,
        finished_at="2026-03-11T16:01:00Z",
        initial_cash=Decimal("100000"),
        preregistration=preregistration,
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        simulator=simulator,
        commands=(
            FuturesOrderCommand(
                intent=FuturesOrderIntent(
                    intent_id="builtin.future.open",
                    contract_id=near.contract_id,
                    side=OrderSide.BUY,
                    quantity=1,
                    decision_at=later_chain.observed_at,
                    signal_series_id=SERIES_ID,
                    signal_point_hash=entry_point.content_hash,
                ),
                fill_id="builtin.future.open.fill",
                step_id="builtin.future.open.step",
            ),
            FuturesSettlementCommand(
                contract_id=near.contract_id,
                as_of=later_chain.observed_at,
                event_id="builtin.future.settlement",
                step_id="builtin.future.settlement.step",
            ),
            FuturesRollCommand(
                decision=roll,
                execution_id="builtin.future.roll.execution",
                step_id="builtin.future.roll.step",
            ),
        ),
    )
    return request, (first_point, entry_point, roll_point)


def _option_requests() -> tuple[
    OptionStudyRequest,
    OptionStudyRequest,
    MultiLegStudyRequest,
    Decimal,
]:
    selected = option_contract("builtin_call_100", strike="100")
    competitor = option_contract("builtin_call_110", strike="110")
    selected_quote = option_quote(selected)
    competitor_quote = option_quote(
        competitor,
        bid="2.4",
        ask="2.6",
    )
    chain = OptionChainSnapshot(
        chain_snapshot_id="chain.option.builtin",
        underlying_id=selected.underlying_id,
        knowledge_time=NOW,
        underlying_price=Decimal("100"),
        contracts=(selected, competitor),
        quotes=(selected_quote, competitor_quote),
        source_manifest_hashes=(_hash("d"),),
        quality_results=_quality(),
    )
    single_policy = OptionExecutionPolicy(
        policy_id="option.execution.builtin.single",
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
        universe_ids=(selected.contract_id, competitor.contract_id),
    )
    model = BlackScholesModel()
    spec = derivative_spec(
        dataset,
        simulation_policy_hash=single_policy.content_hash,
        cost_model_hash=single_policy.cost_model_hash,
        fill_model_hash=single_policy.fill_model_hash,
        hypothesis_hash=preregistration.hypothesis_version.content_hash,
        valuation_model_hash=model.content_hash,
    )
    lifecycle_dataset = _option_lifecycle_dataset(dataset)
    selected_input = _inputs(selected, selected_quote)
    selected_iv = model.implied_volatility(selected_input)
    assert selected_iv.success and selected_iv.volatility is not None
    selected_delta = model.greeks(
        selected_input,
        selected_iv.volatility,
    ).delta
    option_request = OptionStudyRequest(
        run_id="run.option.builtin",
        simulation_id="simulation.option.builtin",
        started_at=NOW,
        finished_at="2026-07-03T00:00:00Z",
        preregistration=preregistration,
        dataset=dataset,
        experiment_spec=spec,
        chain=chain,
        execution_policy=single_policy,
        valuation_model=model,
        orders=(
            OptionOrderCommand(
                order_id="builtin.option.order",
                position_id="builtin.option.position",
                contract_id=selected.contract_id,
                side=TransactionSide.BUY,
                quantity=Decimal("1"),
                requested_at=NOW,
                valuation_input=selected_input,
                lifecycle=OptionLifecycleCommand(
                    event_id="builtin.option.expiry",
                    event_at=EXPIRY,
                    settlement_input=_settlement_input(
                        selected,
                        "110",
                        settlement_at=EXPIRY,
                    ),
                    observation_dataset_hash=(lifecycle_dataset.content_hash),
                ),
            ),
        ),
        lifecycle_datasets=(lifecycle_dataset,),
    )

    intermediate_at = "2026-04-01T12:00:00+00:00"
    intermediate_availability = AvailabilityTimes(
        event_at=intermediate_at,
        published_at=intermediate_at,
        provider_received_at=intermediate_at,
        system_received_at=intermediate_at,
        processed_at=intermediate_at,
    )
    intermediate_selected_quote = option_quote(
        selected,
        bid="4.0",
        ask="4.2",
        as_of=intermediate_at,
        availability=intermediate_availability,
    )
    intermediate_competitor_quote = option_quote(
        competitor,
        bid="1.2",
        ask="1.4",
        as_of=intermediate_at,
        availability=intermediate_availability,
    )
    intermediate_chain = OptionChainSnapshot(
        chain_snapshot_id="chain.option.builtin.intermediate",
        underlying_id=selected.underlying_id,
        knowledge_time=intermediate_at,
        underlying_price=Decimal("100"),
        contracts=(selected, competitor),
        quotes=(
            intermediate_selected_quote,
            intermediate_competitor_quote,
        ),
        source_manifest_hashes=(_hash("d"),),
        quality_results=_quality(),
    )
    intermediate_dataset = replace(
        _dataset(
            instrument=InstrumentKind.OPTION,
            chain_hash=intermediate_chain.content_hash,
            universe_ids=(selected.contract_id, competitor.contract_id),
        ),
        knowledge_time=intermediate_at,
        period_end=intermediate_at,
    )
    intermediate_spec = derivative_spec(
        intermediate_dataset,
        simulation_policy_hash=single_policy.content_hash,
        cost_model_hash=single_policy.cost_model_hash,
        fill_model_hash=single_policy.fill_model_hash,
        hypothesis_hash=preregistration.hypothesis_version.content_hash,
        valuation_model_hash=model.content_hash,
    )
    intermediate_input = replace(
        selected_input,
        valuation_input_id="valuation.builtin.option.intermediate",
        quote=intermediate_selected_quote,
        valuation_at=intermediate_at,
        spot_availability=intermediate_availability,
        rate_availability=intermediate_availability,
        dividend_availability=intermediate_availability,
        forward_availability=intermediate_availability,
    )
    intermediate_request = OptionStudyRequest(
        run_id="run.option.builtin.intermediate",
        simulation_id="simulation.option.builtin.intermediate",
        started_at=intermediate_at,
        finished_at="2026-04-01T12:01:00+00:00",
        preregistration=preregistration,
        dataset=intermediate_dataset,
        experiment_spec=intermediate_spec,
        chain=intermediate_chain,
        execution_policy=single_policy,
        valuation_model=model,
        orders=(
            OptionOrderCommand(
                order_id="builtin.option.intermediate.order",
                position_id="builtin.option.intermediate.position",
                contract_id=selected.contract_id,
                side=TransactionSide.BUY,
                quantity=Decimal("1"),
                requested_at=intermediate_at,
                valuation_input=intermediate_input,
            ),
        ),
    )

    multi_policy = OptionExecutionPolicy(
        policy_id="option.execution.builtin.multileg",
        policy_version="v1",
        fill_model_version="recorded.quote.atomic.v1",
        mode=OptionExecutionMode.SIMULTANEOUS,
        fee_per_contract=Decimal("0.5"),
        slippage_ticks=0,
        allow_partial=False,
        allow_illiquid=False,
        maximum_leg_time_skew_seconds=1,
    )
    multi_spec = derivative_spec(
        dataset,
        simulation_policy_hash=multi_policy.content_hash,
        cost_model_hash=multi_policy.cost_model_hash,
        fill_model_hash=multi_policy.fill_model_hash,
        hypothesis_hash=preregistration.hypothesis_version.content_hash,
        valuation_model_hash=model.content_hash,
    )
    order = MultiLegOrder(
        group_id="builtin.multileg.group",
        legs=(
            OptionLeg(
                "builtin.multileg.call100",
                selected,
                PositionSide.LONG,
                Decimal("1"),
            ),
            OptionLeg(
                "builtin.multileg.call110",
                competitor,
                PositionSide.SHORT,
                Decimal("1"),
            ),
        ),
        policy=MultiLegExecutionPolicy.SIMULTANEOUS,
        requested_at=NOW,
        maximum_leg_time_skew_seconds=1,
        allow_partial=False,
        execution_policy_hash=multi_policy.content_hash,
    )
    multi_request = MultiLegStudyRequest(
        run_id="run.multileg.builtin",
        simulation_id="simulation.multileg.builtin",
        started_at=NOW,
        finished_at="2026-07-03T00:00:00Z",
        preregistration=preregistration,
        dataset=dataset,
        experiment_spec=multi_spec,
        chain=chain,
        execution_policy=multi_policy,
        valuation_model=model,
        order=order,
        valuation_inputs=(
            selected_input,
            _inputs(competitor, competitor_quote),
        ),
        fill_times=(
            ("builtin.multileg.call100", NOW),
            ("builtin.multileg.call110", NOW),
        ),
        lifecycle_by_contract=(
            (
                selected.contract_id,
                OptionLifecycleCommand(
                    event_id="builtin.multileg.call100.expiry",
                    event_at=EXPIRY,
                    settlement_input=_settlement_input(
                        selected,
                        "110",
                        settlement_at=EXPIRY,
                    ),
                    observation_dataset_hash=(lifecycle_dataset.content_hash),
                ),
            ),
            (
                competitor.contract_id,
                OptionLifecycleCommand(
                    event_id="builtin.multileg.call110.expiry",
                    event_at=EXPIRY,
                    settlement_input=_settlement_input(
                        competitor,
                        "110",
                        settlement_at=EXPIRY,
                    ),
                    observation_dataset_hash=(lifecycle_dataset.content_hash),
                ),
            ),
        ),
        lifecycle_datasets=(lifecycle_dataset,),
    )
    return (
        option_request,
        intermediate_request,
        multi_request,
        selected_delta,
    )


def _builtin_request(
    tmp_path: Path,
) -> tuple[
    BuiltinMultiAssetRequest,
    ResearchAppContext,
]:
    paths = _paths(tmp_path)
    futures_request, points = _futures_request_and_points()
    (
        option_request,
        intermediate_request,
        multi_request,
        selected_delta,
    ) = _option_requests()
    decision = datetime.fromisoformat(NOW)
    expiry = datetime.fromisoformat(EXPIRY)
    days_to_expiry = int((expiry - decision).total_seconds() // 86400)
    inputs = BuiltinScenarioInputs(
        spot=BuiltinSpotScenarioInput(
            instrument_id="asset_xyz",
            economic_underlying_id="economic.asset_xyz",
            currency="USD",
            decision_at="2025-12-20T12:00:00Z",
            knowledge_at="2025-12-20T11:59:00Z",
            quantity=Decimal("10"),
            entry_price=Decimal("100"),
            split_ratio=Decimal("2"),
            dividend_per_unit=Decimal("1"),
            dividend_tax_rate=Decimal("0.1"),
            commission_per_unit=Decimal("0.25"),
        ),
        policy=BuiltinEconomicScenarioPolicy(
            continuous_series_id=SERIES_ID,
            expected_return=Decimal("0.05"),
            annualized_volatility=Decimal("0.2"),
            downside_tail_return=Decimal("-0.2"),
            upside_return=Decimal("0.25"),
            horizon_days=30,
            futures_signal_return_threshold=Decimal("0.005"),
            futures_target_notional=Decimal("5050"),
            option_target_days_to_expiry=days_to_expiry,
            option_minimum_days_to_expiry=days_to_expiry,
            option_maximum_days_to_expiry=days_to_expiry,
            option_target_delta=selected_delta,
            option_maximum_delta_distance=Decimal("1"),
            option_minimum_liquidity_weight=Decimal("0"),
            option_maximum_absolute_residual=Decimal("600"),
            option_maximum_relative_residual=Decimal("0.25"),
            joint_spot_return=Decimal("-0.05"),
            joint_liquidity_haircut=Decimal("0.01"),
            joint_liquidity_cost_multiplier=Decimal("1.5"),
            joint_margin_multiplier=Decimal("1.1"),
        ),
        futures_signal_points=points,
        futures_request=DerivativeApplicationTransport(futures_request),
        option_request=DerivativeApplicationTransport(option_request),
        option_intermediate_request=DerivativeApplicationTransport(
            intermediate_request
        ),
        multi_leg_request=DerivativeApplicationTransport(multi_request),
    )
    base_spec = outer_spec(paths)
    universe_ids = tuple(
        sorted(
            {
                "asset_xyz",
                *(item.contract_id for item in futures_request.chain.contracts),
                *(item.contract_id for item in option_request.chain.contracts),
            }
        )
    )
    scenarios = tuple(
        (
            replace(
                scenario,
                runner_id=BUILTIN_RUNNER_ID,
                runner_version=BUILTIN_RUNNER_VERSION,
                parameters=(("builtin_inputs_hash", inputs.content_hash),),
            )
            if index < 4
            else scenario
        )
        for index, scenario in enumerate(base_spec.scenarios)
    )
    spec = replace(
        base_spec,
        experiment_id="experiment:builtin-cli",
        data_range=DataRange(
            start_at="2025-01-01T00:00:00Z",
            end_at="2026-07-03T00:00:00Z",
        ),
        universe=UniverseDefinition(
            logical_id="universe:builtin-cli",
            version="v1",
            instrument_ids=universe_ids,
            asset_classes=("FUTURE", "OPTION", "SPOT"),
            point_in_time_selection_rule=(
                "Use only immutable inputs known at each decision timestamp."
            ),
        ),
        scenarios=scenarios,
    )
    request = BuiltinMultiAssetRequest(
        run_id="run:builtin-cli:first",
        spec=spec,
        evidence_references=_references(
            paths=paths,
            spec=spec,
            research_inputs_document=inputs.as_dict(),
            research_inputs_schema_id="builtin-multi-asset-scenario-inputs",
        ),
        inputs=inputs,
    )
    settings = ResearchSettings(
        data_root=paths.data_root,
        artifact_root=paths.artifact_root,
        report_root=paths.report_root,
        cache_root=paths.cache_root,
        db_path=None,
        max_workers=1,
        random_seed=0,
    )
    messages: list[str] = []
    context = ResearchAppContext(
        settings=settings,
        paths=paths,
        printer=messages.append,
    )
    return request, context


def test_public_builtin_cli_executes_and_reproduces_authoritative_study(
    tmp_path: Path,
) -> None:
    request, context = _builtin_request(tmp_path)
    request_path = (tmp_path / "builtin-request.json").resolve()
    execution_path = (tmp_path / "builtin-execution.json").resolve()
    reproduction_path = (tmp_path / "builtin-reproduction.json").resolve()
    write_builtin_multi_asset_request(
        context.paths,
        request_path,
        request,
    )

    execute_rc = command_registry()["research-multi-asset-execute"].handler(
        argparse.Namespace(
            request=str(request_path),
            out=str(execution_path),
        ),
        context,
    )
    assert execute_rc == 0
    execution = load_builtin_execution_record(
        context.paths,
        execution_path,
    )

    assert execution.request_hash == request.content_hash
    assert execution.option_eligible_contract_ids == (
        "builtin_call_100",
        "builtin_call_110",
    )
    assert execution.option_selection_hash.startswith("sha256:")
    assert execution.option_cleaned_chain_hash.startswith("sha256:")
    assert context.paths.research_artifact_path(
        request.spec.experiment_id,
        "multi_asset_study.json",
    ).is_file()

    reproduce_rc = command_registry()["research-multi-asset-reproduce"].handler(
        argparse.Namespace(
            request=str(request_path),
            expected=str(execution_path),
            reproduction_id="run:builtin-cli:reproduction",
            out=str(reproduction_path),
        ),
        context,
    )
    assert reproduce_rc == 0
    reproduction = BuiltinReproductionRecord.from_dict(
        json.loads(reproduction_path.read_text(encoding="utf-8"))
    )
    assert reproduction.status is BuiltinReproductionStatus.PASS
    assert reproduction.mismatch_fields == ()
    assert reproduction.reproduced_study_hash == (execution.study_content_hash)


def test_builtin_request_codec_rejects_external_runner_injection(
    tmp_path: Path,
) -> None:
    request, _context = _builtin_request(tmp_path)
    payload = request.as_dict()
    payload["runner_class"] = "malicious.module:Runner"

    with pytest.raises(
        BuiltinMultiAssetCodecError,
        match="builtin_multi_asset_request_fields_invalid",
    ):
        BuiltinMultiAssetRequest.from_dict(payload)
