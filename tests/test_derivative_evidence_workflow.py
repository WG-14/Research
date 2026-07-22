from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.derivatives.common import (
    DerivativeDatasetSnapshot,
    DerivativeExperimentRun,
    DerivativeExperimentSpec,
    DerivativeResearchError,
    QualityDecision,
    QualityResult,
    RunType,
)
from market_research.research.derivatives.evidence import (
    CheckStatus,
    ComparisonStatus,
    ConclusionStatus,
    CriterionResult,
    DerivativeEvidenceError,
    DerivativeEvidenceRegistry,
    DerivativeModelRefs,
    DerivativeProductKind,
    DerivativeResearchPackageManifest,
    DistributionComparison,
    EvidenceRef,
    KnowledgeEvidenceRefs,
    ProspectiveStatus,
    ProspectiveValidationEvidence,
    ProductChainEvidence,
    ReplayVerificationReceipt,
    ResearchConclusion,
    ResearchInputRefs,
    RobustnessResult,
    RobustnessStatus,
    ValidationDecision,
    ValidationStatus,
    _dataset_from_dict,
    _experiment_spec_from_dict,
    _validate_chain,
    _validate_prospective_monitoring_support,
    _validate_risk_support,
    knowledge_archive_evidence_ref,
    monitoring_artifact_evidence_ref,
    risk_artifact_evidence_ref,
    _supporting_payload_hash,
)
from market_research.research.derivatives.monitoring import (
    EXPECTED_DRIFT_METHOD,
    FrozenMonitoringSpec,
    MetricDriftRule,
    MetricObservation,
    MonitoringMetric,
    MonitoringProductKind,
    ObservationRole,
    ProspectiveMonitoringArtifact,
    evaluate_prospective_monitoring,
    required_metrics,
)
from market_research.research.derivatives.knowledge_evidence import (
    DerivativeKnowledgeEvidenceArchive,
)
from market_research.research.derivatives.simulation_evidence import (
    DerivativeSimulationEvidence,
)
from market_research.research.derivatives.risk_metrics import (
    DerivativeRiskEvidence,
    RiskMetricValue,
    build_futures_risk_evidence,
    build_option_risk_evidence,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.hypothesis_contract import parse_hypothesis_spec
from market_research.research.knowledge_contract import (
    AuthorityRef,
    DecisionAlternative,
    DecisionApprover,
    DecisionRecord,
    DecisionRisk,
    HypothesisOutcomeSpec,
    InternalHypothesisRelation,
    InternalHypothesisRelationType,
    KnowledgeRef,
    LiteratureReproductionStatus,
    LiteratureSource,
    LiteratureSourceType,
    LiteratureSpec,
)
from market_research.research.knowledge_registry import (
    export_knowledge_registry_proof,
    publish_decision_record,
    publish_hypothesis_outcome,
    publish_literature,
    publish_manifest_lineage,
)
from market_research.settings import ResearchSettings
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2


def _manager(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "datasets",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=None,
            max_workers=1,
            random_seed=17,
        ),
        project_root=Path.cwd(),
    )


def _hash(label: str) -> str:
    return sha256_prefixed({"fixture": label})


_MONITORING_VALUES: dict[MonitoringMetric, tuple[Decimal, ...]] = {
    MonitoringMetric.EXPECTED_VALUE: (Decimal("10"),),
    MonitoringMetric.WIN_RATE: (Decimal("0.6"),),
    MonitoringMetric.PNL_DISTRIBUTION: tuple(
        Decimal(value) for value in ("-2", "-1", "1", "2", "3")
    ),
    MonitoringMetric.SIGNAL_FREQUENCY: (Decimal("4"),),
    MonitoringMetric.HOLDING_PERIOD: (Decimal("60"), Decimal("300")),
    MonitoringMetric.COSTS: (Decimal("2"), Decimal("0.2")),
    MonitoringMetric.SLIPPAGE: (Decimal("1"), Decimal("3")),
    MonitoringMetric.LIQUIDITY: (Decimal("2"), Decimal("100")),
    MonitoringMetric.FEATURE_DISTRIBUTION: (
        Decimal("0"),
        Decimal("1"),
        Decimal("0.1"),
    ),
    MonitoringMetric.MARKET_REGIME: (
        Decimal("0.3"),
        Decimal("0.5"),
        Decimal("0.2"),
    ),
    MonitoringMetric.FUTURES_TERM_STRUCTURE: (
        Decimal("0.1"),
        Decimal("0.02"),
        Decimal("0.03"),
    ),
    MonitoringMetric.OPTION_SURFACE_SKEW: (
        Decimal("0.2"),
        Decimal("-0.04"),
        Decimal("0.01"),
    ),
    MonitoringMetric.GREEKS_EXPOSURE: (
        Decimal("1"),
        Decimal("0.1"),
        Decimal("10"),
        Decimal("-2"),
    ),
    MonitoringMetric.TAIL_EVENT_CONTRIBUTION: (
        Decimal("0.2"),
        Decimal("-5"),
    ),
}


def _monitoring_product(product: DerivativeProductKind) -> MonitoringProductKind:
    return MonitoringProductKind(product.value)


def _monitoring_artifact(
    *,
    product_kind: DerivativeProductKind,
    dataset_hash: str,
    prospective_dataset_hash: str,
    experiment_spec_hash: str,
    validation_decision_hash: str,
    frozen_rule_hash: str,
    baseline_source_manifest_hash: str,
    prospective_source_manifest_hash: str,
) -> ProspectiveMonitoringArtifact:
    product = _monitoring_product(product_kind)
    metrics = required_metrics(product)
    baseline = tuple(
        MetricObservation(
            observation_id=f"baseline-{product.value.lower()}-{metric.value}",
            role=ObservationRole.BASELINE,
            product_kind=product,
            metric=metric,
            period_started_at="2026-01-01T00:00:00+00:00",
            period_ended_at="2026-02-28T00:00:00+00:00",
            known_at="2026-03-01T00:00:00+00:00",
            dataset_snapshot_hash=dataset_hash,
            source_manifest_hash=baseline_source_manifest_hash,
            calculation_policy_hash=frozen_rule_hash,
            observed_count=2,
            missing_count=0,
            values=_MONITORING_VALUES[metric],
        )
        for metric in metrics
    )
    rules = tuple(
        MetricDriftRule(
            metric=metric,
            method=EXPECTED_DRIFT_METHOD[metric],
            threshold_version="synthetic_fixture_thresholds_v1",
            minimum_observed_count=2,
            maximum_missing_fraction=Decimal("0"),
            degradation_threshold=Decimal("0.2"),
            invalidation_threshold=Decimal("0.5"),
            relative_scale_floor=Decimal("0.0001"),
        )
        for metric in metrics
    )
    spec = FrozenMonitoringSpec(
        monitoring_id=f"prospective-{product_kind.value.lower().replace('_', '-')}",
        product_kind=product,
        research_rule_hash=frozen_rule_hash,
        experiment_spec_hash=experiment_spec_hash,
        validation_decision_hash=validation_decision_hash,
        baseline_observations=baseline,
        drift_rules=rules,
        frozen_at="2026-03-06T00:00:00+00:00",
        monitoring_started_at="2026-04-01T00:00:00+00:00",
    )
    current = tuple(
        MetricObservation(
            observation_id=f"current-{product.value.lower()}-{metric.value}",
            role=ObservationRole.CURRENT,
            product_kind=product,
            metric=metric,
            period_started_at="2026-04-01T00:00:00+00:00",
            period_ended_at="2026-05-01T00:00:00+00:00",
            known_at="2026-05-02T00:00:00+00:00",
            dataset_snapshot_hash=prospective_dataset_hash,
            source_manifest_hash=prospective_source_manifest_hash,
            calculation_policy_hash=frozen_rule_hash,
            observed_count=2,
            missing_count=0,
            values=_MONITORING_VALUES[metric],
            frozen_spec_hash=spec.content_hash,
        )
        for metric in metrics
    )
    return evaluate_prospective_monitoring(
        spec,
        current,
        evaluated_at="2026-05-02T00:00:00+00:00",
    )


