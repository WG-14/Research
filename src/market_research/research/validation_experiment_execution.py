"""Manifest-authoritative execution of confirmatory validation experiments.

Only immutable candle artifacts and closed, versioned observation builders are
accepted.  This module computes every result through the production strategy
engine; it never promotes a caller-supplied metric or result bundle.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import math
from pathlib import Path
import re
from typing import Any, Mapping

from market_research.paths import ResearchPathManager
from market_research.storage_io import write_json_atomic_create_or_verify

from .datasets.contracts import DatasetArtifactRef
from .experiment_manifest import (
    DateRange,
    ExperimentManifest,
    ValidationExperimentExecutionContract,
)
from .hashing import sha256_prefixed
from .parameter_space import candidate_id as manifest_candidate_id
from .parameter_space import iter_parameter_candidates
from .strategy_registry import StrategyRegistry
from .temporal_validation import build_manifest_nested_temporal_validation_plan
from .validation_experiment_bundle import (
    ValidationExperimentBundle,
    ValidationExperimentCapability,
    ValidationExperimentOutputScope,
    ValidationExperimentOutputs,
    build_validation_experiment_bundle,
)
from .validation_experiments import (
    EvaluationStatus,
    FactorObservation,
    FalsificationObservation,
    FalsificationPolicy,
    FoldEvaluationPhase,
    MetricDirection,
    MetricObservation,
    NestedCandidate,
    NestedSelectionPolicy,
    NestedSelectionResult,
    ProviderMetricTolerance,
    ProviderResearchResult,
    _canonical_mean,
    compare_provider_research_results,
    estimate_factor_exposures,
    execute_nested_temporal_selection,
    run_falsification_suite,
)
from .validation_protocol import (
    ManifestCandidateRangeEvaluation,
    ResearchValidationError,
    run_manifest_candidate_range,
)


NATIVE_VALIDATION_EXECUTION_SCHEMA_VERSION = 2
NATIVE_VALIDATION_EXECUTION_ARTIFACT_TYPE = (
    "native_validation_experiment_computation_receipt"
)
NATIVE_VALIDATION_BUILDER_REGISTRY = {
    "schema_version": 1,
    "nested_metric_builders": ["return_pct"],
    "falsification_observation_builders": [
        "lagged_strategy_exposure_next_bar_return_v1"
    ],
    "factor_observation_builders": [
        "strategy_period_return_market_factor_v1"
    ],
    "provider_semantic_definitions": [
        "selected_candidate_required_metrics_v1"
    ],
}
NATIVE_VALIDATION_BUILDER_REGISTRY_HASH = sha256_prefixed(
    NATIVE_VALIDATION_BUILDER_REGISTRY,
    label="native_validation_builder_registry",
)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


class NativeValidationExperimentExecutionError(ValueError):
    """The manifest-native experiment execution could not produce evidence."""


@dataclass(frozen=True, slots=True)
class NativeValidationExperimentExecution:
    bundle: ValidationExperimentBundle
    selected_candidate_id: str
    selected_candidate_hash: str
    manifest_candidate_universe_hash: str
    pre_holdout_eligibility: tuple[dict[str, Any], ...]
    pre_holdout_eligibility_hash: str
    nested_selection_result_hash: str
    terminal_selection_scores: tuple[dict[str, Any], ...]
    terminal_selection_scores_hash: str
    computation_receipt: dict[str, Any]
    computation_receipt_path: str
    computation_receipt_hash: str


def execute_manifest_validation_experiments(
    *,
    manifest: ExperimentManifest,
    db_path: str | Path | None,
    manager: ResearchPathManager,
    candidates: list[dict[str, Any]],
    preliminary_selection_artifact_hash: str,
    dataset_snapshot_hash: str,
    capability: ValidationExperimentCapability,
    strategy_registry: StrategyRegistry,
    progress_callback: Any = None,
) -> NativeValidationExperimentExecution:
    """Execute and immutably receipt all four pre-holdout experiments."""

    spec = manifest.validation_experiments
    if not isinstance(spec, ValidationExperimentExecutionContract):
        raise NativeValidationExperimentExecutionError(
            "validation_experiments_manifest_contract_required"
        )
    plan = build_manifest_nested_temporal_validation_plan(manifest)
    if plan is None:
        raise NativeValidationExperimentExecutionError(
            "validation_experiments_nested_temporal_plan_required"
        )
    if capability.manifest_hash != manifest.manifest_hash():
        raise NativeValidationExperimentExecutionError(
            "validation_experiments_capability_manifest_hash_mismatch"
        )
    _require_hash(
        preliminary_selection_artifact_hash,
        "validation_experiments_preliminary_selection_artifact_hash",
    )
    candidate_by_id = _authoritative_candidates(
        manifest=manifest,
        candidates=candidates,
    )
    manifest_candidate_universe_hash = _require_manifest_candidate_universe(
        manifest=manifest,
        candidate_by_id=candidate_by_id,
    )
    pre_holdout_eligibility = tuple(
        _pre_holdout_candidate_eligibility(
            manifest=manifest,
            candidate_id=candidate_id,
            candidate=candidate,
        )
        for candidate_id, candidate in sorted(candidate_by_id.items())
    )
    pre_holdout_eligibility_hash = sha256_prefixed(
        list(pre_holdout_eligibility),
        label="native_validation_pre_holdout_eligibility",
    )
    eligible_ids = tuple(
        str(item["candidate_id"])
        for item in pre_holdout_eligibility
        if item["status"] == "PASS"
    )
    # Prior returns, ranks, gates, and holdout fields never reduce the
    # preregistered universe. Execution failures are retained by the nested
    # evaluator as failed observations instead of pre-filtering candidates.
    if set(eligible_ids) != set(candidate_by_id):
        raise NativeValidationExperimentExecutionError(
            "validation_experiments_outcome_independent_universe_incomplete"
        )

    access_records: list[dict[str, Any]] = []
    nested_candidates = tuple(
        NestedCandidate(
            candidate_id=candidate_id,
            version=str(manifest.strategy_version or "1"),
            definition_hash=native_candidate_definition_hash(
                manifest_hash=manifest.manifest_hash(),
                strategy_name=manifest.strategy_name,
                strategy_version=manifest.strategy_version,
                candidate=candidate,
            ),
        )
        for candidate_id, candidate in sorted(candidate_by_id.items())
        if candidate_id in eligible_ids
    )
    nested_contract = spec.nested_selection
    _assert_nested_plan_is_pre_holdout(manifest=manifest, plan=plan)

    def evaluate_nested(
        candidate: NestedCandidate,
        split: Any,
        phase: FoldEvaluationPhase,
    ) -> MetricObservation:
        candidate_payload = candidate_by_id[candidate.candidate_id]
        evaluation_range = DateRange(start=split.test.start, end=split.test.end)
        _assert_date_range_is_pre_holdout(
            manifest=manifest,
            date_range=evaluation_range,
            component="nested_selection",
        )
        try:
            evaluation = run_manifest_candidate_range(
                manifest=manifest,
                db_path=db_path,
                manager=manager,
                date_range=evaluation_range,
                split_name=(
                    "native_nested_"
                    + phase.value.lower()
                    + "_"
                    + split.split_id
                ),
                candidate_id=candidate.candidate_id,
                parameter_values=_candidate_parameters(candidate_payload),
                strategy_registry=strategy_registry,
                progress_callback=progress_callback,
            )
            _assert_compiled_candidate_binding(
                evaluation=evaluation,
                candidate=candidate_payload,
            )
        except (ResearchValidationError, ValueError) as exc:
            failure_code = _stable_failure_code(exc)
            return MetricObservation(
                status=EvaluationStatus.FAIL,
                score=None,
                sample_count=0,
                evidence_hash=sha256_prefixed(
                    {
                        "manifest_hash": manifest.manifest_hash(),
                        "candidate_definition_hash": candidate.definition_hash,
                        "split_hash": split.split_hash(),
                        "phase": phase.value,
                        "failure_code": failure_code,
                    },
                    label="native_nested_metric_failure",
                ),
                failure_code=failure_code,
            )
        _record_range_access(
            access_records,
            component="nested_selection",
            phase=phase.value,
            evaluation=evaluation,
        )
        return MetricObservation(
            status=EvaluationStatus.PASS,
            score=float(evaluation.run.metrics.return_pct),
            sample_count=int(evaluation.run.candle_count),
            evidence_hash=evaluation.evidence_hash,
        )

    nested = execute_nested_temporal_selection(
        plan=plan,
        candidates=nested_candidates,
        policy=NestedSelectionPolicy(
            metric_id=nested_contract.metric_id,
            direction=MetricDirection(nested_contract.direction),
            minimum_inner_sample_count=(
                nested_contract.minimum_inner_sample_count
            ),
            minimum_outer_sample_count=(
                nested_contract.minimum_outer_sample_count
            ),
        ),
        evaluator=evaluate_nested,
    )
    if nested_contract.terminal_selection_rule != "outer_mean_then_candidate_id":
        raise NativeValidationExperimentExecutionError(
            "validation_experiments_terminal_selection_rule_unsupported"
        )
    terminal_selection_scores = _nested_terminal_selection_scores(nested)
    terminal_selection_scores_hash = sha256_prefixed(
        list(terminal_selection_scores),
        label="native_validation_terminal_selection_scores",
    )
    selected_candidate_id = _nested_terminal_candidate_id(nested)
    if selected_candidate_id is None:
        raise NativeValidationExperimentExecutionError(
            "validation_experiments_nested_terminal_winner_missing"
        )
    selected_candidate_hash = native_candidate_definition_hash(
        manifest_hash=manifest.manifest_hash(),
        strategy_name=manifest.strategy_name,
        strategy_version=manifest.strategy_version,
        candidate=candidate_by_id[selected_candidate_id],
    )

    _assert_date_range_is_pre_holdout(
        manifest=manifest,
        date_range=manifest.dataset.split.validation,
        component="selected_candidate_observations",
    )
    selected_evaluation = run_manifest_candidate_range(
        manifest=manifest,
        db_path=db_path,
        manager=manager,
        date_range=manifest.dataset.split.validation,
        split_name="native_validation_observations",
        candidate_id=selected_candidate_id,
        parameter_values=_candidate_parameters(
            candidate_by_id[selected_candidate_id]
        ),
        strategy_registry=strategy_registry,
        progress_callback=progress_callback,
        retain_observation_streams=True,
    )
    _assert_compiled_candidate_binding(
        evaluation=selected_evaluation,
        candidate=candidate_by_id[selected_candidate_id],
    )
    _record_range_access(
        access_records,
        component="falsification_factor_source",
        phase="VALIDATION",
        evaluation=selected_evaluation,
    )
    falsification_observations = _build_falsification_observations(
        selected_evaluation
    )
    factor_observations = _build_factor_observations(selected_evaluation)
    falsification_contract = spec.falsification
    falsification = run_falsification_suite(
        observations=falsification_observations,
        dataset_snapshot_hash=dataset_snapshot_hash,
        policy=FalsificationPolicy(
            policy_id=falsification_contract.policy_id,
            version=falsification_contract.version,
            seed=falsification_contract.seed,
            placebo_shift=falsification_contract.placebo_shift,
            minimum_sample_count=falsification_contract.minimum_sample_count,
            minimum_baseline_abs_effect=(
                falsification_contract.minimum_baseline_abs_effect
            ),
            maximum_control_abs_effect=(
                falsification_contract.maximum_control_abs_effect
            ),
            minimum_confounder_adjusted_retention=(
                falsification_contract.minimum_confounder_adjusted_retention
            ),
        ),
        include_confounder_adjusted=(
            falsification_contract.include_confounder_adjusted
        ),
    )
    factor_contract = spec.factor_exposure
    factor = estimate_factor_exposures(
        observations=factor_observations,
        dataset_snapshot_hash=dataset_snapshot_hash,
        model_id=factor_contract.model_id,
        model_version=factor_contract.model_version,
        hac_lags=factor_contract.hac_lags,
        confidence_z=factor_contract.confidence_z,
    )

    provider_contract = spec.provider_sensitivity
    semantic_definition_hash = sha256_prefixed(
        {
            "semantic_definition_id": provider_contract.semantic_definition_id,
            "metrics": list(provider_contract.metrics),
            "strategy_name": manifest.strategy_name,
            "strategy_version": manifest.strategy_version,
            "execution_model": manifest.execution_model.as_dict(),
            "execution_timing": manifest.execution_timing.as_dict(),
            "portfolio_policy_hash": manifest.portfolio_policy_hash(),
            "risk_policy": manifest.risk_policy.as_dict(),
            "validation_range": manifest.dataset.split.validation.as_dict(),
        },
        label="native_provider_semantic_definition",
    )
    provider_results: list[ProviderResearchResult] = []
    provider_input_refs: list[dict[str, Any]] = []
    for provider_ref in provider_contract.provider_datasets:
        provider_manifest = _manifest_for_provider(
            manifest=manifest,
            artifact_manifest_uri=provider_ref.artifact_manifest_uri,
            artifact_manifest_hash=provider_ref.artifact_manifest_hash,
        )
        _assert_date_range_is_pre_holdout(
            manifest=manifest,
            date_range=manifest.dataset.split.validation,
            component="provider_sensitivity",
        )
        provider_evaluation = run_manifest_candidate_range(
            manifest=provider_manifest,
            db_path=db_path,
            manager=manager,
            date_range=manifest.dataset.split.validation,
            split_name="native_provider_" + provider_ref.provider_id,
            candidate_id=selected_candidate_id,
            parameter_values=_candidate_parameters(
                candidate_by_id[selected_candidate_id]
            ),
            strategy_registry=strategy_registry,
            progress_callback=progress_callback,
        )
        _assert_compiled_candidate_binding(
            evaluation=provider_evaluation,
            candidate=candidate_by_id[selected_candidate_id],
        )
        _record_range_access(
            access_records,
            component="provider_sensitivity",
            phase="VALIDATION",
            evaluation=provider_evaluation,
            provider_id=provider_ref.provider_id,
        )
        metrics_payload = provider_evaluation.run.metrics.as_dict()
        metric_values: list[tuple[str, float]] = []
        for metric_id in provider_contract.metrics:
            raw = metrics_payload.get(metric_id)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise NativeValidationExperimentExecutionError(
                    "native_provider_metric_unavailable:" + metric_id
                )
            value = float(raw)
            if not math.isfinite(value):
                raise NativeValidationExperimentExecutionError(
                    "native_provider_metric_non_finite:" + metric_id
                )
            metric_values.append((metric_id, value))
        snapshot = provider_evaluation.snapshot
        report_hash = sha256_prefixed(
            {
                "provider_id": provider_ref.provider_id,
                "artifact_manifest_uri": provider_ref.artifact_manifest_uri,
                "artifact_manifest_hash": provider_ref.artifact_manifest_hash,
                "artifact_content_hash": snapshot.artifact_content_hash,
                "snapshot_fingerprint_hash": snapshot.snapshot_fingerprint_hash(),
                "evaluation_hash": provider_evaluation.evidence_hash,
                "semantic_definition_hash": semantic_definition_hash,
                "metrics": metric_values,
            },
            label="native_provider_research_report",
        )
        provider_results.append(
            ProviderResearchResult(
                provider_id=provider_ref.provider_id,
                dataset_snapshot_hash=(
                    dataset_snapshot_hash
                    if provider_ref.provider_id
                    == provider_contract.selected_provider_id
                    else snapshot.snapshot_fingerprint_hash()
                ),
                semantic_definition_hash=semantic_definition_hash,
                report_hash=report_hash,
                metrics=tuple(metric_values),
            )
        )
        provider_input_refs.append(
            {
                "provider_id": provider_ref.provider_id,
                "artifact_manifest_uri": provider_ref.artifact_manifest_uri,
                "artifact_manifest_hash": provider_ref.artifact_manifest_hash,
                "artifact_id": snapshot.artifact_id,
                "artifact_content_hash": snapshot.artifact_content_hash,
                "artifact_schema_hash": snapshot.artifact_schema_hash,
                "snapshot_fingerprint_hash": snapshot.snapshot_fingerprint_hash(),
                "snapshot_query_hash": snapshot.snapshot_query_hash(),
                "snapshot_data_hash": snapshot.snapshot_data_hash(),
            }
        )
    provider = compare_provider_research_results(
        results=tuple(provider_results),
        selected_provider_id=provider_contract.selected_provider_id,
        tolerances=tuple(
            ProviderMetricTolerance(
                metric_id=item.metric_id,
                absolute_tolerance=item.absolute_tolerance,
                relative_tolerance=item.relative_tolerance,
            )
            for item in provider_contract.tolerances
        ),
    )

    scope = ValidationExperimentOutputScope(
        manifest_hash=manifest.manifest_hash(),
        capability_hash=capability.content_hash,
        dataset_snapshot_hash=dataset_snapshot_hash,
        temporal_plan_hash=plan.contract_hash(),
        selected_candidate_id=selected_candidate_id,
        selected_candidate_hash=selected_candidate_hash,
    )
    bundle = build_validation_experiment_bundle(
        manifest_hash=manifest.manifest_hash(),
        dataset_snapshot_hash=dataset_snapshot_hash,
        temporal_plan_hash=plan.contract_hash(),
        selected_candidate_id=selected_candidate_id,
        selected_candidate_hash=selected_candidate_hash,
        capability=capability,
        policy=capability.policy,
        outputs=ValidationExperimentOutputs(
            scope=scope,
            nested_selection=nested,
            falsification=falsification,
            factor_exposure=factor,
            provider_sensitivity=provider,
        ),
    )

    spec_hash = sha256_prefixed(
        spec.as_dict(), label="validation_experiment_execution_spec"
    )
    manifest_candidate_universe = [
        {
            "candidate_id": candidate_id,
            "parameter_values_hash": sha256_prefixed(
                _candidate_parameters(candidate),
                label="manifest_candidate_parameters",
            ),
            "candidate_definition_hash": native_candidate_definition_hash(
                manifest_hash=manifest.manifest_hash(),
                strategy_name=manifest.strategy_name,
                strategy_version=manifest.strategy_version,
                candidate=candidate,
            ),
        }
        for candidate_id, candidate in sorted(candidate_by_id.items())
    ]
    receipt_material = {
        "schema_version": NATIVE_VALIDATION_EXECUTION_SCHEMA_VERSION,
        "artifact_type": NATIVE_VALIDATION_EXECUTION_ARTIFACT_TYPE,
        "execution_authority": "manifest_native_production_engine",
        "manifest_hash": manifest.manifest_hash(),
        "execution_spec_hash": spec_hash,
        "builder_registry_hash": NATIVE_VALIDATION_BUILDER_REGISTRY_HASH,
        "capability_hash": capability.content_hash,
        "dataset_snapshot_hash": dataset_snapshot_hash,
        "temporal_plan_hash": plan.contract_hash(),
        "candidate_universe_hash": sha256_prefixed(
            [item.as_dict() for item in nested_candidates],
            label="native_validation_candidate_universe",
        ),
        "manifest_candidate_universe": manifest_candidate_universe,
        "manifest_candidate_universe_hash": manifest_candidate_universe_hash,
        "pre_holdout_eligibility": list(pre_holdout_eligibility),
        "pre_holdout_eligibility_hash": pre_holdout_eligibility_hash,
        "preliminary_selection_artifact_hash": preliminary_selection_artifact_hash,
        "selected_candidate_id": selected_candidate_id,
        "selected_candidate_hash": selected_candidate_hash,
        "nested_selection_result_hash": nested.content_hash,
        "terminal_selection_rule": nested_contract.terminal_selection_rule,
        "terminal_selection_scores": list(terminal_selection_scores),
        "terminal_selection_scores_hash": terminal_selection_scores_hash,
        "raw_immutable_input_artifact_refs": provider_input_refs,
        "source_access_records": sorted(
            access_records,
            key=lambda item: (
                str(item.get("component")),
                str(item.get("provider_id") or ""),
                str(item.get("split_name")),
                str(item.get("candidate_id")),
            ),
        ),
        "observation_hashes": {
            "falsification": sha256_prefixed(
                [item.as_dict() for item in falsification_observations],
                label="falsification_observations",
            ),
            "factor_exposure": factor.observation_hash,
        },
        "result_hashes": {
            "nested_selection": nested.content_hash,
            "falsification": falsification.content_hash,
            "factor_exposure": factor.content_hash,
            "provider_sensitivity": provider.content_hash,
        },
        "validation_experiment_bundle_hash": bundle.content_hash,
        "holdout_accessed": False,
    }
    receipt_hash = sha256_prefixed(
        receipt_material,
        label="native_validation_experiment_computation_receipt",
    )
    receipt = {**receipt_material, "content_hash": receipt_hash}
    receipt_path = manager.research_artifact_path(
        manifest.experiment_id,
        "validation_experiments",
        receipt_hash.removeprefix("sha256:") + ".json",
    )
    write_json_atomic_create_or_verify(receipt_path, receipt)
    return NativeValidationExperimentExecution(
        bundle=bundle,
        selected_candidate_id=selected_candidate_id,
        selected_candidate_hash=selected_candidate_hash,
        manifest_candidate_universe_hash=manifest_candidate_universe_hash,
        pre_holdout_eligibility=pre_holdout_eligibility,
        pre_holdout_eligibility_hash=pre_holdout_eligibility_hash,
        nested_selection_result_hash=nested.content_hash,
        terminal_selection_scores=terminal_selection_scores,
        terminal_selection_scores_hash=terminal_selection_scores_hash,
        computation_receipt=receipt,
        computation_receipt_path=str(receipt_path.resolve()),
        computation_receipt_hash=receipt_hash,
    )


def validate_native_validation_computation_receipt(
    value: object,
    *,
    expected_manifest_hash: str | None = None,
    expected_bundle_hash: str | None = None,
    expected_bundle: Mapping[str, Any] | None = None,
    expected_selected_candidate_id: str | None = None,
    expected_selected_candidate_hash: str | None = None,
) -> list[str]:
    """Validate the immutable native computation receipt without executing it."""

    if not isinstance(value, dict):
        return ["native_validation_computation_receipt_must_be_object"]
    reasons: list[str] = []
    material = dict(value)
    recorded = material.pop("content_hash", None)
    expected_fields = {
        "schema_version",
        "artifact_type",
        "execution_authority",
        "manifest_hash",
        "execution_spec_hash",
        "builder_registry_hash",
        "capability_hash",
        "dataset_snapshot_hash",
        "temporal_plan_hash",
        "candidate_universe_hash",
        "manifest_candidate_universe",
        "manifest_candidate_universe_hash",
        "pre_holdout_eligibility",
        "pre_holdout_eligibility_hash",
        "preliminary_selection_artifact_hash",
        "selected_candidate_id",
        "selected_candidate_hash",
        "nested_selection_result_hash",
        "terminal_selection_rule",
        "terminal_selection_scores",
        "terminal_selection_scores_hash",
        "raw_immutable_input_artifact_refs",
        "source_access_records",
        "observation_hashes",
        "result_hashes",
        "validation_experiment_bundle_hash",
        "holdout_accessed",
    }
    if set(material) != expected_fields:
        reasons.append("native_validation_computation_receipt_fields_invalid")
    if material.get("schema_version") != NATIVE_VALIDATION_EXECUTION_SCHEMA_VERSION:
        reasons.append("native_validation_computation_receipt_schema_invalid")
    if material.get("artifact_type") != NATIVE_VALIDATION_EXECUTION_ARTIFACT_TYPE:
        reasons.append("native_validation_computation_receipt_type_invalid")
    if material.get("execution_authority") != "manifest_native_production_engine":
        reasons.append("native_validation_computation_authority_invalid")
    if material.get("builder_registry_hash") != NATIVE_VALIDATION_BUILDER_REGISTRY_HASH:
        reasons.append("native_validation_builder_registry_hash_mismatch")
    if material.get("holdout_accessed") is not False:
        reasons.append("native_validation_computation_holdout_access_invalid")
    for field in (
        "manifest_hash",
        "execution_spec_hash",
        "capability_hash",
        "dataset_snapshot_hash",
        "temporal_plan_hash",
        "manifest_candidate_universe_hash",
        "candidate_universe_hash",
        "pre_holdout_eligibility_hash",
        "preliminary_selection_artifact_hash",
        "selected_candidate_hash",
        "nested_selection_result_hash",
        "terminal_selection_scores_hash",
        "validation_experiment_bundle_hash",
    ):
        field_value = material.get(field)
        if not _is_hash(field_value):
            reasons.append(
                "native_validation_computation_" + field + "_invalid"
            )

    universe = material.get("manifest_candidate_universe")
    universe_fields = {
        "candidate_id",
        "parameter_values_hash",
        "candidate_definition_hash",
    }
    universe_valid = isinstance(universe, list) and bool(universe)
    if universe_valid:
        assert isinstance(universe, list)
        universe_valid = (
            all(
                isinstance(item, dict)
                and set(item) == universe_fields
                and bool(str(item.get("candidate_id") or ""))
                and _is_hash(item.get("parameter_values_hash"))
                and _is_hash(item.get("candidate_definition_hash"))
                for item in universe
            )
            and universe
            == sorted(universe, key=lambda item: str(item.get("candidate_id")))
            and len({str(item.get("candidate_id")) for item in universe})
            == len(universe)
        )
    if not universe_valid:
        reasons.append("native_validation_manifest_candidate_universe_invalid")
    elif material.get("manifest_candidate_universe_hash") != sha256_prefixed(
        {
            "schema_version": 1,
            "manifest_hash": material.get("manifest_hash"),
            "candidate_bindings": universe,
        },
        label="manifest_native_candidate_universe",
    ):
        reasons.append("native_validation_manifest_candidate_universe_hash_mismatch")

    eligibility = material.get("pre_holdout_eligibility")
    if not isinstance(eligibility, list) or not eligibility:
        reasons.append("native_validation_pre_holdout_eligibility_invalid")
    elif material.get("pre_holdout_eligibility_hash") != sha256_prefixed(
        eligibility,
        label="native_validation_pre_holdout_eligibility",
    ):
        reasons.append("native_validation_pre_holdout_eligibility_hash_mismatch")
    else:
        eligibility_fields = {
            "schema_version",
            "candidate_id",
            "candidate_definition_hash",
            "parameter_values_hash",
            "evidence",
            "status",
            "reasons",
            "content_hash",
        }
        eligibility_valid = all(
            isinstance(item, dict)
            and set(item) == eligibility_fields
            and item.get("status") == "PASS"
            and item.get("reasons") == []
            and item.get("evidence")
            == {
                "candidate_id_matches_manifest_parameters": True,
                "candidate_definition_is_outcome_independent": True,
                "manifest_parameter_member": True,
            }
            and item.get("content_hash")
            == sha256_prefixed(
                {key: val for key, val in item.items() if key != "content_hash"},
                label="native_validation_pre_holdout_candidate_eligibility",
            )
            for item in eligibility
        )
        if not eligibility_valid:
            reasons.append("native_validation_pre_holdout_eligibility_invalid")
        selected_id = material.get("selected_candidate_id")
        selected_rows = [
            item
            for item in eligibility
            if isinstance(item, dict) and item.get("candidate_id") == selected_id
        ]
        if len(selected_rows) != 1 or selected_rows[0].get("status") != "PASS":
            reasons.append(
                "native_validation_selected_candidate_pre_holdout_ineligible"
            )
        if universe_valid:
            assert isinstance(universe, list)
            universe_bindings = {
                (
                    item.get("candidate_id"),
                    item.get("candidate_definition_hash"),
                    item.get("parameter_values_hash"),
                )
                for item in universe
            }
            eligibility_bindings = {
                (
                    item.get("candidate_id"),
                    item.get("candidate_definition_hash"),
                    item.get("parameter_values_hash"),
                )
                for item in eligibility
                if isinstance(item, dict)
            }
            if eligibility_bindings != universe_bindings:
                reasons.append(
                    "native_validation_pre_holdout_eligibility_universe_mismatch"
                )

    scores = material.get("terminal_selection_scores")
    score_fields = {
        "candidate_id",
        "candidate_definition_hash",
        "outer_pass_count",
        "outer_mean_score",
        "selected",
        "content_hash",
    }
    scores_valid = isinstance(scores, list) and bool(scores)
    if scores_valid:
        assert isinstance(scores, list)
        scores_valid = all(
            isinstance(item, dict)
            and set(item) == score_fields
            and _is_hash(item.get("candidate_definition_hash"))
            and isinstance(item.get("outer_pass_count"), int)
            and not isinstance(item.get("outer_pass_count"), bool)
            and int(item.get("outer_pass_count") or 0) >= 0
            and (
                item.get("outer_mean_score") is None
                or (
                    isinstance(item.get("outer_mean_score"), (int, float))
                    and not isinstance(item.get("outer_mean_score"), bool)
                    and math.isfinite(float(item["outer_mean_score"]))
                )
            )
            and isinstance(item.get("selected"), bool)
            and item.get("content_hash")
            == sha256_prefixed(
                {key: val for key, val in item.items() if key != "content_hash"},
                label="native_validation_terminal_candidate_score",
            )
            for item in scores
        )
        score_ids = [str(item.get("candidate_id") or "") for item in scores]
        scores_valid = scores_valid and score_ids == sorted(set(score_ids))
        eligible_scores = [
            item
            for item in scores
            if item.get("outer_mean_score") is not None
        ]
        expected_winner = (
            min(
                (
                    item
                    for item in eligible_scores
                    if item.get("outer_mean_score")
                    == max(row.get("outer_mean_score") for row in eligible_scores)
                ),
                key=lambda item: str(item.get("candidate_id")),
            ).get("candidate_id")
            if eligible_scores
            else None
        )
        selected_scores = [item for item in scores if item.get("selected") is True]
        scores_valid = (
            scores_valid
            and len(selected_scores) == 1
            and selected_scores[0].get("candidate_id") == expected_winner
            and expected_winner == material.get("selected_candidate_id")
        )
        if universe_valid:
            assert isinstance(universe, list)
            scores_valid = scores_valid and {
                (item.get("candidate_id"), item.get("candidate_definition_hash"))
                for item in scores
            } == {
                (item.get("candidate_id"), item.get("candidate_definition_hash"))
                for item in universe
            }
    if (
        material.get("terminal_selection_rule") != "outer_mean_then_candidate_id"
        or not scores_valid
        or not isinstance(scores, list)
        or material.get("terminal_selection_scores_hash")
        != sha256_prefixed(
            scores if isinstance(scores, list) else [],
            label="native_validation_terminal_selection_scores",
        )
    ):
        reasons.append("native_validation_terminal_selection_scores_invalid")

    result_hashes = material.get("result_hashes")
    if (
        not isinstance(result_hashes, dict)
        or result_hashes.get("nested_selection")
        != material.get("nested_selection_result_hash")
    ):
        reasons.append("native_validation_nested_selection_result_hash_mismatch")
    elif set(result_hashes) != {
        "nested_selection",
        "falsification",
        "factor_exposure",
        "provider_sensitivity",
    } or any(not _is_hash(item) for item in result_hashes.values()):
        reasons.append("native_validation_result_hashes_invalid")
    observation_hashes = material.get("observation_hashes")
    if (
        not isinstance(observation_hashes, dict)
        or set(observation_hashes) != {"falsification", "factor_exposure"}
        or any(not _is_hash(item) for item in observation_hashes.values())
    ):
        reasons.append("native_validation_observation_hashes_invalid")
    access_records = material.get("source_access_records")
    if not isinstance(access_records, list) or not access_records:
        reasons.append("native_validation_source_access_records_invalid")
    else:
        selected_id = material.get("selected_candidate_id")
        required_access_fields = {
            "access_scope",
            "component",
            "phase",
            "split_name",
            "candidate_id",
            "requested_range",
            "artifact_id",
            "artifact_manifest_hash",
            "artifact_content_hash",
            "snapshot_fingerprint_hash",
            "snapshot_query_hash",
            "snapshot_data_hash",
            "evaluation_hash",
            "compiled_strategy_contract_hash",
        }
        if any(
            not isinstance(item, dict)
            or not required_access_fields.issubset(item)
            or item.get("access_scope") != "pre_holdout_only"
            or "holdout" in str(item.get("split_name") or "").lower()
            or not isinstance(item.get("requested_range"), dict)
            or not str(item.get("artifact_id") or "")
            or any(
                not _is_hash(item.get(hash_field))
                for hash_field in (
                    "artifact_manifest_hash",
                    "artifact_content_hash",
                    "snapshot_fingerprint_hash",
                    "snapshot_query_hash",
                    "snapshot_data_hash",
                    "evaluation_hash",
                    "compiled_strategy_contract_hash",
                )
            )
            or (
                item.get("component") != "nested_selection"
                and item.get("candidate_id") != selected_id
            )
            for item in access_records
        ) or access_records != sorted(
            access_records,
            key=lambda item: (
                str(item.get("component")),
                str(item.get("provider_id") or ""),
                str(item.get("split_name")),
                str(item.get("candidate_id")),
            ),
        ):
            reasons.append("native_validation_source_access_records_invalid")
    raw_refs = material.get("raw_immutable_input_artifact_refs")
    raw_ref_fields = {
        "provider_id",
        "artifact_manifest_uri",
        "artifact_manifest_hash",
        "artifact_id",
        "artifact_content_hash",
        "artifact_schema_hash",
        "snapshot_fingerprint_hash",
        "snapshot_query_hash",
        "snapshot_data_hash",
    }
    raw_refs_valid = isinstance(raw_refs, list) and len(raw_refs) >= 2
    if raw_refs_valid:
        assert isinstance(raw_refs, list)
        raw_refs_valid = (
            all(
                isinstance(item, dict)
                and set(item) == raw_ref_fields
                and bool(str(item.get("provider_id") or ""))
                and bool(str(item.get("artifact_id") or ""))
                and isinstance(item.get("artifact_manifest_uri"), str)
                and Path(str(item.get("artifact_manifest_uri"))).is_absolute()
                and all(
                    _is_hash(item.get(hash_field))
                    for hash_field in raw_ref_fields
                    - {
                        "provider_id",
                        "artifact_id",
                        "artifact_manifest_uri",
                    }
                )
                for item in raw_refs
            )
            and raw_refs
            == sorted(raw_refs, key=lambda item: str(item.get("provider_id")))
            and len({str(item.get("provider_id")) for item in raw_refs})
            == len(raw_refs)
        )
        if raw_refs_valid and isinstance(access_records, list):
            provider_rows = [
                item
                for item in access_records
                if isinstance(item, dict)
                and item.get("component") == "provider_sensitivity"
            ]
            provider_access = {
                str(item.get("provider_id")): item
                for item in provider_rows
            }
            raw_refs_valid = (
                len(provider_rows) == len(raw_refs)
                and set(provider_access)
                == {str(item.get("provider_id")) for item in raw_refs}
                and all(
                    provider_access[str(item.get("provider_id"))].get(field_name)
                    == item.get(field_name)
                    for item in raw_refs
                    for field_name in (
                        "artifact_id",
                        "artifact_manifest_hash",
                        "artifact_content_hash",
                        "snapshot_fingerprint_hash",
                        "snapshot_query_hash",
                        "snapshot_data_hash",
                    )
                )
            )
    if not raw_refs_valid:
        reasons.append("native_validation_raw_input_artifact_refs_invalid")
    if expected_bundle is not None:
        reasons.extend(
            _receipt_bundle_binding_reasons(material=material, bundle=expected_bundle)
        )
    calculated = sha256_prefixed(
        material,
        label="native_validation_experiment_computation_receipt",
    )
    if recorded != calculated:
        reasons.append("native_validation_computation_receipt_hash_mismatch")
    if expected_manifest_hash is not None and material.get("manifest_hash") != (
        expected_manifest_hash
    ):
        reasons.append("native_validation_computation_manifest_hash_mismatch")
    if expected_bundle_hash is not None and material.get(
        "validation_experiment_bundle_hash"
    ) != expected_bundle_hash:
        reasons.append("native_validation_computation_bundle_hash_mismatch")
    if expected_selected_candidate_id is not None and material.get(
        "selected_candidate_id"
    ) != expected_selected_candidate_id:
        reasons.append("native_validation_computation_selected_candidate_id_mismatch")
    if expected_selected_candidate_hash is not None and material.get(
        "selected_candidate_hash"
    ) != expected_selected_candidate_hash:
        reasons.append("native_validation_computation_selected_candidate_hash_mismatch")
    return sorted(set(reasons))


def _authoritative_candidates(
    *,
    manifest: ExperimentManifest,
    candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        candidate_id = str(candidate.get("parameter_candidate_id") or "")
        if not candidate_id or candidate_id in result:
            raise NativeValidationExperimentExecutionError(
                "validation_experiments_candidate_universe_invalid"
            )
        _candidate_parameters(candidate)
        result[candidate_id] = candidate
    if not result:
        raise NativeValidationExperimentExecutionError(
            "validation_experiments_candidate_universe_empty"
        )
    expected = {
        manifest_candidate_id(parameters, index): parameters
        for index, parameters in enumerate(
            iter_parameter_candidates(manifest.parameter_space)
        )
    }
    if set(result) != set(expected):
        raise NativeValidationExperimentExecutionError(
            "validation_experiments_manifest_candidate_identity_mismatch"
        )
    for candidate_id, parameters in expected.items():
        if _candidate_parameters(result[candidate_id]) != parameters:
            raise NativeValidationExperimentExecutionError(
                "validation_experiments_manifest_candidate_identity_rebound"
            )
        native_candidate_definition_hash(
            manifest_hash=manifest.manifest_hash(),
            strategy_name=manifest.strategy_name,
            strategy_version=manifest.strategy_version,
            candidate=result[candidate_id],
        )
    return result


def _require_manifest_candidate_universe(
    *,
    manifest: ExperimentManifest,
    candidate_by_id: dict[str, dict[str, Any]],
) -> str:
    """Prove that the executor received exactly the preregistered grid.

    Candidate identifiers are engine-generated, so universe equality is based
    on the canonical parameter assignments declared by the manifest.  Neither
    a caller nor a prior terminal selection can add, remove, or duplicate a
    nested candidate.
    """

    actual_bindings = [
        {
            "candidate_id": candidate_id,
            "parameter_values_hash": sha256_prefixed(
                _candidate_parameters(candidate),
                label="manifest_candidate_parameters",
            ),
            "candidate_definition_hash": native_candidate_definition_hash(
                manifest_hash=manifest.manifest_hash(),
                strategy_name=manifest.strategy_name,
                strategy_version=manifest.strategy_version,
                candidate=candidate,
            ),
        }
        for candidate_id, candidate in sorted(candidate_by_id.items())
    ]
    return sha256_prefixed(
        {
            "schema_version": 1,
            "manifest_hash": manifest.manifest_hash(),
            "candidate_bindings": actual_bindings,
        },
        label="manifest_native_candidate_universe",
    )


def _pre_holdout_candidate_eligibility(
    *,
    manifest: ExperimentManifest,
    candidate_id: str,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Project only direct evidence available before nested selection.

    In particular this projection deliberately excludes terminal candidate
    identifiers, final-holdout fields, final-selection ranks, and aggregate or
    ordinary acceptance results.  Those fields are downstream consumers of
    nested selection and must never influence its candidate universe.
    """

    evidence = {
        "candidate_id_matches_manifest_parameters": True,
        "candidate_definition_is_outcome_independent": True,
        "manifest_parameter_member": True,
    }
    reasons: list[str] = []
    material = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "candidate_definition_hash": native_candidate_definition_hash(
            manifest_hash=manifest.manifest_hash(),
            strategy_name=manifest.strategy_name,
            strategy_version=manifest.strategy_version,
            candidate=candidate,
        ),
        "parameter_values_hash": sha256_prefixed(
            _candidate_parameters(candidate),
            label="manifest_candidate_parameters",
        ),
        "evidence": evidence,
        "status": "PASS" if not reasons else "FAIL",
        "reasons": reasons,
    }
    return {
        **material,
        "content_hash": sha256_prefixed(
            material,
            label="native_validation_pre_holdout_candidate_eligibility",
        ),
    }


