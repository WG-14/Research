from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.derivatives.common import (
    DerivativeResearchError,
    InstrumentKind,
)
from market_research.research.derivatives.options import (
    BlackScholesModel,
    OptionRobustnessInput,
    OptionRobustnessPolicy,
    run_option_robustness_suite,
    standard_option_robustness_cases,
)
from market_research.research.derivatives.risk_metrics import (
    DerivativeRiskEvidence,
    RiskMetricId,
    RiskMetricStatus,
    RiskProductKind,
    build_futures_risk_evidence,
    build_option_risk_evidence,
)
from market_research.research.derivatives.simulation_evidence import (
    DerivativeSimulationEvidence,
    OptionExecutionMode,
    OptionExecutionPolicy,
    OptionOrderIntentEvidence,
)
from tests.test_derivative_simulation_evidence import (
    _dataset,
    _futures_evidence,
    _multi_leg_evidence,
    _run,
    _single_option_evidence,
    _spec,
)
from tests.test_options_stress_execution import _robustness_input


EVALUATED_AT = "2026-07-04T00:00:00Z"


def _by_id(evidence: DerivativeRiskEvidence) -> dict[RiskMetricId, object]:
    return {item.metric_id: item for item in evidence.metrics}


def _robustness_simulation() -> tuple[
    OptionRobustnessInput, DerivativeSimulationEvidence
]:
    inputs = _robustness_input()
    policy = OptionExecutionPolicy(
        policy_id="option.execution.robustness",
        policy_version="v1",
        fill_model_version="recorded.quote.cross.v1",
        mode=OptionExecutionMode.SINGLE,
        fee_per_contract=Decimal("0"),
        slippage_ticks=0,
        allow_partial=False,
        allow_illiquid=False,
    )
    priced_ids = set(inputs.priced_position_ids)
    positions = tuple(
        item for item in inputs.positions if item.position_id in priced_ids
    )
    fill_by_hash = {item.content_hash: item for item in inputs.fills}
    fills = tuple(fill_by_hash[item.source_fill_hash] for item in positions)
    orders = tuple(
        OptionOrderIntentEvidence(
            order_id=fill.fill_id,
            contract_id=fill.contract.contract_id,
            side=fill.side,
            quantity=fill.requested_quantity,
            requested_at=fill.filled_at,
            quote_hash=fill.quote_hash,
            execution_policy_hash=policy.content_hash,
        )
        for fill in fills
    )
    dataset = _dataset(
        instrument=InstrumentKind.OPTION,
        chain_hash=inputs.chain_snapshot.content_hash,
        universe_ids=tuple(
            item.contract_id for item in inputs.chain_snapshot.contracts
        ),
    )
    dataset = replace(
        dataset,
        knowledge_time=inputs.chain_snapshot.knowledge_time,
        period_end=inputs.chain_snapshot.knowledge_time,
    )
    valuation_model = BlackScholesModel(
        model_version=inputs.base_iv_results[0].model_version
    )
    spec = _spec(
        dataset,
        simulation_policy_hash=policy.content_hash,
        cost_model_hash=policy.cost_model_hash,
        fill_model_hash=policy.fill_model_hash,
        valuation_model_hash=valuation_model.content_hash,
    )
    simulation = DerivativeSimulationEvidence.from_option(
        simulation_id="simulation.option.robustness.risk",
        dataset=dataset,
        experiment_spec=spec,
        chain=inputs.chain_snapshot,
        execution_policy=policy,
        valuation_model=valuation_model,
        orders=orders,
        fills=fills,
        positions=positions,
        valuation_inputs=inputs.valuation_inputs,
        implied_volatilities=inputs.base_iv_results,
        greeks=inputs.greeks,
        marks=inputs.marks,
    )
    return inputs, simulation


