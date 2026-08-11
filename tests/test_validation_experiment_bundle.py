from __future__ import annotations

import copy
import json
from pathlib import Path
from statistics import fmean
from types import SimpleNamespace

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.hashing import sha256_prefixed
from market_research.research.report_writer import candidate_evidence_hash_inputs
from market_research.research.validation_experiment_bundle import (
    ValidationExperimentComponent,
    ValidationExperimentBundleError,
    ValidationExperimentCapability,
    ValidationExperimentCapabilityMode,
    ValidationExperimentOutputScope,
    ValidationExperimentOutputs,
    ValidationExperimentPolicy,
    build_validation_experiment_bundle,
    derive_validation_experiment_capability,
    parse_validation_experiment_capability,
    validate_validation_experiment_bundle,
)
from market_research.research.validation_experiments import (
    EvaluationStatus,
    FactorEstimate,
    FactorExposureResult,
    FalsificationKind,
    FalsificationPolicy,
    FalsificationResult,
    FalsificationSuiteResult,
    FoldEvaluation,
    FoldEvaluationPhase,
    MetricDirection,
    NestedCandidate,
    NestedSelectionPolicy,
    NestedSelectionResult,
    OuterFoldSelection,
    ProviderMetricTolerance,
    ProviderResearchResult,
    compare_provider_research_results,
)
from market_research.research.validation_pipeline import (
    ValidationRunError,
    _load_precomputed_validation_experiment_bundle,
    _validated_validation_experiment_reasons,
    aggregate_validation_gates,
    run_research_validation,
)
from market_research.settings import ResearchSettings


MANIFEST_HASH = "sha256:" + "a" * 64
DATASET_HASH = "sha256:" + "b" * 64
TEMPORAL_PLAN_HASH = "sha256:" + "c" * 64
OTHER_HASH = "sha256:" + "d" * 64


def _policy(
    *required: ValidationExperimentComponent,
) -> ValidationExperimentPolicy:
    return ValidationExperimentPolicy(required_components=tuple(required))


def _capability(
    *,
    manifest_hash: str = MANIFEST_HASH,
    research_classification: str = "validated_candidate",
) -> ValidationExperimentCapability:
    return derive_validation_experiment_capability(
        manifest_hash=manifest_hash,
        research_classification=research_classification,
    )


def _falsification(*, passed: bool, dataset_hash: str = DATASET_HASH):
    policy = FalsificationPolicy(
        policy_id="terminal-controls",
        version="1",
        seed=7,
        placebo_shift=1,
        minimum_sample_count=20,
        minimum_baseline_abs_effect=0.2,
        maximum_control_abs_effect=0.1,
        minimum_confounder_adjusted_retention=0.5,
    )
    kinds = (
        FalsificationKind.LABEL_SHUFFLE,
        FalsificationKind.NEGATIVE_CONTROL,
        FalsificationKind.PLACEBO_SHIFT,
        FalsificationKind.SIGNAL_SHUFFLE,
    )
    results = tuple(
        FalsificationResult(
            kind=kind,
            effect=0.01 if passed else 0.9,
            passed=passed,
            sample_count=(
                100 - policy.placebo_shift
                if kind is FalsificationKind.PLACEBO_SHIFT
                else 100
            ),
            transformation_hash=sha256_prefixed(
                kind.value, label="test_falsification_transform"
            ),
        )
        for kind in kinds
    )
    return FalsificationSuiteResult(
        dataset_snapshot_hash=dataset_hash,
        policy=policy,
        baseline_effect=0.7,
        results=results,
        passed=passed,
    )


def _candidate() -> dict[str, object]:
    return {
        "parameter_candidate_id": "candidate-a",
        "parameter_values": {"window": 10},
        "validation_metrics": {"return_pct": 1.0},
    }


def _candidate_hash(candidate: dict[str, object]) -> str:
    return sha256_prefixed(
        candidate_evidence_hash_inputs(candidate),
        label="candidate_evidence_hash",
    )


def _scope(
    *,
    manifest_hash: str = MANIFEST_HASH,
    dataset_snapshot_hash: str = DATASET_HASH,
    temporal_plan_hash: str = TEMPORAL_PLAN_HASH,
    selected_candidate_id: str = "candidate-a",
    selected_candidate_hash: str | None = None,
    research_classification: str = "validated_candidate",
) -> ValidationExperimentOutputScope:
    capability = _capability(
        manifest_hash=manifest_hash,
        research_classification=research_classification,
    )
    return ValidationExperimentOutputScope(
        manifest_hash=manifest_hash,
        capability_hash=capability.content_hash,
        dataset_snapshot_hash=dataset_snapshot_hash,
        temporal_plan_hash=temporal_plan_hash,
        selected_candidate_id=selected_candidate_id,
        selected_candidate_hash=(
            selected_candidate_hash or _candidate_hash(_candidate())
        ),
    )


def _bundle(*, passed: bool, manifest_hash: str = MANIFEST_HASH):
    candidate = _candidate()
    capability = _capability(manifest_hash=manifest_hash)
    return build_validation_experiment_bundle(
        manifest_hash=manifest_hash,
        dataset_snapshot_hash=DATASET_HASH,
        temporal_plan_hash=TEMPORAL_PLAN_HASH,
        selected_candidate_id="candidate-a",
        selected_candidate_hash=_candidate_hash(candidate),
        capability=capability,
        policy=capability.policy,
        outputs=_all_outputs(passed=passed, manifest_hash=manifest_hash),
    )