def _nested_terminal_selection_scores(
    result: NestedSelectionResult,
) -> tuple[dict[str, Any], ...]:
    scores: dict[str, list[float]] = {}
    for fold in result.folds:
        evaluation = fold.outer_evaluation
        if evaluation.status is not EvaluationStatus.PASS or evaluation.score is None:
            continue
        scores.setdefault(fold.selected_candidate.candidate_id, []).append(
            float(evaluation.score)
        )
    means = {
        candidate_id: _canonical_mean(values)
        for candidate_id, values in scores.items()
    }
    best = (
        (
            max(means.values())
            if result.policy.direction is MetricDirection.MAXIMIZE
            else min(means.values())
        )
        if means
        else None
    )
    winner = (
        min(candidate_id for candidate_id, score in means.items() if score == best)
        if best is not None
        else None
    )
    rows: list[dict[str, Any]] = []
    for candidate in result.candidates:
        values = scores.get(candidate.candidate_id, [])
        material = {
            "candidate_id": candidate.candidate_id,
            "candidate_definition_hash": candidate.definition_hash,
            "outer_pass_count": len(values),
            "outer_mean_score": means.get(candidate.candidate_id),
            "selected": candidate.candidate_id == winner,
        }
        rows.append(
            {
                **material,
                "content_hash": sha256_prefixed(
                    material,
                    label="native_validation_terminal_candidate_score",
                ),
            }
        )
    return tuple(rows)