@dataclass(frozen=True)
class _Workflow:
    dataset: DerivativeDatasetSnapshot
    experiment_spec: DerivativeExperimentSpec
    experiment_run: DerivativeExperimentRun
    simulation: DerivativeSimulationEvidence
    risk: DerivativeRiskEvidence
    knowledge_archive: DerivativeKnowledgeEvidenceArchive
    decision: ValidationDecision
    robustness: RobustnessResult
    monitoring: ProspectiveMonitoringArtifact
    prospective: ProspectiveValidationEvidence
    conclusion: ResearchConclusion
    package: DerivativeResearchPackageManifest
    supporting: dict[EvidenceRef, dict[str, object]]


class _SupportBuilder:
    def __init__(self) -> None:
        self.payloads: dict[EvidenceRef, dict[str, object]] = {}

    def add(self, authority: str, logical_id: str) -> EvidenceRef:
        payload: dict[str, object] = {
            "artifact_type": authority,
            "logical_id": logical_id,
            "immutable_fixture": True,
        }
        ref = EvidenceRef.from_payload(
            authority=authority,
            logical_id=logical_id,
            version="1",
            payload=payload,
        )
        self.payloads[ref] = payload
        return ref

    def add_payload(
        self,
        authority: str,
        logical_id: str,
        payload: dict[str, object],
    ) -> EvidenceRef:
        ref = EvidenceRef.from_payload(
            authority=authority,
            logical_id=logical_id,
            version="1",
            payload=payload,
        )
        self.payloads[ref] = payload
        return ref

    def add_domain(
        self,
        authority: str,
        logical_id: str,
        content_hash: str,
        payload: dict[str, object],
    ) -> EvidenceRef:
        ref = EvidenceRef(authority, logical_id, "1", content_hash)
        assert _supporting_payload_hash(ref, payload) == content_hash
        self.payloads[ref] = payload
        return ref


def _add_knowledge_row(
    support: _SupportBuilder, row: object
) -> tuple[EvidenceRef, KnowledgeRef]:
    assert isinstance(row, dict)
    payload = row["payload"]
    assert isinstance(payload, dict)
    knowledge_ref = KnowledgeRef(
        record_type=str(row["record_type"]),
        logical_id=str(row["logical_id"]),
        version=str(row["version"]),
        record_hash=str(row["record_hash"]),
    )
    evidence_ref = EvidenceRef(
        authority="knowledge_registry_record",
        logical_id=knowledge_ref.logical_id,
        version=knowledge_ref.version,
        content_hash=knowledge_ref.record_hash,
    )
    assert _supporting_payload_hash(evidence_ref, payload) == evidence_ref.content_hash
    support.payloads[evidence_ref] = payload
    return evidence_ref, knowledge_ref


def _risk_evidence(
    *,
    product_kind: DerivativeProductKind,
    simulation: DerivativeSimulationEvidence,
    experiment_run: DerivativeExperimentRun,
    dataset: DerivativeDatasetSnapshot,
) -> DerivativeRiskEvidence:
    assert simulation.dataset_snapshot_hash == dataset.content_hash
    risk_id = f"risk-{product_kind.value.lower().replace('_', '-')}"
    if product_kind is DerivativeProductKind.FUTURE:
        return build_futures_risk_evidence(
            risk_id=risk_id,
            version="1",
            simulation_result=simulation,
            experiment_run=experiment_run,
            evaluated_at="2026-03-04T12:00:00+00:00",
        )
    return build_option_risk_evidence(
        risk_id=risk_id,
        version="1",
        simulation_result=simulation,
        experiment_run=experiment_run,
        evaluated_at="2026-03-04T12:00:00+00:00",
    )


def _chain_evidence_from_simulation(
    simulation: DerivativeSimulationEvidence,
) -> ProductChainEvidence:
    chain = simulation.simulation_payload["product_chain"]
    assert isinstance(chain, dict)
    raw_quality = chain["quality_results"]
    assert isinstance(raw_quality, list)
    quality = tuple(
        QualityResult(
            check_id=str(item["check_id"]),
            check_version=str(item["check_version"]),
            decision=QualityDecision(str(item["decision"])),
            affected_ids=tuple(str(value) for value in item["affected_ids"]),
            diagnostics=tuple(str(value) for value in item["diagnostics"]),
        )
        for item in raw_quality
        if isinstance(item, dict)
    )
    contracts = chain["contracts"]
    assert isinstance(contracts, list)
    if simulation.product_kind.value == "FUTURE":
        availability = chain["availability"]
        assert isinstance(availability, dict)
        snapshot_id = str(chain["snapshot_id"])
        knowledge_time = str(availability["processed_at"])
        product_kind = DerivativeProductKind.FUTURE
    else:
        snapshot_id = str(chain["chain_snapshot_id"])
        knowledge_time = str(chain["knowledge_time"])
        product_kind = DerivativeProductKind.OPTION
    return ProductChainEvidence(
        chain_snapshot_id=snapshot_id,
        product_kind=product_kind,
        knowledge_time=knowledge_time,
        source_chain_hash=simulation.product_chain_hash,
        source_manifest_hashes=tuple(
            str(value) for value in chain["source_manifest_hashes"]
        ),
        universe_ids=tuple(str(item["contract_id"]) for item in contracts),
        quality_results=quality,
        chain_payload=chain,
    )