def _all_outputs(
    *, passed: bool = True, manifest_hash: str = MANIFEST_HASH
) -> ValidationExperimentOutputs:
    candidate = NestedCandidate(
        candidate_id="candidate-a",
        version="1",
        definition_hash=_candidate_hash(_candidate()),
    )
    candidate_b = NestedCandidate(
        candidate_id="candidate-b",
        version="1",
        definition_hash=OTHER_HASH,
    )
    inner_scores = (0.0121529503606, 0.0119886684624)
    inner = FoldEvaluation(
        candidate=candidate,
        split_id="inner-001",
        split_hash=sha256_prefixed("inner", label="test_split"),
        phase=FoldEvaluationPhase.INNER_VALIDATION,
        status=EvaluationStatus.PASS,
        score=inner_scores[0],
        sample_count=20,
        evidence_hash=sha256_prefixed("inner", label="test_evidence"),
        failure_code=None,
    )
    second_inner = FoldEvaluation(
        candidate=candidate,
        split_id="inner-002",
        split_hash=sha256_prefixed("inner-002", label="test_split"),
        phase=FoldEvaluationPhase.INNER_VALIDATION,
        status=EvaluationStatus.PASS,
        score=inner_scores[1],
        sample_count=20,
        evidence_hash=sha256_prefixed("inner-002", label="test_evidence"),
        failure_code=None,
    )
    candidate_b_inner = FoldEvaluation(
        candidate=candidate_b,
        split_id="inner-001",
        split_hash=sha256_prefixed("inner", label="test_split"),
        phase=FoldEvaluationPhase.INNER_VALIDATION,
        status=EvaluationStatus.PASS,
        score=inner_scores[0] + 1e-14,
        sample_count=20,
        evidence_hash=sha256_prefixed("candidate-b-inner", label="test_evidence"),
        failure_code=None,
    )
    candidate_b_second_inner = FoldEvaluation(
        candidate=candidate_b,
        split_id="inner-002",
        split_hash=sha256_prefixed("inner-002", label="test_split"),
        phase=FoldEvaluationPhase.INNER_VALIDATION,
        status=EvaluationStatus.PASS,
        score=inner_scores[1] + 1e-14,
        sample_count=20,
        evidence_hash=sha256_prefixed(
            "candidate-b-inner-002", label="test_evidence"
        ),
        failure_code=None,
    )
    outer = FoldEvaluation(
        candidate=candidate,
        split_id="outer-001",
        split_hash=sha256_prefixed("outer", label="test_split"),
        phase=FoldEvaluationPhase.OUTER_TEST,
        status=EvaluationStatus.PASS,
        score=0.4,
        sample_count=20,
        evidence_hash=sha256_prefixed("outer", label="test_evidence"),
        failure_code=None,
    )
    nested = NestedSelectionResult(
        plan_hash=TEMPORAL_PLAN_HASH,
        source_binding_hash=OTHER_HASH,
        policy=NestedSelectionPolicy(
            metric_id="information-ratio",
            direction=MetricDirection.MAXIMIZE,
            minimum_inner_sample_count=10,
            minimum_outer_sample_count=10,
        ),
        candidates=(candidate, candidate_b),
        folds=(
            OuterFoldSelection(
                outer_split_id="outer-001",
                selected_candidate=candidate,
                inner_evaluations=(
                    inner,
                    second_inner,
                    candidate_b_inner,
                    candidate_b_second_inner,
                ),
                inner_mean_score=fmean(inner_scores),
                outer_evaluation=outer,
            ),
        ),
        failed_evaluations=(),
    )
    estimate = FactorEstimate(
        factor_id="market",
        coefficient=0.2,
        hac_standard_error=0.01,
        confidence_low=0.18,
        confidence_high=0.22,
    )
    factor = FactorExposureResult(
        dataset_snapshot_hash=DATASET_HASH,
        model_id="market-model",
        model_version="1",
        hac_lags=1,
        sample_count=20,
        alpha=FactorEstimate(
            factor_id="ALPHA",
            coefficient=0.01,
            hac_standard_error=0.005,
            confidence_low=0.0,
            confidence_high=0.02,
        ),
        exposures=(estimate,),
        r_squared=0.5,
        residual_volatility=0.1,
        observation_hash=sha256_prefixed("factor-observations"),
    )
    provider = compare_provider_research_results(
        results=(
            ProviderResearchResult(
                provider_id="provider-a",
                dataset_snapshot_hash=DATASET_HASH,
                semantic_definition_hash=OTHER_HASH,
                report_hash=sha256_prefixed("provider-a"),
                metrics=(("annual-return", 0.0121212792836),),
            ),
            ProviderResearchResult(
                provider_id="provider-b",
                dataset_snapshot_hash=sha256_prefixed("provider-b-dataset"),
                semantic_definition_hash=OTHER_HASH,
                report_hash=sha256_prefixed("provider-b"),
                metrics=(("annual-return", 0.0121212639626),),
            ),
        ),
        selected_provider_id="provider-a",
        tolerances=(
            ProviderMetricTolerance(
                metric_id="annual-return",
                absolute_tolerance=0.01,
                relative_tolerance=0.1,
            ),
        ),
    )
    return ValidationExperimentOutputs(
        scope=_scope(manifest_hash=manifest_hash),
        nested_selection=nested,
        falsification=_falsification(passed=passed),
        factor_exposure=factor,
        provider_sensitivity=provider,
    )


def _manifest():
    return SimpleNamespace(
        manifest_hash=lambda: MANIFEST_HASH,
        research_classification="validated_candidate",
        acceptance_gate=SimpleNamespace(
            walk_forward_required=False,
            final_holdout_required_for_validation=False,
        ),
        stress_suite=SimpleNamespace(required_for_validation=True),
        statistical_validation=SimpleNamespace(required_for_validation=True),
        final_selection=SimpleNamespace(required_for_validation=True),
        dataset=SimpleNamespace(
            split=SimpleNamespace(
                train="train",
                validation="validation",
                final_holdout=None,
            )
        ),
    )