def _nested_terminal_candidate_id(result: NestedSelectionResult) -> str | None:
    return next(
        (
            str(item["candidate_id"])
            for item in _nested_terminal_selection_scores(result)
            if item["selected"] is True
        ),
        None,
    )


def _candidate_parameters(candidate: dict[str, Any]) -> dict[str, Any]:
    parameters = candidate.get("parameter_values")
    if not isinstance(parameters, dict):
        raise NativeValidationExperimentExecutionError(
            "validation_experiments_candidate_parameters_missing"
        )
    return dict(parameters)


def native_candidate_definition_hash(
    *,
    manifest_hash: str,
    strategy_name: str,
    strategy_version: str | None,
    candidate: Mapping[str, Any],
) -> str:
    """Hash only preregistered, executable candidate-definition evidence."""

    _require_hash(manifest_hash, "native_candidate_definition_manifest_hash")
    if (
        not isinstance(strategy_name, str)
        or not strategy_name
        or strategy_name != strategy_name.strip()
    ):
        raise NativeValidationExperimentExecutionError(
            "native_candidate_definition_strategy_name_invalid"
        )
    if strategy_version is not None and (
        not isinstance(strategy_version, str)
        or not strategy_version
        or strategy_version != strategy_version.strip()
    ):
        raise NativeValidationExperimentExecutionError(
            "native_candidate_definition_strategy_version_invalid"
        )
    candidate_identity = str(
        candidate.get("parameter_candidate_id") or candidate.get("candidate_id") or ""
    )
    if not candidate_identity:
        raise NativeValidationExperimentExecutionError(
            "native_candidate_definition_candidate_id_missing"
        )
    parameters = candidate.get("parameter_values")
    if not isinstance(parameters, Mapping):
        parameters = candidate.get("parameter_values_raw")
    raw_binding = candidate.get("selection_binding")
    binding = raw_binding if isinstance(raw_binding, Mapping) else {}
    parameter_values_hash = (
        sha256_prefixed(dict(parameters))
        if isinstance(parameters, Mapping)
        else binding.get("parameter_values_hash")
    )
    effective_hash = candidate.get(
        "effective_strategy_parameters_hash",
        binding.get("effective_strategy_parameters_hash"),
    )
    compiled_hash = candidate.get(
        "compiled_strategy_contract_hash",
        binding.get("compiled_strategy_contract_hash"),
    )
    for field_name, field_value in (
        ("parameter_values_hash", parameter_values_hash),
        ("effective_strategy_parameters_hash", effective_hash),
        ("compiled_strategy_contract_hash", compiled_hash),
    ):
        _require_hash(field_value, "native_candidate_definition_" + field_name)
    material = {
        "schema_version": 1,
        "manifest_hash": manifest_hash,
        "strategy_name": strategy_name,
        "strategy_version": strategy_version,
        "candidate_id": candidate_identity,
        "parameter_values_hash": parameter_values_hash,
        "effective_strategy_parameters_hash": effective_hash,
        "compiled_strategy_contract_hash": compiled_hash,
    }
    return sha256_prefixed(material, label="native_candidate_definition")