def _workflow(
    product_kind: DerivativeProductKind,
    tmp_path: Path,
) -> _Workflow:
    slug = product_kind.value.lower().replace("_", "-")
    support = _SupportBuilder()
    knowledge_manager = _manager(tmp_path / "knowledge")
    hypothesis = parse_hypothesis_spec(
        hypothesis_spec_v2(
            hypothesis_id=f"derivative-{slug}-edge",
            version="1",
            hypothesis_text=(
                f"The frozen {product_kind.value} research configuration has "
                "positive expectancy after modeled costs."
            ),
            phenomenon=f"A reproducible {product_kind.value} research effect exists.",
            mechanism=(
                "The frozen signal and execution assumptions isolate the proposed "
                "research mechanism."
            ),
            experiment_family_id=f"derivative-{slug}-family",
        )
    )
    publication = publish_manifest_lineage(
        manager=knowledge_manager, hypothesis=hypothesis
    )
    question_ref, question_knowledge_ref = _add_knowledge_row(
        support, publication["research_question"]
    )
    observation_pairs = tuple(
        _add_knowledge_row(support, row) for row in publication["observations"]
    )
    observation_refs = tuple(item[0] for item in observation_pairs)
    hypothesis_ref, hypothesis_knowledge_ref = _add_knowledge_row(
        support, publication["hypothesis"]
    )
    mechanism_ref = support.add("mechanism_registry", f"mechanism-{slug}")
    competing_ref = support.add("hypothesis_registry", f"competing-hypothesis-{slug}")
    knowledge = KnowledgeEvidenceRefs(
        question_ref=question_ref,
        observation_refs=observation_refs,
        hypothesis_ref=hypothesis_ref,
        mechanism_ref=mechanism_ref,
        competing_hypothesis_refs=(competing_ref,),
    )

    feature_ref = support.add("feature_definition_registry", f"feature-{slug}")
    from tests.test_derivative_simulation_evidence import (
        _futures_evidence,
        _multi_leg_evidence,
        _single_option_evidence,
    )

    if product_kind is DerivativeProductKind.FUTURE:
        simulation = _futures_evidence(
            hypothesis_hash=hypothesis_ref.content_hash,
            feature_hash=feature_ref.content_hash,
        )
    elif product_kind is DerivativeProductKind.OPTION:
        simulation = _single_option_evidence(
            hypothesis_hash=hypothesis_ref.content_hash,
            feature_hash=feature_ref.content_hash,
        )
    else:
        simulation = _multi_leg_evidence(
            hypothesis_hash=hypothesis_ref.content_hash,
            feature_hash=feature_ref.content_hash,
        )
    simulation_payload = simulation.simulation_payload
    dataset = _dataset_from_dict(simulation_payload["dataset_snapshot"])
    experiment_spec = _experiment_spec_from_dict(simulation_payload["experiment_spec"])
    chain_evidence = _chain_evidence_from_simulation(simulation)
    chain_ref = chain_evidence.ref()
    support.payloads[chain_ref] = chain_evidence.as_dict()

    if product_kind is DerivativeProductKind.FUTURE:
        simulator_bundle = simulation_payload["simulator"]
        assert isinstance(simulator_bundle, dict)
        cost_payload = simulator_bundle["cost_policy"]
        simulator_payload = simulator_bundle["simulator"]
        assert isinstance(cost_payload, dict)
        assert isinstance(simulator_payload, dict)
        cost_ref = support.add_domain(
            "derivative_futures_cost_model",
            f"cost-{slug}",
            str(cost_payload["content_hash"]),
            cost_payload,
        )
        fill_identity: dict[str, object] = {
            "simulator_id": simulator_payload["simulator_id"],
            "simulator_version": simulator_payload["simulator_version"],
            "method": "LISTED_CONTRACT_QUOTE_TICK_ROUNDED",
            "cost_policy_hash": cost_payload["content_hash"],
        }
        fill_payload = {
            **fill_identity,
            "content_hash": simulator_bundle["fill_model_hash"],
        }
        fill_ref = support.add_domain(
            "derivative_futures_fill_model",
            f"fill-{slug}",
            str(simulator_bundle["fill_model_hash"]),
            fill_payload,
        )
    else:
        execution_policy = simulation_payload["execution_policy"]
        assert isinstance(execution_policy, dict)
        cost_payload = {
            "policy_id": execution_policy["policy_id"],
            "policy_version": execution_policy["policy_version"],
            "fee_model": "FLAT_PER_FILLED_CONTRACT",
            "fee_per_contract": execution_policy["fee_per_contract"],
            "content_hash": execution_policy["cost_model_hash"],
        }
        fill_payload = {
            key: execution_policy[key]
            for key in (
                "policy_id",
                "policy_version",
                "fill_model_version",
                "mode",
                "slippage_ticks",
                "allow_partial",
                "allow_illiquid",
                "maximum_leg_time_skew_seconds",
            )
        }
        fill_payload["method"] = "CROSS_RECORDED_TWO_SIDED_QUOTE"
        fill_payload["content_hash"] = execution_policy["fill_model_hash"]
        cost_ref = support.add_domain(
            "derivative_option_cost_model",
            f"cost-{slug}",
            str(execution_policy["cost_model_hash"]),
            cost_payload,
        )
        fill_ref = support.add_domain(
            "derivative_option_fill_model",
            f"fill-{slug}",
            str(execution_policy["fill_model_hash"]),
            fill_payload,
        )
    settlement_ref = support.add("settlement_model_registry", f"settlement-{slug}")
    futures_roll_ref: EvidenceRef | None = None
    futures_margin_ref: EvidenceRef | None = None
    option_chain_ref: EvidenceRef | None = None
    implied_volatility_ref: EvidenceRef | None = None
    greeks_ref: EvidenceRef | None = None
    volatility_surface_ref: EvidenceRef | None = None
    exercise_ref: EvidenceRef | None = None
    assignment_ref: EvidenceRef | None = None
    multileg_ref: EvidenceRef | None = None
    tail_risk_ref: EvidenceRef | None = None
    if product_kind is DerivativeProductKind.FUTURE:
        futures_roll_ref = support.add("roll_policy_registry", f"roll-{slug}")
        futures_margin_ref = support.add("margin_policy_registry", f"margin-{slug}")
    else:
        option_chain_ref = chain_ref
        valuation_model = simulation_payload["valuation_model"]
        assert isinstance(valuation_model, dict)
        valuation_model_hash = valuation_model["content_hash"]
        assert isinstance(valuation_model_hash, str)
        implied_volatility_ref = support.add_payload(
            "iv_model_registry",
            f"iv-{slug}",
            {
                "schema_version": 2,
                "artifact_type": "derivative_option_valuation_model_authority",
                "role": "IMPLIED_VOLATILITY",
                "valuation_model": valuation_model,
                "valuation_model_hash": valuation_model_hash,
            },
        )
        greeks_ref = support.add_payload(
            "greeks_model_registry",
            f"greeks-{slug}",
            {
                "schema_version": 2,
                "artifact_type": "derivative_option_valuation_model_authority",
                "role": "GREEKS",
                "valuation_model": valuation_model,
                "valuation_model_hash": valuation_model_hash,
            },
        )
        volatility_surface_ref = support.add(
            "surface_model_registry", f"surface-{slug}"
        )
        exercise_ref = support.add("exercise_policy_registry", f"exercise-{slug}")
        assignment_ref = support.add("assignment_policy_registry", f"assignment-{slug}")
        tail_risk_ref = support.add("tail_risk_policy_registry", f"tail-{slug}")
        if product_kind is DerivativeProductKind.MULTI_LEG:
            multileg_ref = support.add("multileg_policy_registry", f"multileg-{slug}")
    models = DerivativeModelRefs(
        model_bundle_id=f"models-{slug}",
        version="1",
        product_kind=product_kind,
        cost_model_ref=cost_ref,
        fill_model_ref=fill_ref,
        settlement_model_ref=settlement_ref,
        futures_roll_ref=futures_roll_ref,
        futures_margin_ref=futures_margin_ref,
        option_chain_ref=option_chain_ref,
        implied_volatility_ref=implied_volatility_ref,
        greeks_ref=greeks_ref,
        volatility_surface_ref=volatility_surface_ref,
        exercise_ref=exercise_ref,
        assignment_ref=assignment_ref,
        multileg_ref=multileg_ref,
        tail_risk_ref=tail_risk_ref,
    )

    observation_datasets = simulation_payload.get("lifecycle_dataset_snapshots", [])
    assert isinstance(observation_datasets, list)
    observation_hashes = tuple(
        str(item["content_hash"])
        for item in observation_datasets
        if isinstance(item, dict)
    )
    experiment_run = DerivativeExperimentRun(
        run_id=f"run-{slug}",
        experiment_spec_hash=experiment_spec.content_hash,
        dataset_snapshot_hash=dataset.content_hash,
        started_at="2026-03-03T00:00:00+00:00",
        finished_at=(
            "2026-07-03T00:00:00+00:00"
            if observation_hashes
            else "2026-03-03T02:00:00+00:00"
        ),
        status="SUCCEEDED",
        event_stream_hash=simulation.event_stream_hash,
        result_artifact_hash=simulation.content_hash,
        observation_dataset_snapshot_hashes=observation_hashes,
    )
    risk = _risk_evidence(
        product_kind=product_kind,
        simulation=simulation,
        experiment_run=experiment_run,
        dataset=dataset,
    )
    risk_ref = risk_artifact_evidence_ref(risk)
    support.payloads[risk_ref] = risk.as_dict()
    inputs = ResearchInputRefs(
        dataset_snapshot_ref=EvidenceRef(
            "derivative_dataset_snapshot",
            dataset.snapshot_id,
            str(dataset.schema_version),
            dataset.content_hash,
        ),
        chain_snapshot_refs=(chain_ref,),
        feature_definition_refs=(feature_ref,),
        experiment_spec_ref=EvidenceRef(
            "derivative_experiment_spec",
            experiment_spec.experiment_id,
            str(experiment_spec.schema_version),
            experiment_spec.content_hash,
        ),
        experiment_run_ref=EvidenceRef(
            "derivative_experiment_run",
            experiment_run.run_id,
            str(experiment_run.schema_version),
            experiment_run.content_hash,
        ),
    )

    validation_check_ref = support.add(
        "validation_metric_registry", f"validation-metrics-{slug}"
    )
    simulation_ref = EvidenceRef.from_payload(
        authority="derivative_simulation",
        logical_id=simulation.simulation_id,
        version="1",
        payload=simulation.as_dict(),
    )
    support.payloads[simulation_ref] = simulation.as_dict()
    decision = ValidationDecision(
        decision_id=f"decision-{slug}",
        version="1",
        product_kind=product_kind,
        knowledge=knowledge,
        inputs=inputs,
        models=models,
        criterion_results=(
            CriterionResult(
                criterion_id="economic-and-statistical-gates",
                status=CheckStatus.PASS,
                evidence_refs=(simulation_ref, validation_check_ref),
                rationale="All preregistered validation gates passed after costs.",
            ),
        ),
        status=ValidationStatus.PASS,
        failure_reasons=(),
        limitations=("Externally prepared synthetic fixture coverage is short.",),
        decided_by="derivatives-reviewer",
        decided_at="2026-03-04T00:00:00+00:00",
    )

    robustness_scenario_ref = support.add(
        "robustness_scenario_registry", f"scenario-{slug}"
    )
    robustness_check_ref = support.add(
        "robustness_metric_registry", f"robustness-metrics-{slug}"
    )
    robustness = RobustnessResult(
        robustness_id=f"robustness-{slug}",
        version="1",
        product_kind=product_kind,
        validation_decision_ref=decision.ref(),
        experiment_run_ref=inputs.experiment_run_ref,
        risk_evidence_ref=risk_ref,
        scenario_refs=(robustness_scenario_ref,),
        criterion_results=(
            CriterionResult(
                criterion_id="cost-delay-tail-stress",
                status=CheckStatus.PASS,
                evidence_refs=(robustness_check_ref, risk_ref),
                rationale="Frozen adverse scenarios remained within acceptance bounds.",
            ),
        ),
        status=RobustnessStatus.PASS,
        failure_modes=(),
        limitations=("Stress scenarios do not exhaust all market states.",),
        evaluated_at="2026-03-05T00:00:00+00:00",
    )

    frozen_rule_ref = support.add("frozen_rule_registry", f"rule-{slug}")
    prospective_dataset_ref = support.add(
        "prospective_dataset_registry", f"prospective-dataset-{slug}"
    )
    observation_stream_ref = support.add(
        "prospective_stream_registry", f"prospective-stream-{slug}"
    )
    historical_distribution_ref = support.add(
        "distribution_registry", f"historical-distribution-{slug}"
    )
    prospective_distribution_ref = support.add(
        "distribution_registry", f"prospective-distribution-{slug}"
    )
    comparison_ref = support.add(
        "distribution_comparison_registry", f"distribution-comparison-{slug}"
    )
    monitoring = _monitoring_artifact(
        product_kind=product_kind,
        dataset_hash=dataset.content_hash,
        prospective_dataset_hash=prospective_dataset_ref.content_hash,
        experiment_spec_hash=experiment_spec.content_hash,
        validation_decision_hash=decision.content_hash,
        frozen_rule_hash=frozen_rule_ref.content_hash,
        baseline_source_manifest_hash=dataset.raw_manifest_hashes[0],
        prospective_source_manifest_hash=prospective_dataset_ref.content_hash,
    )
    monitoring_ref = monitoring_artifact_evidence_ref(monitoring.reference())
    support.payloads[monitoring_ref] = monitoring.as_dict()
    prospective_observations = tuple(
        support.add_payload(
            "prospective_observation_registry",
            f"prospective-obs-{index}-{slug}",
            {
                "artifact_type": "prospective_monitoring_observation_batch",
                "logical_id": f"prospective-obs-{index}-{slug}",
                "dataset_snapshot_hash": prospective_dataset_ref.content_hash,
                "source_manifest_hash": prospective_dataset_ref.content_hash,
                "calculation_policy_hash": frozen_rule_ref.content_hash,
                "period_started_at": "2026-04-01T00:00:00+00:00",
                "period_ended_at": "2026-05-01T00:00:00+00:00",
                "monitoring_observation_hashes": [
                    item.content_hash for item in monitoring.current_observations
                ],
            },
        )
        for index in (1, 2)
    )
    prospective = ProspectiveValidationEvidence(
        prospective_id=f"prospective-{slug}",
        version="1",
        product_kind=product_kind,
        validation_decision_ref=decision.ref(),
        robustness_result_ref=robustness.ref(),
        frozen_model_bundle_ref=models.ref(),
        frozen_rule_ref=frozen_rule_ref,
        prospective_dataset_ref=prospective_dataset_ref,
        monitoring_artifact_ref=monitoring.reference(),
        observation_refs=prospective_observations,
        observation_stream_ref=observation_stream_ref,
        historical_distribution_ref=historical_distribution_ref,
        prospective_distribution_ref=prospective_distribution_ref,
        distribution_comparisons=(
            DistributionComparison(
                metric_id="expected-value-after-costs",
                historical_value="0.012",
                prospective_value="0.011",
                status=ComparisonStatus.PASS,
                evidence_ref=comparison_ref,
            ),
        ),
        frozen_at="2026-03-06T00:00:00+00:00",
        period_start="2026-04-01T00:00:00+00:00",
        period_end="2026-05-01T00:00:00+00:00",
        evaluated_at="2026-05-02T00:00:00+00:00",
        minimum_observations=2,
        observation_count=2,
        missing_count=0,
        late_count=0,
        maximum_missing_rate="0",
        maximum_late_rate="0",
        maximum_delay_seconds="30",
        observed_maximum_delay_seconds="5",
        parameter_change_count=0,
        status=ProspectiveStatus.CONFIRMED,
        limitations=("The prospective period remains short.",),
    )
    applicability = (
        "Offline research for the frozen instrument universe and settlement regime.",
    )
    invalidation = (
        "Invalidate when the frozen prospective distribution guard is crossed.",
    )
    conclusion = ResearchConclusion(
        conclusion_id=f"conclusion-{slug}",
        version="1",
        product_kind=product_kind,
        hypothesis_ref=hypothesis_ref,
        validation_decision_ref=decision.ref(),
        robustness_result_ref=robustness.ref(),
        risk_evidence_ref=risk_ref,
        prospective_validation_ref=prospective.ref(),
        status=ConclusionStatus.CONFIRMED,
        rationale="The frozen prospective evidence confirmed the research hypothesis.",
        applicability=applicability,
        invalidation_criteria=invalidation,
        limitations=("The prospective period remains short.",),
        decided_by="derivatives-conclusion-reviewer",
        decided_at="2026-05-03T00:00:00+00:00",
    )
    literature = LiteratureSpec(
        schema_version=2,
        literature_id=f"literature-{slug}-cost-pit",
        version="2",
        title=f"Cost- and point-in-time-aware {product_kind.value} research",
        citation="Synthetic fixture literature contract (2025)",
        actor_id="derivatives-literature-reviewer",
        recorded_at="2026-02-01T00:00:00+00:00",
        source=LiteratureSource(
            source_type=LiteratureSourceType.TECHNICAL_REPORT,
            publisher="Offline Research Fixture Authority",
            locator=f"urn:market-research:literature:{slug}:cost-pit",
            content_hash=_hash(f"literature-source-{slug}"),
        ),
        published_at="2025-12-01T00:00:00+00:00",
        accessed_at="2026-01-31T00:00:00+00:00",
        key_claims=(
            "Modeled costs materially affect derivative research conclusions.",
            "Point-in-time inputs are required to prevent information leakage.",
        ),
        reproduction_status=LiteratureReproductionStatus.REPRODUCED,
        reproduction_evidence_hashes=(simulation.content_hash, risk.content_hash),
        internal_hypothesis_relations=(
            InternalHypothesisRelation(
                hypothesis_ref=hypothesis_knowledge_ref,
                relation=InternalHypothesisRelationType.CONTEXTUALIZES,
                rationale=(
                    "The reviewed claims define the frozen cost and knowledge-time "
                    "assumptions used by this hypothesis."
                ),
            ),
        ),
    )
    outcome = HypothesisOutcomeSpec(
        schema_version=2,
        outcome_id=f"outcome-{slug}-prospective",
        version="2",
        hypothesis_ref=hypothesis_knowledge_ref,
        question_ref=question_knowledge_ref,
        outcome="supported",
        rationale=(
            "The frozen validation, robustness, risk, and prospective evidence "
            "support the research-only conclusion."
        ),
        actor_id="derivatives-outcome-reviewer",
        recorded_at="2026-05-03T01:00:00+00:00",
        evidence_hashes=(conclusion.content_hash, prospective.content_hash),
    )
    publish_literature(manager=knowledge_manager, literature=literature)
    publish_hypothesis_outcome(manager=knowledge_manager, outcome=outcome)
    knowledge_decision = DecisionRecord(
        schema_version=1,
        decision_id=f"decision-{slug}-conclusion",
        version="1",
        decision_type="derivative_research_conclusion",
        subject=AuthorityRef(
            authority="derivative_research_conclusion",
            subject_type="research_conclusion",
            subject_id=conclusion.conclusion_id,
            subject_version=conclusion.version,
            authority_hash=conclusion.content_hash,
        ),
        chosen_action="confirm_research_only_conclusion",
        rationale=(
            "The immutable evidence graph and detached knowledge proofs support "
            "the stated research-only conclusion."
        ),
        evidence_hashes=(
            conclusion.content_hash,
            outcome.contract_hash(),
            literature.contract_hash(),
        ),
        alternatives=(
            DecisionAlternative(
                alternative_id="remain-inconclusive",
                description="Keep the research conclusion inconclusive.",
                rejection_reason=(
                    "The frozen prospective and robustness criteria passed."
                ),
            ),
        ),
        expected_effects=(
            "The conclusion may be retained as offline research evidence only.",
        ),
        risks=(
            DecisionRisk(
                risk_id="synthetic-evidence-limit",
                description="Synthetic fixtures do not establish a market claim.",
                severity="high",
                mitigation=(
                    "Retain research-only scope and require external E5 evidence "
                    "before any stronger claim."
                ),
            ),
        ),
        proposer_ids=("derivatives-conclusion-reviewer",),
        approver=DecisionApprover(
            approver_type="human",
            approver_id="derivatives-knowledge-approver",
            role="research_knowledge_approver",
        ),
        policy_version="derivative-knowledge-decision.v1",
        decided_at="2026-05-03T03:00:00+00:00",
    )
    publish_decision_record(manager=knowledge_manager, decision=knowledge_decision)
    knowledge_archive = DerivativeKnowledgeEvidenceArchive(
        archive_id=f"knowledge-archive-{slug}",
        version="1",
        conclusion_id=conclusion.conclusion_id,
        conclusion_version=conclusion.version,
        conclusion_hash=conclusion.content_hash,
        outcome_proof=export_knowledge_registry_proof(
            manager=knowledge_manager, target_ref=outcome.ref()
        ),
        literature_proofs=(
            export_knowledge_registry_proof(
                manager=knowledge_manager, target_ref=literature.ref()
            ),
        ),
        decision_proof=export_knowledge_registry_proof(
            manager=knowledge_manager, target_ref=knowledge_decision.ref()
        ),
        assembled_at="2026-05-03T04:00:00+00:00",
    )
    knowledge_archive_ref = knowledge_archive_evidence_ref(knowledge_archive)
    support.payloads[knowledge_archive_ref] = knowledge_archive.as_dict()
    package = DerivativeResearchPackageManifest(
        package_id=f"package-{slug}",
        version="1",
        product_kind=product_kind,
        knowledge=knowledge,
        knowledge_archive_ref=knowledge_archive_ref,
        inputs=inputs,
        models=models,
        validation_decision_ref=decision.ref(),
        robustness_result_ref=robustness.ref(),
        risk_evidence_ref=risk_ref,
        prospective_validation_ref=prospective.ref(),
        research_conclusion_ref=conclusion.ref(),
        applicability=applicability,
        invalidation_criteria=invalidation,
        limitations=(
            "Externally prepared synthetic fixture coverage is short.",
            "Stress scenarios do not exhaust all market states.",
            "The prospective period remains short.",
        ),
        reproduction_command=(
            "market-research",
            "research-derivative-replay",
            "--bundle",
            f"/external/{slug}-bundle.json",
            "--verified-at",
            "2026-05-05T00:00:00+00:00",
        ),
        created_by="derivatives-package-reviewer",
        created_at="2026-05-04T00:00:00+00:00",
    )
    return _Workflow(
        dataset=dataset,
        experiment_spec=experiment_spec,
        experiment_run=experiment_run,
        simulation=simulation,
        risk=risk,
        knowledge_archive=knowledge_archive,
        decision=decision,
        robustness=robustness,
        monitoring=monitoring,
        prospective=prospective,
        conclusion=conclusion,
        package=package,
        supporting=support.payloads,
    )