def _selection_report() -> dict[str, object]:
    return {
        "manifest_hash": MANIFEST_HASH,
        "dataset_content_hash": DATASET_HASH,
        "nested_temporal_validation": {"plan_hash": TEMPORAL_PLAN_HASH},
        "dataset_quality_gate_status": "PASS",
        "stress_suite_gate_result": "PASS",
        "statistical_gate_result": "PASS",
        "final_selection_gate_result": "PASS",
        "validation_eligibility_gate_result": "PASS",
        "validation_blocking_reasons": [],
    }


def _aggregate(monkeypatch, bundle):
    monkeypatch.setattr(
        "market_research.research.validation_pipeline.validate_selection_artifact_binding",
        lambda **_kwargs: [],
    )
    return aggregate_validation_gates(
        manifest=_manifest(),
        selection_report=_selection_report(),
        selection_artifact={},
        selected_candidate=_candidate(),
        final_holdout_confirmation=None,
        validation_experiment_policy=_policy(
            *tuple(ValidationExperimentComponent)
        ),
        validation_experiment_bundle=bundle,
    )


def _component(payload: dict[str, object], name: str) -> dict[str, object]:
    return next(
        item
        for item in payload["components"]  # type: ignore[union-attr]
        if item["component"] == name
    )


def _rehash_component_and_bundle(
    payload: dict[str, object], component: dict[str, object]
) -> None:
    evidence = component["evidence"]
    assert isinstance(evidence, dict)
    output_scope = evidence["output_scope"]
    result = evidence["result"]
    native_source_bindings = evidence["native_source_bindings"]
    assert isinstance(output_scope, dict)
    assert isinstance(result, dict)
    assert isinstance(native_source_bindings, dict)
    evidence["content_hash"] = sha256_prefixed(
        {
            "schema_version": evidence["schema_version"],
            "component": component["component"],
            "output_scope_hash": output_scope["content_hash"],
            "result_hash": result["content_hash"],
            "native_source_bindings_hash": sha256_prefixed(
                native_source_bindings,
                label="validation_experiment_native_source_bindings",
            ),
        },
        label="validation_experiment_component_evidence",
    )
    component["evidence_hash"] = evidence["content_hash"]
    component["evidence_payload_hash"] = sha256_prefixed(
        evidence, label="validation_experiment_component_payload"
    )
    payload["content_hash"] = sha256_prefixed(
        {key: value for key, value in payload.items() if key != "content_hash"},
        label="validation_experiment_bundle",
    )


def _result_evidence(component: dict[str, object]) -> dict[str, object]:
    evidence = component["evidence"]
    assert isinstance(evidence, dict)
    result = evidence["result"]
    assert isinstance(result, dict)
    return result


def _rehash_falsification_evidence(evidence: dict[str, object]) -> None:
    results = evidence["results"]
    assert isinstance(results, list)
    evidence["content_hash"] = sha256_prefixed(
        {
            "schema_version": evidence["schema_version"],
            "dataset_snapshot_hash": evidence["dataset_snapshot_hash"],
            "policy": evidence["policy"],
            "policy_hash": evidence["policy_hash"],
            "baseline_effect": evidence["baseline_effect"],
            "result_hashes": [item["content_hash"] for item in results],
            "passed": evidence["passed"],
        },
        label="falsification_suite_result",
    )


def _rehash_nested_evidence(evidence: dict[str, object]) -> None:
    folds = evidence["folds"]
    failed = evidence["failed_evaluations"]
    assert isinstance(folds, list)
    assert isinstance(failed, list)
    evidence["content_hash"] = sha256_prefixed(
        {
            "schema_version": evidence["schema_version"],
            "selection_is_fully_nested": evidence["selection_is_fully_nested"],
            "plan_hash": evidence["plan_hash"],
            "source_binding_hash": evidence["source_binding_hash"],
            "policy": evidence["policy"],
            "policy_hash": evidence["policy_hash"],
            "candidates": evidence["candidates"],
            "fold_hashes": [item["content_hash"] for item in folds],
            "failed_evaluation_hashes": [
                item["content_hash"] for item in failed
            ],
        },
        label="nested_selection_result",
    )


def _rehash_provider_evidence(evidence: dict[str, object]) -> None:
    provider_results = evidence["provider_results"]
    assert isinstance(provider_results, list)
    evidence["content_hash"] = sha256_prefixed(
        {
            "schema_version": evidence["schema_version"],
            "selected_provider_id": evidence["selected_provider_id"],
            "semantic_definition_hash": evidence["semantic_definition_hash"],
            "provider_result_hashes": [
                item["content_hash"] for item in provider_results
            ],
            "tolerances": evidence["tolerances"],
            "differences": evidence["differences"],
            "passed": evidence["passed"],
        },
        label="provider_sensitivity_result",
    )


def _rehash_fold_evaluation(evaluation: dict[str, object]) -> None:
    evaluation["content_hash"] = sha256_prefixed(
        {
            "candidate": evaluation["candidate"],
            "split_id": evaluation["split_id"],
            "split_hash": evaluation["split_hash"],
            "phase": evaluation["phase"],
            "observation": {
                "status": evaluation["status"],
                "score": evaluation["score"],
                "sample_count": evaluation["sample_count"],
                "evidence_hash": evaluation["evidence_hash"],
                "failure_code": evaluation["failure_code"],
            },
        },
        label="nested_fold_evaluation",
    )


def _rehash_outer_fold(fold: dict[str, object]) -> None:
    inner = fold["inner_evaluations"]
    outer = fold["outer_evaluation"]
    assert isinstance(inner, list)
    assert isinstance(outer, dict)
    fold["content_hash"] = sha256_prefixed(
        {
            "outer_split_id": fold["outer_split_id"],
            "selected_candidate": fold["selected_candidate"],
            "inner_evaluation_hashes": [item["content_hash"] for item in inner],
            "inner_mean_score": fold["inner_mean_score"],
            "outer_evaluation_hash": outer["content_hash"],
        },
        label="outer_fold_selection",
    )