def _stable_failure_code(exc: BaseException) -> str:
    raw = str(exc).split(":", 1)[0].strip().upper()
    normalized = "".join(char if char.isalnum() else "_" for char in raw)
    return (normalized or type(exc).__name__.upper())[:255]


def _assert_compiled_candidate_binding(
    *,
    evaluation: ManifestCandidateRangeEvaluation,
    candidate: Mapping[str, Any],
) -> None:
    expected = candidate.get("compiled_strategy_contract_hash")
    if (
        not _is_hash(expected)
        or evaluation.compiled_strategy_contract_hash != expected
    ):
        raise NativeValidationExperimentExecutionError(
            "native_validation_compiled_candidate_binding_mismatch"
        )


def _record_range_access(
    sink: list[dict[str, Any]],
    *,
    component: str,
    phase: str,
    evaluation: ManifestCandidateRangeEvaluation,
    provider_id: str | None = None,
) -> None:
    snapshot = evaluation.snapshot
    row: dict[str, Any] = {
        "access_scope": "pre_holdout_only",
        "component": component,
        "phase": phase,
        "split_name": evaluation.split_name,
        "candidate_id": evaluation.candidate_id,
        "requested_range": snapshot.date_range.as_dict(),
        "artifact_id": snapshot.artifact_id,
        "artifact_manifest_hash": snapshot.artifact_manifest_hash,
        "artifact_content_hash": snapshot.artifact_content_hash,
        "snapshot_fingerprint_hash": snapshot.snapshot_fingerprint_hash(),
        "snapshot_query_hash": snapshot.snapshot_query_hash(),
        "snapshot_data_hash": snapshot.snapshot_data_hash(),
        "evaluation_hash": evaluation.evidence_hash,
        "compiled_strategy_contract_hash": (
            evaluation.compiled_strategy_contract_hash
        ),
    }
    if provider_id is not None:
        row["provider_id"] = provider_id
    sink.append(row)