def _register(registry: DerivativeEvidenceRegistry, workflow: _Workflow) -> None:
    registry.register(
        workflow.package,
        dataset=workflow.dataset,
        experiment_spec=workflow.experiment_spec,
        experiment_run=workflow.experiment_run,
        decision=workflow.decision,
        robustness=workflow.robustness,
        prospective=workflow.prospective,
        conclusion=workflow.conclusion,
        supporting_evidence=workflow.supporting,
    )


@pytest.mark.parametrize("product_kind", tuple(DerivativeProductKind))
def test_common_dataset_experiment_run_flows_to_replayable_package(
    tmp_path: Path, product_kind: DerivativeProductKind
) -> None:
    workflow = _workflow(product_kind, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))

    package_ref = registry.register(
        workflow.package,
        dataset=workflow.dataset,
        experiment_spec=workflow.experiment_spec,
        experiment_run=workflow.experiment_run,
        decision=workflow.decision,
        robustness=workflow.robustness,
        prospective=workflow.prospective,
        conclusion=workflow.conclusion,
        supporting_evidence=workflow.supporting,
    )
    assert package_ref == workflow.package.ref()
    assert registry.resolve(workflow.package.package_id, "1") == workflow.package
    monitoring_ref = monitoring_artifact_evidence_ref(workflow.monitoring.reference())
    assert registry.resolve_ref(monitoring_ref) == workflow.monitoring.as_dict()
    risk_ref = risk_artifact_evidence_ref(workflow.risk)
    assert registry.resolve_ref(risk_ref) == workflow.risk.as_dict()
    knowledge_archive_ref = knowledge_archive_evidence_ref(workflow.knowledge_archive)
    assert (
        registry.resolve_ref(knowledge_archive_ref)
        == workflow.knowledge_archive.as_dict()
    )

    receipt = registry.verify_replay(
        workflow.package.package_id,
        "1",
        dataset=workflow.dataset,
        experiment_spec=workflow.experiment_spec,
        experiment_run=workflow.experiment_run,
        decision=workflow.decision,
        robustness=workflow.robustness,
        prospective=workflow.prospective,
        conclusion=workflow.conclusion,
        supporting_evidence=workflow.supporting,
        verified_at="2026-05-05T00:00:00+00:00",
    )
    assert receipt.status == "PASS"
    assert receipt.package_ref == package_ref
    assert receipt.simulation_result_ref.authority == "derivative_simulation"
    assert receipt.risk_evidence_ref == risk_ref
    assert receipt.knowledge_archive_ref == knowledge_archive_ref
    assert ReplayVerificationReceipt.from_dict(receipt.as_dict()) == receipt
    assert not registry.manager.is_within(
        registry.evidence_path(package_ref), registry.manager.project_root
    )