def test_bundle_binds_complete_output_and_detects_nested_payload_tampering() -> None:
    bundle = _bundle(passed=True)
    payload = bundle.as_dict()
    assert validate_validation_experiment_bundle(
        payload,
        expected_policy=_capability().policy,
        expected_manifest_hash=MANIFEST_HASH,
        expected_capability_hash=_capability().content_hash,
        expected_dataset_snapshot_hash=DATASET_HASH,
        expected_temporal_plan_hash=TEMPORAL_PLAN_HASH,
        expected_selected_candidate_id="candidate-a",
        expected_selected_candidate_hash=_candidate_hash(_candidate()),
    ) == []

    tampered = copy.deepcopy(payload)
    _result_evidence(_component(tampered, "falsification"))[
        "baseline_effect"
    ] = "999"
    reasons = validate_validation_experiment_bundle(tampered)
    assert (
        "validation_experiment_component_payload_hash_mismatch:falsification"
        in reasons
    )
    assert "validation_experiment_component_evidence_invalid:falsification" in reasons


def test_policy_preserves_and_reports_every_missing_required_component() -> None:
    policy = _policy(*tuple(ValidationExperimentComponent))
    bundle = build_validation_experiment_bundle(
        manifest_hash=MANIFEST_HASH,
        dataset_snapshot_hash=DATASET_HASH,
        temporal_plan_hash=TEMPORAL_PLAN_HASH,
        selected_candidate_id="candidate-a",
        selected_candidate_hash=_candidate_hash(_candidate()),
        capability=_capability(),
        policy=policy,
        outputs=ValidationExperimentOutputs(),
    )

    assert bundle.gate_result.value == "FAIL"
    assert bundle.gate_reasons == tuple(
        sorted(
            "validation_experiment_required_component_missing:" + item.value
            for item in ValidationExperimentComponent
        )
    )


def test_bundle_validates_all_four_supported_component_outputs() -> None:
    policy = _policy(*tuple(ValidationExperimentComponent))
    bundle = build_validation_experiment_bundle(
        manifest_hash=MANIFEST_HASH,
        dataset_snapshot_hash=DATASET_HASH,
        temporal_plan_hash=TEMPORAL_PLAN_HASH,
        selected_candidate_id="candidate-a",
        selected_candidate_hash=_candidate_hash(_candidate()),
        capability=_capability(),
        policy=policy,
        outputs=_all_outputs(),
    )

    assert bundle.gate_result.value == "PASS"
    assert validate_validation_experiment_bundle(
        bundle.as_dict(),
        expected_policy=policy,
        expected_manifest_hash=MANIFEST_HASH,
        expected_capability_hash=_capability().content_hash,
        expected_dataset_snapshot_hash=DATASET_HASH,
        expected_temporal_plan_hash=TEMPORAL_PLAN_HASH,
        expected_selected_candidate_id="candidate-a",
        expected_selected_candidate_hash=_candidate_hash(_candidate()),
    ) == []


def test_nonempty_outputs_require_original_immutable_scope() -> None:
    with pytest.raises(
        ValidationExperimentBundleError,
        match="validation_experiment_output_scope_required",
    ):
        ValidationExperimentOutputs(
            falsification=_falsification(passed=True)
        )


def test_builder_rejects_rebinding_outputs_from_scope_a_to_other_candidate_b() -> None:
    outputs_from_a = _all_outputs()

    with pytest.raises(
        ValidationExperimentBundleError,
        match="validation_experiment_output_scope_manifest_hash_mismatch",
    ):
        build_validation_experiment_bundle(
            manifest_hash=OTHER_HASH,
            dataset_snapshot_hash=DATASET_HASH,
            temporal_plan_hash=TEMPORAL_PLAN_HASH,
            selected_candidate_id="candidate-b",
            selected_candidate_hash=OTHER_HASH,
            capability=_capability(manifest_hash=OTHER_HASH),
            policy=_policy(*tuple(ValidationExperimentComponent)),
            outputs=outputs_from_a,
        )

    with pytest.raises(
        ValidationExperimentBundleError,
        match="validation_experiment_output_scope_selected_candidate_id_mismatch",
    ):
        build_validation_experiment_bundle(
            manifest_hash=MANIFEST_HASH,
            dataset_snapshot_hash=DATASET_HASH,
            temporal_plan_hash=TEMPORAL_PLAN_HASH,
            selected_candidate_id="candidate-b",
            selected_candidate_hash=OTHER_HASH,
            capability=_capability(),
            policy=_policy(*tuple(ValidationExperimentComponent)),
            outputs=outputs_from_a,
        )


def test_serialized_scope_a_component_cannot_be_rewrapped_under_top_b() -> None:
    payload = _bundle(passed=True).as_dict()
    bindings = payload["bindings"]
    assert isinstance(bindings, dict)
    bindings["manifest_hash"] = OTHER_HASH
    bindings["selected_candidate_id"] = "candidate-b"
    bindings["selected_candidate_hash"] = OTHER_HASH
    payload["content_hash"] = sha256_prefixed(
        {key: value for key, value in payload.items() if key != "content_hash"},
        label="validation_experiment_bundle",
    )

    reasons = validate_validation_experiment_bundle(payload)

    assert (
        "validation_experiment_component_output_scope_manifest_hash_mismatch"
        in reasons
    )
    assert (
        "validation_experiment_component_output_scope_selected_candidate_id_mismatch"
        in reasons
    )
    assert "validation_experiment_bundle_gate_result_mismatch" in reasons


def test_component_evidence_hash_is_bound_to_original_output_scope() -> None:
    component_a = _component(_bundle(passed=True).as_dict(), "falsification")
    component_other = _component(
        _bundle(passed=True, manifest_hash=OTHER_HASH).as_dict(),
        "falsification",
    )

    assert (
        _result_evidence(component_a)["content_hash"]
        == _result_evidence(component_other)["content_hash"]
    )
    assert component_a["evidence_hash"] != component_other["evidence_hash"]