def test_futures_builder_calculates_stream_metrics_and_keeps_missing_stress_explicit() -> (
    None
):
    simulation = _futures_evidence()
    run = _run(simulation)

    evidence = build_futures_risk_evidence(
        risk_id="risk.future.focused",
        version="v1",
        simulation_result=simulation,
        experiment_run=run,
        evaluated_at=EVALUATED_AT,
    )
    metrics = _by_id(evidence)

    assert evidence.product_kind is RiskProductKind.FUTURE
    assert len(evidence.metrics) == 20
    assert tuple(item.metric_id for item in evidence.metrics) == tuple(RiskMetricId)
    assert metrics[RiskMetricId.S5_R01].status is RiskMetricStatus.AVAILABLE
    assert metrics[RiskMetricId.S5_R02].status is RiskMetricStatus.AVAILABLE
    assert metrics[RiskMetricId.S5_R04].status is RiskMetricStatus.UNAVAILABLE_SAMPLE
    assert metrics[RiskMetricId.S5_R05].status is RiskMetricStatus.UNAVAILABLE_SAMPLE
    assert metrics[RiskMetricId.S5_R06].status is RiskMetricStatus.AVAILABLE
    assert metrics[RiskMetricId.S5_R07].status is RiskMetricStatus.AVAILABLE
    assert metrics[RiskMetricId.S5_R08].status is RiskMetricStatus.AVAILABLE
    assert metrics[RiskMetricId.S5_R09].status is RiskMetricStatus.UNAVAILABLE_SAMPLE
    assert metrics[RiskMetricId.S5_R09].values == ()
    assert metrics[RiskMetricId.S5_R10].status is RiskMetricStatus.NOT_APPLICABLE
    assert {
        simulation.content_hash,
        run.content_hash,
        simulation.dataset_snapshot_hash,
    }.issubset(set(evidence.source_hashes))


def test_single_option_builder_calculates_premium_greeks_and_expiry_metrics() -> None:
    simulation = _single_option_evidence()
    evidence = build_option_risk_evidence(
        risk_id="risk.option.focused",
        version="v1",
        simulation_result=simulation,
        experiment_run=_run(simulation),
        evaluated_at=EVALUATED_AT,
    )
    metrics = _by_id(evidence)

    assert evidence.product_kind is RiskProductKind.OPTION
    assert metrics[RiskMetricId.S5_R10].status is RiskMetricStatus.AVAILABLE
    assert metrics[RiskMetricId.S5_R14].status is RiskMetricStatus.AVAILABLE
    assert {item.name for item in metrics[RiskMetricId.S5_R14].values} == {
        "net_delta",
        "net_gamma",
        "net_vega",
        "net_theta",
    }
    assert metrics[RiskMetricId.S5_R17].status is RiskMetricStatus.AVAILABLE
    assert metrics[RiskMetricId.S5_R18].status is RiskMetricStatus.UNAVAILABLE_SAMPLE
    assert metrics[RiskMetricId.S5_R18].values == ()
    assert (
        metrics[RiskMetricId.S5_R19].status
        is RiskMetricStatus.UNAVAILABLE_ZERO_DENOMINATOR
    )
    assert metrics[RiskMetricId.S5_R20].status is RiskMetricStatus.NOT_APPLICABLE


def test_option_robustness_risk_is_bound_to_the_same_simulated_portfolio() -> None:
    inputs, simulation = _robustness_simulation()
    executions, _summary = run_option_robustness_suite(
        suite_id="options.risk.integration",
        inputs=inputs,
        cases=standard_option_robustness_cases(
            OptionRobustnessPolicy(policy_id="options.risk.integration.v1")
        ),
    )

    evidence = build_option_risk_evidence(
        risk_id="risk.option.robustness.integrated",
        version="v1",
        simulation_result=simulation,
        experiment_run=_run(simulation),
        evaluated_at=EVALUATED_AT,
        robustness_input=inputs,
        robustness_executions=executions,
        minimum_rate_sample=1,
    )
    metrics = _by_id(evidence)

    for metric_id in (
        RiskMetricId.S5_R11,
        RiskMetricId.S5_R12,
        RiskMetricId.S5_R13,
        RiskMetricId.S5_R15,
        RiskMetricId.S5_R16,
        RiskMetricId.S5_R20,
    ):
        assert metrics[metric_id].status is RiskMetricStatus.AVAILABLE
    assert metrics[RiskMetricId.S5_R20].values[0].value == 1

    unrelated = _single_option_evidence()
    with pytest.raises(DerivativeResearchError, match="robustness_chain_mismatch"):
        build_option_risk_evidence(
            risk_id="risk.option.robustness.cross.study",
            version="v1",
            simulation_result=unrelated,
            experiment_run=_run(unrelated),
            evaluated_at=EVALUATED_AT,
            robustness_input=inputs,
            robustness_executions=executions,
        )