@pytest.mark.parametrize("model_ref_name", ("implied_volatility_ref", "greeks_ref"))
def test_option_model_authorities_bind_the_exact_frozen_valuation_model(
    tmp_path: Path, model_ref_name: str
) -> None:
    workflow = _workflow(DerivativeProductKind.OPTION, tmp_path)
    ref = getattr(workflow.package.models, model_ref_name)
    assert isinstance(ref, EvidenceRef)
    supporting = dict(workflow.supporting)
    changed = deepcopy(supporting[ref])
    model = changed["valuation_model"]
    assert isinstance(model, dict)
    model["model_version"] = "different_black_scholes_version"
    model["content_hash"] = sha256_prefixed(
        {key: value for key, value in model.items() if key != "content_hash"},
        label="option_valuation_model",
    )
    changed["valuation_model_hash"] = model["content_hash"]
    supporting[ref] = changed

    with pytest.raises(
        DerivativeEvidenceError, match="valuation_model_spec_binding_mismatch"
    ):
        DerivativeEvidenceRegistry(_manager(tmp_path)).register(
            workflow.package,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
            supporting_evidence=supporting,
        )


def test_register_rejects_monitoring_bound_to_another_prospective_dataset(
    tmp_path: Path,
) -> None:
    workflow = _workflow(DerivativeProductKind.OPTION, tmp_path)
    wrong_current = tuple(
        replace(item, dataset_snapshot_hash=_hash("wrong-prospective-dataset"))
        for item in workflow.monitoring.current_observations
    )
    wrong_monitoring = evaluate_prospective_monitoring(
        workflow.monitoring.spec,
        wrong_current,
        evaluated_at=workflow.monitoring.evaluated_at,
    )
    wrong_prospective = replace(
        workflow.prospective,
        monitoring_artifact_ref=wrong_monitoring.reference(),
    )
    wrong_conclusion = replace(
        workflow.conclusion,
        prospective_validation_ref=wrong_prospective.ref(),
    )
    wrong_package = replace(
        workflow.package,
        prospective_validation_ref=wrong_prospective.ref(),
        research_conclusion_ref=wrong_conclusion.ref(),
    )
    supporting = dict(workflow.supporting)
    supporting.pop(monitoring_artifact_evidence_ref(workflow.monitoring.reference()))
    supporting[monitoring_artifact_evidence_ref(wrong_monitoring.reference())] = (
        wrong_monitoring.as_dict()
    )

    with pytest.raises(
        DerivativeEvidenceError,
        match="prospective_monitoring_current_dataset_mismatch",
    ):
        DerivativeEvidenceRegistry(_manager(tmp_path)).register(
            wrong_package,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=wrong_prospective,
            conclusion=wrong_conclusion,
            supporting_evidence=supporting,
        )