def _equity_candle_rows(
    evaluation: ManifestCandidateRangeEvaluation,
) -> list[tuple[Any, Any]]:
    candles = list(evaluation.snapshot.candles)
    points = list(evaluation.run.equity_curve)
    point_by_mark_ts: dict[int, Any] = {}
    for equity_point in points:
        mark_ts = int(equity_point.mark_ts)
        if mark_ts in point_by_mark_ts:
            raise NativeValidationExperimentExecutionError(
                "native_validation_equity_candle_alignment_invalid"
            )
        point_by_mark_ts[mark_ts] = equity_point
    rows: list[tuple[Any, Any]] = []
    for candle in candles:
        point = point_by_mark_ts.get(
            candle.available_at_ms(interval=evaluation.snapshot.interval)
        )
        if point is None or point.mark_price_source != "candle_close":
            raise NativeValidationExperimentExecutionError(
                "native_validation_equity_candle_alignment_invalid"
            )
        rows.append((candle, point))
    if len(rows) != len(candles):
        raise NativeValidationExperimentExecutionError(
            "native_validation_equity_candle_alignment_invalid"
        )
    return rows


def _build_falsification_observations(
    evaluation: ManifestCandidateRangeEvaluation,
) -> tuple[FalsificationObservation, ...]:
    rows = _equity_candle_rows(evaluation)
    observations: list[FalsificationObservation] = []
    for index, ((current, point), (future, _future_point)) in enumerate(
        zip(rows, rows[1:]),
        start=1,
    ):
        if point.equity == 0 or point.mark_price is None or current.close == 0:
            raise NativeValidationExperimentExecutionError(
                "native_falsification_observation_denominator_invalid"
            )
        exposure = float(point.asset_qty) * float(point.mark_price) / float(
            point.equity
        )
        outcome = float(future.close) / float(current.close) - 1.0
        observed_at = datetime.fromtimestamp(
            current.ts / 1000.0, tz=timezone.utc
        ).isoformat()
        known_at = datetime.fromtimestamp(
            future.available_at_ms(interval=evaluation.snapshot.interval) / 1000.0,
            tz=timezone.utc,
        ).isoformat()
        source_hash = sha256_prefixed(
            {
                "evaluation_hash": evaluation.evidence_hash,
                "current_candle": current.as_tuple(),
                "future_candle": future.as_tuple(),
                "equity_point": point.as_dict(),
            },
            label="native_falsification_observation_source",
        )
        observations.append(
            FalsificationObservation(
                sample_id=f"falsification-{index:06d}",
                observed_at=observed_at,
                known_at=known_at,
                signal=exposure,
                outcome=outcome,
                negative_control=math.sin(index * 1.7),
                confounders=(("calendar-parity", float(index % 2)),),
                source_hash=source_hash,
            )
        )
    return tuple(observations)