def test_multileg_failure_rate_requires_sample_then_is_available_at_explicit_threshold() -> (
    None
):
    simulation = _multi_leg_evidence()
    run = _run(simulation)

    insufficient = build_option_risk_evidence(
        risk_id="risk.multileg.insufficient",
        version="v1",
        simulation_result=simulation,
        experiment_run=run,
        evaluated_at=EVALUATED_AT,
    )
    available = build_option_risk_evidence(
        risk_id="risk.multileg.available",
        version="v1",
        simulation_result=simulation,
        experiment_run=run,
        evaluated_at=EVALUATED_AT,
        minimum_rate_sample=1,
    )

    assert insufficient.product_kind is RiskProductKind.MULTI_LEG
    assert (
        _by_id(insufficient)[RiskMetricId.S5_R20].status
        is RiskMetricStatus.UNAVAILABLE_SAMPLE
    )
    rate_metric = _by_id(available)[RiskMetricId.S5_R20]
    assert rate_metric.status is RiskMetricStatus.AVAILABLE
    assert rate_metric.values[0].value == 0


@pytest.mark.parametrize(
    "factory",
    [_futures_evidence, _single_option_evidence, _multi_leg_evidence],
)
def test_risk_evidence_strict_round_trip(factory: object) -> None:
    assert callable(factory)
    simulation = factory()
    if simulation.product_kind.value == "FUTURE":
        evidence = build_futures_risk_evidence(
            risk_id="risk.roundtrip.future",
            version="v1",
            simulation_result=simulation,
            experiment_run=_run(simulation),
            evaluated_at=EVALUATED_AT,
        )
    else:
        evidence = build_option_risk_evidence(
            risk_id=f"risk.roundtrip.{simulation.product_kind.value.lower()}",
            version="v1",
            simulation_result=simulation,
            experiment_run=_run(simulation),
            evaluated_at=EVALUATED_AT,
        )

    assert DerivativeRiskEvidence.from_dict(evidence.as_dict()) == evidence


def test_serialized_metric_tampering_and_unknown_fields_fail_closed() -> None:
    simulation = _single_option_evidence()
    evidence = build_option_risk_evidence(
        risk_id="risk.option.tamper",
        version="v1",
        simulation_result=simulation,
        experiment_run=_run(simulation),
        evaluated_at=EVALUATED_AT,
    )
    tampered = deepcopy(evidence.as_dict())
    tampered["metrics"][9]["values"][0]["value"] = "999999"

    with pytest.raises(DerivativeResearchError, match="content_hash_mismatch"):
        DerivativeRiskEvidence.from_dict(tampered)

    unknown = deepcopy(evidence.as_dict())
    unknown["implicit_fx_rate"] = "1"
    with pytest.raises(DerivativeResearchError, match="fields_invalid"):
        DerivativeRiskEvidence.from_dict(unknown)


def test_nested_simulation_tampering_and_precompletion_evaluation_fail_closed() -> None:
    simulation = _futures_evidence()
    run = _run(simulation)
    payload = deepcopy(simulation.simulation_payload)
    payload["steps"][0]["ledger"]["cash_balance"] = "999999999"
    object.__setattr__(
        simulation,
        "simulation_payload_json",
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )

    with pytest.raises(DerivativeResearchError, match="mismatch"):
        build_futures_risk_evidence(
            risk_id="risk.future.deep.tamper",
            version="v1",
            simulation_result=simulation,
            experiment_run=run,
            evaluated_at=EVALUATED_AT,
        )

    clean = _futures_evidence()
    with pytest.raises(DerivativeResearchError, match="evaluated_before_run_finished"):
        build_futures_risk_evidence(
            risk_id="risk.future.early",
            version="v1",
            simulation_result=clean,
            experiment_run=_run(clean),
            evaluated_at="2026-07-02T00:00:00Z",
        )