def test_terminal_gate_rejects_wrong_authority_binding(monkeypatch) -> None:
    result, _stages, reasons = _aggregate(
        monkeypatch,
        _bundle(passed=True, manifest_hash=OTHER_HASH),
    )

    assert result == "FAIL"
    assert "validation_experiment_bundle_manifest_hash_mismatch" in reasons
    assert "validation_experiment_gate_not_passed" in reasons


def test_terminal_gate_preserves_and_blocks_failed_falsification(monkeypatch) -> None:
    bundle = _bundle(passed=False)
    payload = bundle.as_dict()
    result, _stages, reasons = _aggregate(monkeypatch, bundle)

    assert _result_evidence(_component(payload, "falsification"))["passed"] is False
    assert payload["gate_result"] == "FAIL"
    assert result == "FAIL"
    assert (
        "validation_experiment_required_component_failed:falsification" in reasons
    )
    assert "validation_experiment_gate_not_passed" in reasons


def test_terminal_gate_rejects_tampered_component_even_if_required(monkeypatch) -> None:
    tampered = _bundle(passed=True).as_dict()
    _result_evidence(_component(tampered, "falsification"))[
        "baseline_effect"
    ] = "999"

    result, _stages, reasons = _aggregate(monkeypatch, tampered)

    assert result == "FAIL"
    assert "validation_experiment_component_evidence_invalid:falsification" in reasons
    assert "validation_experiment_gate_not_passed" in reasons


def test_terminal_result_validator_rechecks_embedded_bundle_integrity() -> None:
    capability = _capability()
    policy = capability.policy
    bundle = _bundle(passed=True).as_dict()
    report = {
        "manifest_hash": MANIFEST_HASH,
        "research_classification": "validated_candidate",
        "dataset_content_hash": DATASET_HASH,
        "selected_candidate_id": "candidate-a",
        "selected_candidate": {
            "candidate_payload_hash": _candidate_hash(_candidate())
        },
        "validation_experiment_capability": capability.as_dict(),
        "validation_experiment_capability_hash": capability.content_hash,
        "validation_experiment_policy": policy.as_dict(),
        "validation_experiment_policy_hash": policy.contract_hash(),
        "validation_experiment_temporal_plan_hash": TEMPORAL_PLAN_HASH,
        "validation_experiment_bundle": bundle,
        "validation_experiment_bundle_hash": bundle["content_hash"],
        "validation_experiment_gate_result": bundle["gate_result"],
        "validation_experiment_gate_reasons": bundle["gate_reasons"],
    }
    assert _validated_validation_experiment_reasons(report) == []

    report["validation_experiment_bundle"] = copy.deepcopy(bundle)
    _result_evidence(
        _component(report["validation_experiment_bundle"], "falsification")
    )["baseline_effect"] = "999"
    reasons = _validated_validation_experiment_reasons(report)
    assert any("evidence_invalid:falsification" in item for item in reasons)


def test_self_consistent_rehash_cannot_flip_failed_falsification_suite() -> None:
    payload = _bundle(passed=False).as_dict()
    component = _component(payload, "falsification")
    evidence = _result_evidence(component)
    evidence["passed"] = True
    _rehash_falsification_evidence(evidence)
    component["status"] = "PASS"
    payload["gate_result"] = "PASS"
    payload["gate_reasons"] = []
    _rehash_component_and_bundle(payload, component)

    reasons = validate_validation_experiment_bundle(payload)

    assert "validation_experiment_component_evidence_invalid:falsification" in reasons
    assert "validation_experiment_bundle_gate_result_mismatch" in reasons


def test_self_consistent_rehash_cannot_forge_falsification_children() -> None:
    payload = _bundle(passed=False).as_dict()
    component = _component(payload, "falsification")
    evidence = _result_evidence(component)
    results = evidence["results"]
    assert isinstance(results, list)
    for result in results:
        result["passed"] = True
        result["content_hash"] = sha256_prefixed(
            {
                key: value
                for key, value in result.items()
                if key != "content_hash"
            },
            label="falsification_result",
        )
    evidence["passed"] = True
    _rehash_falsification_evidence(evidence)
    component["status"] = "PASS"
    payload["gate_result"] = "PASS"
    payload["gate_reasons"] = []
    _rehash_component_and_bundle(payload, component)

    reasons = validate_validation_experiment_bundle(payload)

    assert "validation_experiment_component_evidence_invalid:falsification" in reasons


def test_self_consistent_rehash_cannot_forge_nested_selection_score() -> None:
    policy = _policy(*tuple(ValidationExperimentComponent))
    payload = build_validation_experiment_bundle(
        manifest_hash=MANIFEST_HASH,
        dataset_snapshot_hash=DATASET_HASH,
        temporal_plan_hash=TEMPORAL_PLAN_HASH,
        selected_candidate_id="candidate-a",
        selected_candidate_hash=_candidate_hash(_candidate()),
        capability=_capability(),
        policy=policy,
        outputs=_all_outputs(),
    ).as_dict()
    component = _component(payload, "nested_selection")
    evidence = _result_evidence(component)
    folds = evidence["folds"]
    assert isinstance(folds, list)
    fold = folds[0]
    fold["inner_mean_score"] = "999"
    inner = fold["inner_evaluations"]
    assert isinstance(inner, list)
    fold["content_hash"] = sha256_prefixed(
        {
            "outer_split_id": fold["outer_split_id"],
            "selected_candidate": fold["selected_candidate"],
            "inner_evaluation_hashes": [item["content_hash"] for item in inner],
            "inner_mean_score": fold["inner_mean_score"],
            "outer_evaluation_hash": fold["outer_evaluation"]["content_hash"],
        },
        label="outer_fold_selection",
    )
    _rehash_nested_evidence(evidence)
    _rehash_component_and_bundle(payload, component)

    reasons = validate_validation_experiment_bundle(payload)

    assert "validation_experiment_component_evidence_invalid:nested_selection" in reasons