def _build_factor_observations(
    evaluation: ManifestCandidateRangeEvaluation,
) -> tuple[FactorObservation, ...]:
    rows = _equity_candle_rows(evaluation)
    observations: list[FactorObservation] = []
    for index, ((previous, previous_point), (current, current_point)) in enumerate(
        zip(rows, rows[1:]),
        start=1,
    ):
        if previous_point.equity == 0 or previous.close == 0:
            raise NativeValidationExperimentExecutionError(
                "native_factor_observation_denominator_invalid"
            )
        observed_at = datetime.fromtimestamp(
            current.ts / 1000.0, tz=timezone.utc
        ).isoformat()
        known_at = datetime.fromtimestamp(
            current.available_at_ms(interval=evaluation.snapshot.interval) / 1000.0,
            tz=timezone.utc,
        ).isoformat()
        source_hash = sha256_prefixed(
            {
                "evaluation_hash": evaluation.evidence_hash,
                "previous_candle": previous.as_tuple(),
                "current_candle": current.as_tuple(),
                "previous_equity": previous_point.as_dict(),
                "current_equity": current_point.as_dict(),
            },
            label="native_factor_observation_source",
        )
        observations.append(
            FactorObservation(
                sample_id=f"factor-{index:06d}",
                observed_at=observed_at,
                known_at=known_at,
                strategy_return=(
                    float(current_point.equity) / float(previous_point.equity) - 1.0
                ),
                factor_returns=(
                    (
                        "market-return",
                        float(current.close) / float(previous.close) - 1.0,
                    ),
                ),
                source_hash=source_hash,
            )
        )
    return tuple(observations)