def test_resolve_strictly_reparses_monitoring_payload(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.FUTURE, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    monitoring_ref = monitoring_artifact_evidence_ref(workflow.monitoring.reference())
    path = registry.evidence_path(monitoring_ref)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["current_observations"][0]["values"][0] = "999"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        DerivativeEvidenceError,
        match="prospective_monitoring_payload_invalid",
    ):
        registry.resolve(workflow.package.package_id, workflow.package.version)


def test_replay_strictly_reparses_supplied_monitoring_payload(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.MULTI_LEG, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    monitoring_ref = monitoring_artifact_evidence_ref(workflow.monitoring.reference())
    supporting = dict(workflow.supporting)
    changed = deepcopy(supporting[monitoring_ref])
    current = changed["current_observations"]
    assert isinstance(current, list)
    first = current[0]
    assert isinstance(first, dict)
    values = first["values"]
    assert isinstance(values, list)
    values[0] = "999"
    supporting[monitoring_ref] = changed

    with pytest.raises(
        DerivativeEvidenceError,
        match="prospective_monitoring_payload_invalid",
    ):
        registry.verify_replay(
            workflow.package.package_id,
            workflow.package.version,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
            supporting_evidence=supporting,
            verified_at="2026-05-05T00:00:00+00:00",
        )


def test_resolve_strictly_reparses_risk_payload(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.OPTION, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    risk_ref = risk_artifact_evidence_ref(workflow.risk)
    path = registry.evidence_path(risk_ref)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metrics"][0]["reason"] = "tampered-risk-reason"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        DerivativeEvidenceError,
        match="derivative_risk_payload_invalid",
    ):
        registry.resolve(workflow.package.package_id, workflow.package.version)


def test_resolve_strictly_reparses_knowledge_archive_payload(
    tmp_path: Path,
) -> None:
    workflow = _workflow(DerivativeProductKind.OPTION, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    archive_ref = knowledge_archive_evidence_ref(workflow.knowledge_archive)
    path = registry.evidence_path(archive_ref)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["decision_proof"]["rows"][-1]["payload"]["chosen_action"] = (
        "tampered-decision"
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        DerivativeEvidenceError,
        match="derivative_knowledge_archive_payload_invalid",
    ):
        registry.resolve(workflow.package.package_id, workflow.package.version)


def test_register_requires_bound_knowledge_archive_payload(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.FUTURE, tmp_path)
    supporting = dict(workflow.supporting)
    supporting.pop(knowledge_archive_evidence_ref(workflow.knowledge_archive))

    with pytest.raises(
        DerivativeEvidenceError,
        match="derivative_supporting_evidence_missing",
    ):
        DerivativeEvidenceRegistry(_manager(tmp_path)).register(
            workflow.package,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
            supporting_evidence=supporting,
        )


def test_register_requires_bound_risk_payload(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.FUTURE, tmp_path)
    supporting = dict(workflow.supporting)
    supporting.pop(risk_artifact_evidence_ref(workflow.risk))

    with pytest.raises(
        DerivativeEvidenceError,
        match="derivative_supporting_evidence_missing",
    ):
        DerivativeEvidenceRegistry(_manager(tmp_path)).register(
            workflow.package,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
            supporting_evidence=supporting,
        )


def test_replay_strictly_reparses_supplied_risk_payload(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.MULTI_LEG, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    risk_ref = risk_artifact_evidence_ref(workflow.risk)
    supporting = dict(workflow.supporting)
    changed = deepcopy(supporting[risk_ref])
    metrics = changed["metrics"]
    assert isinstance(metrics, list)
    first = metrics[0]
    assert isinstance(first, dict)
    first["reason"] = "tampered-risk-reason"
    supporting[risk_ref] = changed

    with pytest.raises(
        DerivativeEvidenceError,
        match="derivative_risk_payload_invalid",
    ):
        registry.verify_replay(
            workflow.package.package_id,
            workflow.package.version,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
            supporting_evidence=supporting,
            verified_at="2026-05-05T00:00:00+00:00",
        )


def test_package_requires_one_typed_simulation_result_reference(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.FUTURE, tmp_path)
    criterion = workflow.decision.criterion_results[0]
    without_simulation = replace(
        workflow.decision,
        criterion_results=(
            replace(
                criterion,
                evidence_refs=tuple(
                    ref
                    for ref in criterion.evidence_refs
                    if ref.authority != "derivative_simulation"
                ),
            ),
        ),
    )

    with pytest.raises(
        DerivativeEvidenceError,
        match="requires_one_simulation_result",
    ):
        DerivativeEvidenceRegistry(_manager(tmp_path)).register(
            workflow.package,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=without_simulation,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
            supporting_evidence=workflow.supporting,
        )


def test_replay_compares_every_supporting_payload_not_only_internal_graph(
    tmp_path: Path,
) -> None:
    workflow = _workflow(DerivativeProductKind.OPTION, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    incomplete = dict(workflow.supporting)
    incomplete.pop(next(iter(incomplete)))

    with pytest.raises(
        DerivativeEvidenceError,
        match="replay_supporting_evidence_set_mismatch",
    ):
        registry.verify_replay(
            workflow.package.package_id,
            "1",
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
            supporting_evidence=incomplete,
            verified_at="2026-05-05T00:00:00+00:00",
        )


@pytest.mark.parametrize(
    ("product_kind", "missing_field", "message"),
    (
        (
            DerivativeProductKind.FUTURE,
            "futures_margin_ref",
            "future_roll_and_margin_refs_required",
        ),
        (
            DerivativeProductKind.OPTION,
            "greeks_ref",
            "option_model_refs_incomplete",
        ),
        (
            DerivativeProductKind.MULTI_LEG,
            "multileg_ref",
            "multileg_model_refs_incomplete",
        ),
    ),
)
def test_product_discriminator_rejects_incomplete_model_refs(
    tmp_path: Path,
    product_kind: DerivativeProductKind,
    missing_field: str,
    message: str,
) -> None:
    workflow = _workflow(product_kind, tmp_path)
    with pytest.raises(DerivativeEvidenceError, match=message):
        if missing_field == "futures_margin_ref":
            replace(workflow.package.models, futures_margin_ref=None)
        elif missing_field == "greeks_ref":
            replace(workflow.package.models, greeks_ref=None)
        else:
            replace(workflow.package.models, multileg_ref=None)


@pytest.mark.parametrize(
    "field_name",
    (
        "live_approval",
        "liveApproval",
        "live-approval",
        "account_id",
        "accountId",
        "account-id",
        "deployment_target",
        "deploymentTarget",
        "capital_allocation",
        "capital-allocation",
        "brokerAPIKey",
        "orderRouter",
        "private-exchange",
        "networkMarketDataCollection",
    ),
)
def test_package_parser_rejects_live_authority_fields(
    tmp_path: Path, field_name: str
) -> None:
    payload = _workflow(DerivativeProductKind.FUTURE, tmp_path).package.as_dict()
    payload[field_name] = "forbidden"
    with pytest.raises(
        DerivativeEvidenceError, match="derivative_package_live_field_forbidden"
    ):
        DerivativeResearchPackageManifest.from_dict(payload)


@pytest.mark.parametrize(
    "forbidden_argument",
    (
        "--liveAccount",
        "--live-account",
        "--submitOrder",
        "--submit-order",
        "--orderRouter",
        "--private-exchange",
        "--networkMarketData",
    ),
)
def test_package_reproduction_command_rejects_forbidden_value_aliases(
    tmp_path: Path, forbidden_argument: str
) -> None:
    package = _workflow(DerivativeProductKind.FUTURE, tmp_path).package

    with pytest.raises(
        DerivativeEvidenceError,
        match="package_reproduction_command_not_research_only",
    ):
        replace(
            package,
            reproduction_command=(
                "market-research",
                "research-derivative-replay",
                forbidden_argument,
            ),
        )


def test_missing_supporting_ref_fails_before_any_publication(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.OPTION, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    incomplete = dict(workflow.supporting)
    incomplete.pop(next(iter(incomplete)))

    with pytest.raises(
        DerivativeEvidenceError, match="derivative_supporting_evidence_missing"
    ):
        registry.register(
            workflow.package,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
            supporting_evidence=incomplete,
        )

    assert not registry.evidence_path(workflow.package.ref()).exists()


def test_resolve_rejects_tampered_decision_artifact(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.MULTI_LEG, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    decision_path = registry.evidence_path(workflow.decision.ref())
    payload = json.loads(decision_path.read_text(encoding="utf-8"))
    payload["decided_by"] = "tampered-reviewer"
    decision_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DerivativeEvidenceError, match="content_hash_mismatch"):
        registry.resolve(workflow.package.package_id, "1")


def test_duplicate_identity_conflict_preserves_first_package(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.FUTURE, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    package_path = registry.evidence_path(workflow.package.ref())
    original = package_path.read_bytes()
    conflicting = replace(
        workflow.package,
        limitations=(*workflow.package.limitations, "Conflicting same-version claim."),
    )

    with pytest.raises(
        DerivativeEvidenceError, match="derivative_evidence_identity_conflict"
    ):
        registry.register(
            conflicting,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
            supporting_evidence=workflow.supporting,
        )

    assert package_path.read_bytes() == original
    assert registry.resolve(workflow.package.package_id, "1") == workflow.package


def test_replay_rejects_changed_common_run(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.OPTION, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    changed_run = replace(
        workflow.experiment_run,
        result_artifact_hash=_hash("different-result"),
    )

    with pytest.raises(DerivativeEvidenceError, match="experiment_run_ref_mismatch"):
        registry.verify_replay(
            workflow.package.package_id,
            "1",
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=changed_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
            supporting_evidence=workflow.supporting,
            verified_at="2026-05-05T00:00:00+00:00",
        )


def test_diff_resolves_versioned_package_and_predecessor(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.FUTURE, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    revised = replace(
        workflow.package,
        version="2",
        limitations=(*workflow.package.limitations, "Second review limitation."),
        reproduction_command=(
            "market-research",
            "research-derivative-replay",
            "--bundle",
            "/external/future-bundle-v2.json",
            "--verified-at",
            "2026-05-05T00:00:00+00:00",
        ),
        supersedes=workflow.package.ref(),
    )
    registry.register(
        revised,
        dataset=workflow.dataset,
        experiment_spec=workflow.experiment_spec,
        experiment_run=workflow.experiment_run,
        decision=workflow.decision,
        robustness=workflow.robustness,
        prospective=workflow.prospective,
        conclusion=workflow.conclusion,
        supporting_evidence=workflow.supporting,
    )

    difference = registry.diff(
        workflow.package.package_id, "1", revised.package_id, "2"
    )
    assert difference["same_content"] is False
    changed_paths = difference["changed_paths"]
    assert isinstance(changed_paths, list)
    assert "$.version" in changed_paths
    assert "$.limitations[3]" in changed_paths
    assert "$.supersedes" in changed_paths


def test_conclusion_cannot_substitute_an_unrelated_prospective_ref(
    tmp_path: Path,
) -> None:
    workflow = _workflow(DerivativeProductKind.FUTURE, tmp_path)
    other = EvidenceRef.from_payload(
        authority="derivative_prospective_validation",
        logical_id="other-prospective",
        version="1",
        payload={"other": True},
    )
    substituted = replace(workflow.conclusion, prospective_validation_ref=other)
    package = replace(workflow.package, research_conclusion_ref=substituted.ref())
    with pytest.raises(
        DerivativeEvidenceError, match="conclusion_upstream_ref_mismatch"
    ):
        DerivativeEvidenceRegistry(_manager(tmp_path)).register(
            package,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=substituted,
            supporting_evidence=workflow.supporting,
        )


def test_failed_product_chain_quality_cannot_enter_confirmatory_package(
    tmp_path: Path,
) -> None:
    del tmp_path
    from market_research.research.derivatives.options import OptionChainSnapshot
    from tests.test_options_derivative_research import (
        NOW,
        _contract as option_contract,
        _hash as option_hash,
        _quote as option_quote,
    )

    contract = option_contract("option.failed.chain")
    chain = OptionChainSnapshot(
        chain_snapshot_id="option-chain-failed-quality",
        underlying_id=contract.underlying_id,
        knowledge_time=NOW,
        underlying_price=Decimal("100"),
        contracts=(contract,),
        quotes=(option_quote(contract),),
        source_manifest_hashes=(option_hash("a"),),
        quality_results=(
            QualityResult(
                check_id="chain-completeness",
                check_version="1",
                decision=QualityDecision.FAILED,
            ),
        ),
    )
    chain_evidence = ProductChainEvidence.from_option_chain(chain)

    with pytest.raises(
        DerivativeResearchError,
        match="confirmatory_dataset_quality_blocked:chain-completeness",
    ):
        chain_evidence.admit(RunType.CONFIRMATORY)


def test_risk_support_recomputes_metrics_instead_of_trusting_valid_hashes(
    tmp_path: Path,
) -> None:
    workflow = _workflow(DerivativeProductKind.FUTURE, tmp_path)
    original = workflow.risk.metrics[0]
    forged_metric = replace(
        original,
        values=(
            RiskMetricValue(
                name=original.values[0].name,
                value=Decimal("999999999"),
                unit=original.values[0].unit,
            ),
        ),
    )
    forged = replace(
        workflow.risk,
        metrics=(forged_metric, *workflow.risk.metrics[1:]),
    )
    forged_ref = risk_artifact_evidence_ref(forged)

    with pytest.raises(DerivativeEvidenceError, match="semantic_replay_mismatch"):
        _validate_risk_support(
            package=replace(workflow.package, risk_evidence_ref=forged_ref),
            dataset=workflow.dataset,
            experiment_run=workflow.experiment_run,
            robustness=replace(workflow.robustness, risk_evidence_ref=forged_ref),
            simulation=workflow.simulation,
            supporting_evidence={forged_ref: forged.as_dict()},
        )


@pytest.mark.parametrize(
    "field_name",
    ("source_manifest_hash", "calculation_policy_hash"),
)
def test_monitoring_rejects_unresolved_current_provenance(
    tmp_path: Path,
    field_name: str,
) -> None:
    workflow = _workflow(DerivativeProductKind.OPTION, tmp_path)
    changed = tuple(
        replace(item, **{field_name: _hash(f"unrelated-{field_name}")})
        for item in workflow.monitoring.current_observations
    )
    monitoring = evaluate_prospective_monitoring(
        workflow.monitoring.spec,
        changed,
        evaluated_at=workflow.monitoring.evaluated_at,
    )
    prospective = replace(
        workflow.prospective,
        monitoring_artifact_ref=monitoring.reference(),
    )
    support = {
        monitoring_artifact_evidence_ref(monitoring.reference()): monitoring.as_dict()
    }
    support.update(
        {ref: workflow.supporting[ref] for ref in workflow.prospective.observation_refs}
    )

    with pytest.raises(DerivativeEvidenceError, match="current_.*_mismatch"):
        _validate_prospective_monitoring_support(
            package=workflow.package,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            decision=workflow.decision,
            prospective=prospective,
            supporting_evidence=support,
        )


def test_monitoring_observation_batches_bind_the_derived_metric_records(
    tmp_path: Path,
) -> None:
    workflow = _workflow(DerivativeProductKind.MULTI_LEG, tmp_path)
    monitoring_ref = monitoring_artifact_evidence_ref(workflow.monitoring.reference())
    support = {monitoring_ref: workflow.monitoring.as_dict()}
    support.update(
        {
            ref: deepcopy(workflow.supporting[ref])
            for ref in workflow.prospective.observation_refs
        }
    )
    first_ref = workflow.prospective.observation_refs[0]
    support[first_ref]["monitoring_observation_hashes"] = [_hash("substituted")]

    with pytest.raises(
        DerivativeEvidenceError,
        match="monitoring_observation_hashes_mismatch",
    ):
        _validate_prospective_monitoring_support(
            package=workflow.package,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            decision=workflow.decision,
            prospective=workflow.prospective,
            supporting_evidence=support,
        )


def test_chain_and_replay_reject_backdated_evidence(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.FUTURE, tmp_path)
    with pytest.raises(DerivativeEvidenceError, match="chain_time_order_mismatch"):
        _validate_chain(
            package=workflow.package,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=replace(workflow.decision, decided_at="2026-03-02T00:00:00+00:00"),
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
        )
    with pytest.raises(DerivativeEvidenceError, match="chain_time_order_mismatch"):
        _validate_chain(
            package=workflow.package,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=replace(
                workflow.conclusion,
                decided_at="2026-05-01T12:00:00+00:00",
            ),
        )

    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    with pytest.raises(DerivativeEvidenceError, match="before_package_creation"):
        registry.verify_replay(
            workflow.package.package_id,
            workflow.package.version,
            dataset=workflow.dataset,
            experiment_spec=workflow.experiment_spec,
            experiment_run=workflow.experiment_run,
            decision=workflow.decision,
            robustness=workflow.robustness,
            prospective=workflow.prospective,
            conclusion=workflow.conclusion,
            supporting_evidence=workflow.supporting,
            verified_at="2000-01-01T00:00:00+00:00",
        )


def test_registry_reader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    workflow = _workflow(DerivativeProductKind.OPTION, tmp_path)
    registry = DerivativeEvidenceRegistry(_manager(tmp_path))
    _register(registry, workflow)
    path = registry.evidence_path(workflow.package.ref())
    encoded = path.read_text(encoding="utf-8")
    marker = '"artifact_type":"derivative_research_package"'
    spaced_marker = '"artifact_type": "derivative_research_package"'
    if marker in encoded:
        encoded = encoded.replace(
            marker,
            '"artifact_type":"evil",' + marker,
            1,
        )
    else:
        assert spaced_marker in encoded
        encoded = encoded.replace(
            spaced_marker,
            '"artifact_type": "evil", ' + spaced_marker,
            1,
        )
    path.write_text(encoded, encoding="utf-8")

    with pytest.raises(DerivativeEvidenceError, match="json_duplicate_key"):
        registry.resolve_ref(workflow.package.ref())