def test_self_consistent_rehash_rejects_one_wire_digit_nested_mean_tamper() -> None:
    payload = _bundle(passed=True).as_dict()
    component = _component(payload, "nested_selection")
    evidence = _result_evidence(component)
    folds = evidence["folds"]
    assert isinstance(folds, list)
    fold = folds[0]
    assert isinstance(fold, dict)
    assert fold["inner_mean_score"] == "0.012070809411"
    fold["inner_mean_score"] = "0.012070809412"
    _rehash_outer_fold(fold)
    _rehash_nested_evidence(evidence)
    _rehash_component_and_bundle(payload, component)

    reasons = validate_validation_experiment_bundle(payload)

    assert "validation_experiment_component_evidence_invalid:nested_selection" in reasons


def test_self_consistent_rehash_rejects_noncanonical_numeric_spelling() -> None:
    payload = _bundle(passed=True).as_dict()
    component = _component(payload, "nested_selection")
    evidence = _result_evidence(component)
    folds = evidence["folds"]
    assert isinstance(folds, list)
    fold = folds[0]
    inner = fold["inner_evaluations"]
    assert isinstance(inner, list)
    evaluation = inner[0]
    assert isinstance(evaluation, dict)
    evaluation["score"] = str(evaluation["score"]) + "0"
    _rehash_fold_evaluation(evaluation)
    _rehash_outer_fold(fold)
    _rehash_nested_evidence(evidence)
    _rehash_component_and_bundle(payload, component)

    reasons = validate_validation_experiment_bundle(payload)

    assert "validation_experiment_component_evidence_invalid:nested_selection" in reasons


def test_self_consistent_rehash_cannot_forge_factor_alpha_identity() -> None:
    policy = _policy(*tuple(ValidationExperimentComponent))
    payload = build_validation_experiment_bundle(
        manifest_hash=MANIFEST_HASH,
        dataset_snapshot_hash=DATASET_HASH,
        temporal_plan_hash=TEMPORAL_PLAN_HASH,
        selected_candidate_id="candidate-a",
        selected_candidate_hash=_candidate_hash(_candidate()),
        capability=_capability(),
        policy=policy,
        outputs=_all_outputs(),
    ).as_dict()
    component = _component(payload, "factor_exposure")
    evidence = _result_evidence(component)
    evidence["alpha"]["factor_id"] = "market-alpha"
    evidence["content_hash"] = sha256_prefixed(
        {key: value for key, value in evidence.items() if key != "content_hash"},
        label="factor_exposure_result",
    )
    _rehash_component_and_bundle(payload, component)

    reasons = validate_validation_experiment_bundle(payload)

    assert "validation_experiment_component_evidence_invalid:factor_exposure" in reasons


def test_self_consistent_rehash_cannot_omit_provider_differences() -> None:
    policy = _policy(*tuple(ValidationExperimentComponent))
    payload = build_validation_experiment_bundle(
        manifest_hash=MANIFEST_HASH,
        dataset_snapshot_hash=DATASET_HASH,
        temporal_plan_hash=TEMPORAL_PLAN_HASH,
        selected_candidate_id="candidate-a",
        selected_candidate_hash=_candidate_hash(_candidate()),
        capability=_capability(),
        policy=policy,
        outputs=_all_outputs(),
    ).as_dict()
    component = _component(payload, "provider_sensitivity")
    evidence = _result_evidence(component)
    evidence["differences"] = []
    evidence["passed"] = True
    _rehash_provider_evidence(evidence)
    _rehash_component_and_bundle(payload, component)

    reasons = validate_validation_experiment_bundle(payload)

    assert (
        "validation_experiment_component_evidence_invalid:provider_sensitivity"
        in reasons
    )


def test_self_consistent_rehash_rejects_last_wire_digit_provider_difference() -> None:
    payload = _bundle(passed=True).as_dict()
    component = _component(payload, "provider_sensitivity")
    evidence = _result_evidence(component)
    differences = evidence["differences"]
    assert isinstance(differences, list)
    difference = differences[0]
    assert isinstance(difference, dict)
    assert difference["absolute_difference"] == "1.5321e-08"
    assert difference["relative_difference"] == "1.263975e-06"
    difference["absolute_difference"] = "1.5322e-08"
    difference["relative_difference"] = "1.263976e-06"
    _rehash_provider_evidence(evidence)
    _rehash_component_and_bundle(payload, component)

    reasons = validate_validation_experiment_bundle(payload)

    assert (
        "validation_experiment_component_evidence_invalid:provider_sensitivity"
        in reasons
    )


def test_boolean_schema_versions_are_rejected_even_after_rehash() -> None:
    payload = _bundle(passed=True).as_dict()
    payload["schema_version"] = True
    payload["content_hash"] = sha256_prefixed(
        {key: value for key, value in payload.items() if key != "content_hash"},
        label="validation_experiment_bundle",
    )
    assert "validation_experiment_bundle_contract_invalid" in (
        validate_validation_experiment_bundle(payload)
    )

    component = _component(payload, "falsification")
    evidence = component["evidence"]
    assert isinstance(evidence, dict)
    evidence["schema_version"] = True
    _rehash_component_and_bundle(payload, component)
    assert "validation_experiment_component_evidence_invalid:falsification" in (
        validate_validation_experiment_bundle(payload)
    )

    policy_payload = _bundle(passed=True).as_dict()
    policy = policy_payload["policy"]
    assert isinstance(policy, dict)
    policy["schema_version"] = True
    policy_payload["policy_hash"] = sha256_prefixed(
        policy, label="validation_experiment_policy"
    )
    policy_payload["content_hash"] = sha256_prefixed(
        {
            key: value
            for key, value in policy_payload.items()
            if key != "content_hash"
        },
        label="validation_experiment_bundle",
    )
    assert "validation_experiment_bundle_policy_invalid" in (
        validate_validation_experiment_bundle(policy_payload)
    )

    with pytest.raises(ValueError):
        ValidationExperimentPolicy(schema_version=True)