def _manifest_for_provider(
    *,
    manifest: ExperimentManifest,
    artifact_manifest_uri: str,
    artifact_manifest_hash: str,
) -> ExperimentManifest:
    dataset = replace(
        manifest.dataset,
        artifact_ref=DatasetArtifactRef(
            artifact_manifest_uri=artifact_manifest_uri,
            artifact_manifest_hash=artifact_manifest_hash,
        ),
    )
    return replace(manifest, dataset=dataset)


def _assert_nested_plan_is_pre_holdout(*, manifest: ExperimentManifest, plan: Any) -> None:
    validation = manifest.dataset.split.validation
    final_holdout = manifest.dataset.split.final_holdout
    for fold in plan.outer_folds:
        outer_test = DateRange(
            start=fold.outer_split.test.start,
            end=fold.outer_split.test.end,
        )
        if (
            outer_test.start_ts_ms() < validation.start_ts_ms()
            or outer_test.end_ts_ms() > validation.end_ts_ms()
        ):
            raise NativeValidationExperimentExecutionError(
                "validation_experiments_outer_test_outside_validation"
            )
        for split in (*fold.inner_splits, fold.outer_split):
            for field_name in ("train", "purge", "embargo", "test"):
                value = getattr(split, field_name)
                candidate_range = DateRange(start=value.start, end=value.end)
                if (
                    final_holdout is not None
                    and candidate_range.end_ts_ms() >= final_holdout.start_ts_ms()
                ):
                    raise NativeValidationExperimentExecutionError(
                        "validation_experiments_nested_plan_touches_holdout"
                    )