def test_terminal_validator_reports_missing_bundle_and_contradictory_gate() -> None:
    capability = _capability()
    policy = capability.policy
    report = {
        "manifest_hash": MANIFEST_HASH,
        "research_classification": "validated_candidate",
        "validation_experiment_capability": capability.as_dict(),
        "validation_experiment_capability_hash": capability.content_hash,
        "validation_experiment_policy": policy.as_dict(),
        "validation_experiment_policy_hash": policy.contract_hash(),
        "validation_experiment_temporal_plan_hash": TEMPORAL_PLAN_HASH,
        "validation_experiment_bundle": None,
        "validation_experiment_bundle_hash": OTHER_HASH,
        "validation_experiment_gate_result": "PASS",
        "validation_experiment_gate_reasons": [],
    }

    reasons = _validated_validation_experiment_reasons(report)

    assert (
        "validated_research_result_validation_experiment_bundle_missing" in reasons
    )
    assert (
        "validated_research_result_validation_experiment_bundle_hash_mismatch"
        in reasons
    )
    assert (
        "validated_research_result_validation_experiment_gate_result_mismatch"
        in reasons
    )
    assert (
        "validated_research_result_validation_experiment_gate_reasons_mismatch"
        in reasons
    )


def test_complete_experiment_field_stripping_fails_closed() -> None:
    assert _validated_validation_experiment_reasons({"schema_version": 3}) == [
        "validated_research_result_validation_experiment_capability_missing"
    ]


def test_explicit_legacy_schema_capability_is_readable_but_not_promotable() -> None:
    legacy = ValidationExperimentCapability(
        mode=ValidationExperimentCapabilityMode.LEGACY_SCHEMA_3_READ_ONLY,
        manifest_hash=MANIFEST_HASH,
        research_classification="validated_candidate",
        required_components=(),
    )
    assert parse_validation_experiment_capability(legacy.as_dict()) == legacy
    policy = legacy.policy
    report = {
        "manifest_hash": MANIFEST_HASH,
        "research_classification": "validated_candidate",
        "validation_experiment_capability": legacy.as_dict(),
        "validation_experiment_capability_hash": legacy.content_hash,
        "validation_experiment_policy": policy.as_dict(),
        "validation_experiment_policy_hash": policy.contract_hash(),
        "validation_experiment_temporal_plan_hash": TEMPORAL_PLAN_HASH,
        "validation_experiment_bundle": None,
        "validation_experiment_bundle_hash": None,
        "validation_experiment_gate_result": "PASS",
        "validation_experiment_gate_reasons": [],
    }

    reasons = _validated_validation_experiment_reasons(report)

    assert (
        "validated_research_result_validation_experiment_legacy_capability_not_promotable"
        in reasons
    )


def test_self_consistent_capability_downgrade_cannot_promote_validated_result() -> None:
    downgraded = _capability().as_dict()
    downgraded["research_classification"] = "research_only"
    downgraded["required_components"] = []
    downgraded["policy_hash"] = _policy().contract_hash()
    downgraded["content_hash"] = sha256_prefixed(
        {key: value for key, value in downgraded.items() if key != "content_hash"},
        label="validation_experiment_capability",
    )
    parsed = parse_validation_experiment_capability(downgraded)
    report = {
        "manifest_hash": MANIFEST_HASH,
        "research_classification": "validated_candidate",
        "validation_experiment_capability": downgraded,
        "validation_experiment_capability_hash": parsed.content_hash,
        "validation_experiment_policy": parsed.policy.as_dict(),
        "validation_experiment_policy_hash": parsed.policy.contract_hash(),
        "validation_experiment_temporal_plan_hash": TEMPORAL_PLAN_HASH,
        "validation_experiment_bundle": None,
        "validation_experiment_bundle_hash": None,
        "validation_experiment_gate_result": "PASS",
        "validation_experiment_gate_reasons": [],
    }

    assert (
        "validated_research_result_validation_experiment_capability_mismatch"
        in _validated_validation_experiment_reasons(report)
    )


def test_builder_rejects_caller_policy_downgrade() -> None:
    with pytest.raises(
        ValidationExperimentBundleError,
        match="validation_experiment_policy_not_authoritative",
    ):
        build_validation_experiment_bundle(
            manifest_hash=MANIFEST_HASH,
            dataset_snapshot_hash=DATASET_HASH,
            temporal_plan_hash=TEMPORAL_PLAN_HASH,
            selected_candidate_id="candidate-a",
            selected_candidate_hash=_candidate_hash(_candidate()),
            capability=_capability(),
            policy=_policy(ValidationExperimentComponent.FALSIFICATION),
            outputs=_all_outputs(),
        )


def test_terminal_gate_rejects_caller_policy_downgrade(monkeypatch) -> None:
    monkeypatch.setattr(
        "market_research.research.validation_pipeline.validate_selection_artifact_binding",
        lambda **_kwargs: [],
    )
    result, _stages, reasons = aggregate_validation_gates(
        manifest=_manifest(),
        selection_report=_selection_report(),
        selection_artifact={},
        selected_candidate=_candidate(),
        final_holdout_confirmation=None,
        validation_experiment_policy=_policy(
            ValidationExperimentComponent.FALSIFICATION
        ),
        validation_experiment_bundle=_bundle(passed=True),
    )

    assert result == "FAIL"
    assert "validation_experiment_policy_not_authoritative" in reasons


def test_validated_candidate_automatically_requires_all_experiments(monkeypatch) -> None:
    monkeypatch.setattr(
        "market_research.research.validation_pipeline.validate_selection_artifact_binding",
        lambda **_kwargs: [],
    )
    result, _stages, reasons = aggregate_validation_gates(
        manifest=_manifest(),
        selection_report=_selection_report(),
        selection_artifact={},
        selected_candidate=_candidate(),
        final_holdout_confirmation=None,
    )

    assert result == "FAIL"
    assert {
        "validation_experiment_required_component_missing:" + item.value
        for item in ValidationExperimentComponent
    }.issubset(reasons)
    assert "validation_experiment_gate_not_passed" in reasons


def test_official_validation_entrypoint_rejects_policy_downgrade_before_io() -> None:
    manifest = SimpleNamespace(
        manifest_hash=lambda: MANIFEST_HASH,
        research_classification="validated_candidate",
    )
    with pytest.raises(
        ValidationRunError,
        match="validation_experiment_policy_not_authoritative",
    ):
        run_research_validation(
            manifest=manifest,
            db_path=None,
            manager=object(),
            manifest_path="/external/manifest.json",
            strategy_registry=object(),  # type: ignore[arg-type]
            validation_experiment_policy=_policy(
                ValidationExperimentComponent.FALSIFICATION
            ),
        )


def test_official_external_bundle_cannot_supply_its_own_downgraded_policy(
    tmp_path: Path,
) -> None:
    manager = ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=tmp_path / "input.sqlite",
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )
    downgraded_capability = _capability(
        research_classification="research_only"
    )
    downgraded = build_validation_experiment_bundle(
        manifest_hash=MANIFEST_HASH,
        dataset_snapshot_hash=DATASET_HASH,
        temporal_plan_hash=TEMPORAL_PLAN_HASH,
        selected_candidate_id="candidate-a",
        selected_candidate_hash=_candidate_hash(_candidate()),
        capability=downgraded_capability,
        policy=downgraded_capability.policy,
        outputs=ValidationExperimentOutputs(),
    )
    bundle_path = tmp_path / "downgraded-validation-bundle.json"
    bundle_path.write_text(json.dumps(downgraded.as_dict()), encoding="utf-8")
    manifest = SimpleNamespace(
        manifest_hash=lambda: MANIFEST_HASH,
        research_classification="validated_candidate",
    )

    with pytest.raises(
        ValidationRunError,
        match="validation_experiment_bundle_policy_not_authoritative",
    ):
        run_research_validation(
            manifest=manifest,
            db_path=None,
            manager=manager,
            manifest_path="/external/manifest.json",
            strategy_registry=object(),  # type: ignore[arg-type]
            validation_experiment_bundle_path=bundle_path,
        )


def test_native_source_binding_tamper_fails_after_self_consistent_rehash() -> None:
    payload = _bundle(passed=True).as_dict()
    component = _component(payload, "falsification")
    evidence = component["evidence"]
    assert isinstance(evidence, dict)
    native = evidence["native_source_bindings"]
    assert isinstance(native, dict)
    native["dataset_snapshot_hash"] = OTHER_HASH
    _rehash_component_and_bundle(payload, component)

    reasons = validate_validation_experiment_bundle(payload)

    assert "validation_experiment_component_evidence_invalid:falsification" in reasons


def test_capability_schema_bool_and_scope_rewrap_are_rejected() -> None:
    capability = _capability().as_dict()
    capability["schema_version"] = True
    capability["content_hash"] = sha256_prefixed(
        {key: value for key, value in capability.items() if key != "content_hash"},
        label="validation_experiment_capability",
    )
    with pytest.raises(ValidationExperimentBundleError):
        parse_validation_experiment_capability(capability)

    payload = _bundle(passed=True).as_dict()
    bindings = payload["bindings"]
    assert isinstance(bindings, dict)
    bindings["capability_hash"] = OTHER_HASH
    payload["content_hash"] = sha256_prefixed(
        {key: value for key, value in payload.items() if key != "content_hash"},
        label="validation_experiment_bundle",
    )
    reasons = validate_validation_experiment_bundle(payload)
    assert (
        "validation_experiment_component_output_scope_capability_hash_mismatch"
        in reasons
    )


def test_external_precomputed_bundle_loads_and_remains_bound_to_terminal_gate(
    tmp_path: Path,
) -> None:
    manager = ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=tmp_path / "input.sqlite",
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )
    bundle_path = tmp_path / "validation-bundle.json"
    payload = _bundle(passed=True, manifest_hash=OTHER_HASH).as_dict()
    bundle_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded, policy = _load_precomputed_validation_experiment_bundle(
        manager=manager,
        path=bundle_path,
    )
    result, _stages, reasons = aggregate_validation_gates(
        manifest=_manifest(),
        selection_report=_selection_report(),
        selection_artifact={},
        selected_candidate=_candidate(),
        final_holdout_confirmation=None,
        manager=manager,
        validation_experiment_policy=policy,
        validation_experiment_bundle=loaded,
    )

    assert result == "FAIL"
    assert "validation_experiment_bundle_manifest_hash_mismatch" in reasons


@pytest.mark.parametrize(
    "invalid_json",
    (
        '{"policy": {}, "policy": {}}',
        '{"policy": NaN}',
    ),
)
def test_external_bundle_loader_rejects_ambiguous_json(
    tmp_path: Path,
    invalid_json: str,
) -> None:
    manager = ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=tmp_path / "input.sqlite",
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )
    bundle_path = tmp_path / "invalid-bundle.json"
    bundle_path.write_text(invalid_json, encoding="utf-8")

    with pytest.raises(
        ValidationRunError,
        match="validation_experiment_bundle_input_json_invalid",
    ):
        _load_precomputed_validation_experiment_bundle(
            manager=manager,
            path=bundle_path,
        )