def _assert_date_range_is_pre_holdout(
    *,
    manifest: ExperimentManifest,
    date_range: DateRange,
    component: str,
) -> None:
    final_holdout = manifest.dataset.split.final_holdout
    if (
        final_holdout is not None
        and date_range.end_ts_ms() >= final_holdout.start_ts_ms()
    ):
        raise NativeValidationExperimentExecutionError(
            "validation_experiments_holdout_access_prohibited:" + component
        )


def _receipt_bundle_binding_reasons(
    *,
    material: Mapping[str, Any],
    bundle: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    bindings = bundle.get("bindings")
    if not isinstance(bindings, Mapping):
        return ["native_validation_computation_bundle_bindings_invalid"]
    expected = {
        "manifest_hash": material.get("manifest_hash"),
        "capability_hash": material.get("capability_hash"),
        "dataset_snapshot_hash": material.get("dataset_snapshot_hash"),
        "temporal_plan_hash": material.get("temporal_plan_hash"),
        "selected_candidate_id": material.get("selected_candidate_id"),
        "selected_candidate_hash": material.get("selected_candidate_hash"),
    }
    if any(bindings.get(name) != expected_value for name, expected_value in expected.items()):
        reasons.append("native_validation_computation_bundle_bindings_mismatch")
    if bundle.get("content_hash") != material.get(
        "validation_experiment_bundle_hash"
    ):
        reasons.append("native_validation_computation_bundle_hash_mismatch")
    components = bundle.get("components")
    result_hashes = material.get("result_hashes")
    observed: dict[str, Any] = {}
    if isinstance(components, list):
        for component_payload in components:
            if not isinstance(component_payload, Mapping):
                continue
            evidence = component_payload.get("evidence")
            result = evidence.get("result") if isinstance(evidence, Mapping) else None
            if isinstance(result, Mapping):
                observed[str(component_payload.get("component") or "")] = result.get(
                    "content_hash"
                )
    if not isinstance(result_hashes, Mapping) or observed != dict(result_hashes):
        reasons.append("native_validation_computation_bundle_result_hashes_mismatch")
    return reasons


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _require_hash(value: object, field_name: str) -> None:
    if not _is_hash(value):
        raise NativeValidationExperimentExecutionError(field_name + "_invalid")


__all__ = [
    "NATIVE_VALIDATION_BUILDER_REGISTRY_HASH",
    "NativeValidationExperimentExecution",
    "NativeValidationExperimentExecutionError",
    "execute_manifest_validation_experiments",
    "native_candidate_definition_hash",
    "validate_native_validation_computation_receipt",
]
