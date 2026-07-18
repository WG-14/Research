from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, SupportsIndex, SupportsInt, cast

from market_research.paths import ResearchPathManager
from market_research.settings import ResearchSettings

from .audit_trail import validate_audit_trail_binding
from .research_classification import requires_candidate_validation
from .hashing import content_hash_payload, report_content_hash_payload, sha256_prefixed
from .statistical_selection import recompute_candidate_metric_values_hash_from_report
from .final_selection import validate_final_selection_report
from .return_panel import validate_return_panel_binding
from .family_registry import validate_family_registry_binding
from .experiment_registry import validate_experiment_registry_binding


LINEAGE_SCHEMA_VERSION = 2
LINEAGE_HASH_FIELD = "lineage_hash"
LINEAGE_HASH_EXCLUDED_FIELDS = frozenset({LINEAGE_HASH_FIELD, "created_at"})
SECRET_KEY_FRAGMENTS = ("secret", "api_key", "apikey", "token", "password", "webhook")


class LineageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReproducibilityResult:
    summary: dict[str, Any]

    @property
    def ok(self) -> bool:
        return bool(self.summary.get("ok"))


def lineage_hash_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in LINEAGE_HASH_EXCLUDED_FIELDS
    }


def compute_lineage_hash(payload: dict[str, Any]) -> str:
    return sha256_prefixed(content_hash_payload(lineage_hash_payload(payload)))


def validate_lineage_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise LineageValidationError("lineage_payload_not_object")
    if int(payload.get("lineage_schema_version") or 0) != LINEAGE_SCHEMA_VERSION:
        raise LineageValidationError("lineage_schema_version_mismatch")
    expected = payload.get(LINEAGE_HASH_FIELD)
    if not isinstance(expected, str) or not expected.startswith("sha256:"):
        raise LineageValidationError("lineage_hash_missing")
    actual = compute_lineage_hash(payload)
    if actual != expected:
        raise LineageValidationError("lineage_hash_mismatch")
    return dict(payload)


def normalized_command_args_hash(args: dict[str, Any] | None) -> str | None:
    if args is None:
        return None
    return sha256_prefixed(_redacted_mapping(args))


def safe_environment_fingerprint(values: dict[str, Any] | None) -> str | None:
    if values is None:
        return None
    return sha256_prefixed(_redacted_mapping(values))


def build_research_lineage(
    *,
    experiment_id: str,
    manifest_hash: str,
    manifest_canonical_hash: str | None = None,
    manifest_path: str | None = None,
    dataset_snapshot_id: str | None = None,
    dataset_content_hash: str | None = None,
    dataset_quality_hash: str | None = None,
    dataset_split_hash: str | None = None,
    data_source_fingerprint: str | None = None,
    dataset_adapter_provenance_hash: str | None = None,
    dataset_artifact: dict[str, Any] | None = None,
    dataset_split_evidence: dict[str, Any] | None = None,
    repository_version: str | None = None,
    command_name: str | None = None,
    command_args: dict[str, Any] | None = None,
    environment: dict[str, Any] | None = None,
    cost_execution_model_hash: str | None = None,
    portfolio_policy_hash: str | None = None,
    simulation_policy_hash: str | None = None,
    execution_calibration_artifact_hash: str | None = None,
    search_budget: int | None = None,
    parameter_grid_size: int | None = None,
    attempt_index: int | None = None,
    failed_candidate_count: int | None = None,
    holdout_reuse_count: int | None = None,
    experiment_registry_path: str | None = None,
    experiment_registry_prior_hash: str | None = None,
    experiment_registry_row_hash: str | None = None,
    experiment_registry_completion_row_hash: str | None = None,
    final_holdout_fingerprint: str | None = None,
    final_holdout_identity_hash: str | None = None,
    final_holdout_content_hash: str | None = None,
    final_holdout_reuse_key_hash: str | None = None,
    final_holdout_split_hash: str | None = None,
    dataset_artifact_evidence_hash: str | None = None,
    final_holdout_query_hash: str | None = None,
    final_holdout_data_hash: str | None = None,
    final_holdout_fingerprint_hash: str | None = None,
    final_holdout_quality_hash: str | None = None,
    experiment_registry_bound_evidence_hash: str | None = None,
    experiment_registry_evidence_hash_phase: str | None = None,
    computed_attempt_index: int | None = None,
    computed_holdout_reuse_count: int | None = None,
    declared_attempt_index: int | None = None,
    declared_holdout_reuse_count: int | None = None,
    research_freedom_hash: str | None = None,
    registry_gate_result: str | None = None,
    registry_gate_fail_reasons: list[str] | None = None,
    dataset_reuse_policy: str | None = None,
    hypothesis_id: str | None = None,
    hypothesis_status: str | None = None,
    hypothesis_identity_source: str | None = None,
    experiment_family_id: str | None = None,
    experiment_family_identity_source: str | None = None,
    pre_registered_at: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "lineage_schema_version": LINEAGE_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "experiment_family_id": experiment_family_id,
        "hypothesis_id": hypothesis_id,
        "hypothesis_status": hypothesis_status,
        "hypothesis_identity_source": hypothesis_identity_source,
        "experiment_family_identity_source": experiment_family_identity_source,
        "pre_registered_at": pre_registered_at,
        "manifest_path": manifest_path,
        "manifest_hash": manifest_hash,
        "manifest_canonical_hash": manifest_canonical_hash or manifest_hash,
        "dataset_snapshot_id": dataset_snapshot_id,
        "dataset_content_hash": dataset_content_hash,
        "dataset_quality_hash": dataset_quality_hash,
        "dataset_split_hash": dataset_split_hash or dataset_content_hash,
        "data_source_fingerprint": data_source_fingerprint,
        "dataset_adapter_provenance_hash": dataset_adapter_provenance_hash,
        "dataset_artifact": dict(dataset_artifact or {}),
        "dataset_split_evidence": dict(dataset_split_evidence or {}),
        "repository_version": repository_version,
        "command_name": command_name,
        "normalized_command_args": _redacted_mapping(command_args or {}),
        "command_args_hash": normalized_command_args_hash(command_args or {}),
        "environment_config_fingerprint": safe_environment_fingerprint(
            environment or {}
        ),
        "cost_execution_model_hash": cost_execution_model_hash,
        "portfolio_policy_hash": portfolio_policy_hash,
        "simulation_policy_hash": simulation_policy_hash,
        "execution_calibration_artifact_hash": execution_calibration_artifact_hash,
        "search_budget": search_budget,
        "parameter_grid_size": parameter_grid_size,
        "attempt_index": attempt_index,
        "failed_candidate_count": failed_candidate_count,
        "holdout_reuse_count": holdout_reuse_count,
        "experiment_registry_path": experiment_registry_path,
        "experiment_registry_prior_hash": experiment_registry_prior_hash,
        "experiment_registry_row_hash": experiment_registry_row_hash,
        "experiment_registry_completion_row_hash": experiment_registry_completion_row_hash,
        "final_holdout_fingerprint": final_holdout_fingerprint,
        "final_holdout_identity_hash": final_holdout_identity_hash,
        "final_holdout_content_hash": final_holdout_content_hash,
        "final_holdout_reuse_key_hash": final_holdout_reuse_key_hash,
        "final_holdout_split_hash": final_holdout_split_hash,
        "dataset_artifact_evidence_hash": dataset_artifact_evidence_hash,
        "final_holdout_query_hash": final_holdout_query_hash,
        "final_holdout_data_hash": final_holdout_data_hash,
        "final_holdout_fingerprint_hash": final_holdout_fingerprint_hash,
        "final_holdout_quality_hash": final_holdout_quality_hash,
        "experiment_registry_bound_evidence_hash": experiment_registry_bound_evidence_hash,
        "experiment_registry_evidence_hash_phase": experiment_registry_evidence_hash_phase,
        "computed_attempt_index": computed_attempt_index,
        "computed_holdout_reuse_count": computed_holdout_reuse_count,
        "declared_attempt_index": declared_attempt_index,
        "declared_holdout_reuse_count": declared_holdout_reuse_count,
        "research_freedom_hash": research_freedom_hash,
        "registry_gate_result": registry_gate_result,
        "registry_gate_fail_reasons": list(registry_gate_fail_reasons or []),
        "dataset_reuse_policy": dataset_reuse_policy,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }
    payload[LINEAGE_HASH_FIELD] = compute_lineage_hash(payload)
    return payload


def build_validation_lineage(
    *,
    base_lineage: dict[str, Any],
    backtest_report_path: str,
    backtest_report_hash: str,
    walk_forward_report_path: str | None,
    walk_forward_report_hash: str | None,
    candidate_id: str,
    candidate_profile_hash: str,
    validation_artifact_path: str | None = None,
    validation_artifact_hash: str | None = None,
    research_profile_path: str | None = None,
    research_profile_hash: str | None = None,
    paper_validation_evidence_path: str | None = None,
    paper_validation_evidence_hash: str | None = None,
    research_readiness_evidence_path: str | None = None,
    research_readiness_evidence_hash: str | None = None,
    decision_equivalence_report_path: str | None = None,
    decision_equivalence_report_hash: str | None = None,
    execution_calibration_artifact_hash: str | None = None,
    portfolio_policy_hash: str | None = None,
    simulation_policy_hash: str | None = None,
    statistical_evidence_path: str | None = None,
    statistical_evidence_hash: str | None = None,
    return_panel_path: str | None = None,
    return_panel_hash: str | None = None,
    selection_universe_hash: str | None = None,
    candidate_metric_values_hash: str | None = None,
    final_selection_contract_hash: str | None = None,
    selected_candidate_id: str | None = None,
    selected_candidate_score_hash: str | None = None,
    candidate_final_scores_hash: str | None = None,
    experiment_registry_path: str | None = None,
    experiment_registry_prior_hash: str | None = None,
    experiment_registry_row_hash: str | None = None,
    experiment_registry_completion_row_hash: str | None = None,
    final_holdout_fingerprint: str | None = None,
    final_holdout_identity_hash: str | None = None,
    final_holdout_content_hash: str | None = None,
    final_holdout_reuse_key_hash: str | None = None,
    final_holdout_split_hash: str | None = None,
    experiment_registry_bound_evidence_hash: str | None = None,
    experiment_registry_evidence_hash_phase: str | None = None,
    research_freedom_hash: str | None = None,
    hypothesis_identity_source: str | None = None,
    experiment_family_identity_source: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    lineage = validate_lineage_artifact(base_lineage)
    base_calibration_hash = _normalized_sha256(
        lineage.get("execution_calibration_artifact_hash")
    )
    candidate_calibration_hash = _normalized_sha256(execution_calibration_artifact_hash)
    if (
        base_calibration_hash is not None
        and candidate_calibration_hash is not None
        and base_calibration_hash != candidate_calibration_hash
    ):
        raise LineageValidationError(
            "lineage_execution_calibration_artifact_hash_mismatch"
        )
    lineage.update(
        {
            "backtest_report_path": backtest_report_path,
            "backtest_report_hash": backtest_report_hash,
            "walk_forward_report_path": walk_forward_report_path,
            "walk_forward_report_hash": walk_forward_report_hash,
            "candidate_id": candidate_id,
            "candidate_profile_hash": candidate_profile_hash,
            "validation_artifact_path": validation_artifact_path,
            "validation_artifact_hash": validation_artifact_hash,
            "research_profile_path": research_profile_path,
            "research_profile_hash": research_profile_hash,
            "paper_validation_evidence_path": paper_validation_evidence_path,
            "paper_validation_evidence_hash": paper_validation_evidence_hash,
            "research_readiness_evidence_path": research_readiness_evidence_path,
            "research_readiness_evidence_hash": research_readiness_evidence_hash,
            "decision_equivalence_report_path": decision_equivalence_report_path,
            "decision_equivalence_report_hash": decision_equivalence_report_hash,
            "execution_calibration_artifact_hash": candidate_calibration_hash
            or base_calibration_hash,
            "portfolio_policy_hash": portfolio_policy_hash
            or lineage.get("portfolio_policy_hash"),
            "simulation_policy_hash": simulation_policy_hash
            or lineage.get("simulation_policy_hash"),
            "statistical_evidence_path": statistical_evidence_path,
            "statistical_evidence_hash": statistical_evidence_hash,
            "return_panel_path": return_panel_path,
            "return_panel_hash": return_panel_hash,
            "selection_universe_hash": selection_universe_hash,
            "candidate_metric_values_hash": candidate_metric_values_hash,
            "final_selection_contract_hash": final_selection_contract_hash,
            "selected_candidate_id": selected_candidate_id,
            "selected_candidate_score_hash": selected_candidate_score_hash,
            "candidate_final_scores_hash": candidate_final_scores_hash,
            "experiment_registry_path": experiment_registry_path
            or lineage.get("experiment_registry_path"),
            "experiment_registry_prior_hash": experiment_registry_prior_hash
            or lineage.get("experiment_registry_prior_hash"),
            "experiment_registry_row_hash": experiment_registry_row_hash
            or lineage.get("experiment_registry_row_hash"),
            "experiment_registry_completion_row_hash": experiment_registry_completion_row_hash
            or lineage.get("experiment_registry_completion_row_hash"),
            "final_holdout_fingerprint": final_holdout_fingerprint
            or lineage.get("final_holdout_fingerprint"),
            "final_holdout_identity_hash": final_holdout_identity_hash
            or lineage.get("final_holdout_identity_hash"),
            "final_holdout_content_hash": final_holdout_content_hash
            or lineage.get("final_holdout_content_hash"),
            "final_holdout_reuse_key_hash": final_holdout_reuse_key_hash
            or lineage.get("final_holdout_reuse_key_hash"),
            "final_holdout_split_hash": final_holdout_split_hash
            or lineage.get("final_holdout_split_hash"),
            "experiment_registry_bound_evidence_hash": experiment_registry_bound_evidence_hash
            or lineage.get("experiment_registry_bound_evidence_hash"),
            "experiment_registry_evidence_hash_phase": experiment_registry_evidence_hash_phase
            or lineage.get("experiment_registry_evidence_hash_phase"),
            "research_freedom_hash": research_freedom_hash
            or lineage.get("research_freedom_hash"),
            "hypothesis_identity_source": hypothesis_identity_source
            or lineage.get("hypothesis_identity_source"),
            "experiment_family_identity_source": experiment_family_identity_source
            or lineage.get("experiment_family_identity_source"),
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
    )
    lineage.pop(LINEAGE_HASH_FIELD, None)
    lineage[LINEAGE_HASH_FIELD] = compute_lineage_hash(lineage)
    return lineage


def _normalized_sha256(value: object) -> str | None:
    text = str(value or "").strip()
    if text.startswith("sha256:"):
        return text
    return None


def reproduce_validation(
    validation_path: str | Path, *, manager: ResearchPathManager | None = None
) -> ReproducibilityResult:
    path = Path(validation_path).expanduser()
    active_manager = manager or ResearchPathManager.from_settings(
        ResearchSettings.from_env(), project_root=Path.cwd()
    )
    summary: dict[str, Any] = {
        "ok": False,
        "reason": "unknown",
        "validation_path": str(path),
        "validation_content_hash": None,
        "lineage_hash": None,
        "manifest_hash": None,
        "dataset_content_hash": None,
        "dataset_quality_hash": None,
        "dataset_adapter_provenance_hash": None,
        "backtest_report_hash": None,
        "walk_forward_report_hash": None,
        "candidate_profile_hash": None,
        "execution_calibration_artifact_hash": None,
        "portfolio_policy_hash": None,
        "simulation_policy_hash": None,
        "statistical_evidence_hash": None,
        "evidence_grade": None,
        "statistical_method": None,
        "manifest_bootstrap_method": None,
        "bootstrap_sampling_contract_hash": None,
        "return_panel_hash": None,
        "return_unit": None,
        "return_panel_observation_count": None,
        "family_trial_registry_path": None,
        "family_trial_registry_prior_hash": None,
        "family_trial_registry_row_hash": None,
        "experiment_registry_path": None,
        "experiment_registry_prior_hash": None,
        "experiment_registry_row_hash": None,
        "experiment_registry_completion_row_hash": None,
        "final_holdout_fingerprint": None,
        "final_holdout_identity_hash": None,
        "final_holdout_content_hash": None,
        "final_holdout_reuse_key_hash": None,
        "experiment_registry_bound_evidence_hash": None,
        "experiment_registry_evidence_hash_phase": None,
        "computed_attempt_index": None,
        "computed_holdout_reuse_count": None,
        "declared_attempt_index": None,
        "declared_holdout_reuse_count": None,
        "research_freedom_hash": None,
        "white_reality_check_p_value": None,
        "summary_metric_max_bootstrap_p_value": None,
        "statistical_gate_result": None,
        "statistical_gate_fail_reasons": [],
        "validation_eligibility_gate_result": None,
        "validation_blocking_reasons": [],
        "stress_suite_contract_hash": None,
        "validation_stress_suite_hash": None,
        "final_holdout_stress_suite_hash": None,
        "selection_universe_hash": None,
        "candidate_metric_values_hash": None,
        "final_selection_contract_hash": None,
        "selected_candidate_id": None,
        "selected_candidate_score_hash": None,
        "candidate_final_scores_hash": None,
        "audit_trail_status": None,
        "audit_trail_fail_reasons": [],
        "mismatches": [],
        "missing_artifacts": [],
        "legacy_compatibility_used": False,
    }
    if not path.exists():
        summary["reason"] = "validation_path_missing"
        summary["missing_artifacts"].append(
            {"field": "validation_path", "path": str(path)}
        )
        return ReproducibilityResult(summary)
    try:
        validation = _load_object(path)
    except ValueError as exc:
        summary["reason"] = str(exc)
        return ReproducibilityResult(summary)

    expected_validation_hash = str(validation.get("content_hash") or "")
    actual_validation_hash = sha256_prefixed(
        content_hash_payload(
            {k: v for k, v in validation.items() if k != "content_hash"}
        )
    )
    summary["validation_content_hash"] = expected_validation_hash or None
    summary["validation_eligibility_gate_result"] = validation.get(
        "validation_eligibility_gate_result"
    )
    summary["validation_blocking_reasons"] = (
        validation.get("validation_blocking_reasons") or []
    )
    if actual_validation_hash != expected_validation_hash:
        summary["reason"] = "validation_hash_mismatch"
        summary["mismatches"].append(
            _mismatch(
                "validation_content_hash",
                expected_validation_hash,
                actual_validation_hash,
                "validation_content_hash_mismatch",
            )
        )
        return ReproducibilityResult(summary)

    lineage = validation.get("lineage")
    if not isinstance(lineage, dict):
        summary["reason"] = "lineage_missing"
        summary["legacy_compatibility_used"] = bool(
            validation.get("legacy_compatibility_used")
        )
        return ReproducibilityResult(summary)
    try:
        lineage = validate_lineage_artifact(lineage)
    except LineageValidationError as exc:
        summary["reason"] = str(exc)
        return ReproducibilityResult(summary)

    summary["lineage_hash"] = lineage.get("lineage_hash")
    summary["manifest_hash"] = lineage.get("manifest_hash")
    summary["dataset_content_hash"] = lineage.get("dataset_content_hash")
    summary["dataset_quality_hash"] = lineage.get("dataset_quality_hash")
    summary["dataset_adapter_provenance_hash"] = lineage.get(
        "dataset_adapter_provenance_hash"
    )
    summary["backtest_report_hash"] = lineage.get("backtest_report_hash")
    summary["walk_forward_report_hash"] = lineage.get("walk_forward_report_hash")
    summary["candidate_profile_hash"] = lineage.get("candidate_profile_hash")
    summary["execution_calibration_artifact_hash"] = lineage.get(
        "execution_calibration_artifact_hash"
    )
    summary["portfolio_policy_hash"] = lineage.get("portfolio_policy_hash")
    summary["simulation_policy_hash"] = lineage.get("simulation_policy_hash")
    summary["statistical_evidence_hash"] = lineage.get("statistical_evidence_hash")
    summary["return_panel_hash"] = lineage.get("return_panel_hash")
    summary["evidence_grade"] = validation.get("evidence_grade")
    summary["statistical_method"] = validation.get(
        "statistical_method"
    ) or validation.get("white_reality_check_method")
    contract = validation.get("statistical_validation_contract")
    bootstrap = contract.get("bootstrap") if isinstance(contract, dict) else None
    summary["manifest_bootstrap_method"] = (
        bootstrap.get("method") if isinstance(bootstrap, dict) else None
    )
    summary["bootstrap_sampling_contract_hash"] = validation.get(
        "bootstrap_sampling_contract_hash"
    )
    summary["return_unit"] = validation.get("return_unit")
    summary["return_panel_observation_count"] = validation.get(
        "return_panel_observation_count"
    )
    summary["family_trial_registry_path"] = validation.get("family_trial_registry_path")
    summary["family_trial_registry_prior_hash"] = validation.get(
        "family_trial_registry_prior_hash"
    )
    summary["family_trial_registry_row_hash"] = validation.get(
        "family_trial_registry_row_hash"
    )
    summary["experiment_registry_path"] = validation.get("experiment_registry_path")
    summary["experiment_registry_prior_hash"] = validation.get(
        "experiment_registry_prior_hash"
    )
    summary["experiment_registry_row_hash"] = validation.get(
        "experiment_registry_row_hash"
    )
    summary["experiment_registry_completion_row_hash"] = validation.get(
        "experiment_registry_completion_row_hash"
    )
    summary["final_holdout_fingerprint"] = validation.get("final_holdout_fingerprint")
    summary["final_holdout_identity_hash"] = validation.get(
        "final_holdout_identity_hash"
    )
    summary["final_holdout_content_hash"] = validation.get("final_holdout_content_hash")
    summary["final_holdout_reuse_key_hash"] = validation.get(
        "final_holdout_reuse_key_hash"
    )
    summary["experiment_registry_bound_evidence_hash"] = validation.get(
        "experiment_registry_bound_evidence_hash"
    )
    summary["experiment_registry_evidence_hash_phase"] = validation.get(
        "experiment_registry_evidence_hash_phase"
    )
    summary["computed_attempt_index"] = validation.get("computed_attempt_index")
    summary["computed_holdout_reuse_count"] = validation.get(
        "computed_holdout_reuse_count"
    )
    summary["declared_attempt_index"] = validation.get("declared_attempt_index")
    summary["declared_holdout_reuse_count"] = validation.get(
        "declared_holdout_reuse_count"
    )
    summary["research_freedom_hash"] = validation.get("research_freedom_hash")
    summary["white_reality_check_p_value"] = validation.get(
        "white_reality_check_p_value"
    )
    summary["summary_metric_max_bootstrap_p_value"] = validation.get(
        "summary_metric_max_bootstrap_p_value"
    )
    summary["statistical_gate_result"] = validation.get("statistical_gate_result")
    summary["statistical_gate_fail_reasons"] = (
        validation.get("statistical_gate_fail_reasons") or []
    )
    summary["stress_suite_contract_hash"] = validation.get("stress_suite_contract_hash")
    raw_validation_stress = validation.get("validation_stress_suite")
    validation_stress = (
        raw_validation_stress if isinstance(raw_validation_stress, dict) else {}
    )
    raw_final_stress = validation.get("final_holdout_stress_suite")
    final_stress = raw_final_stress if isinstance(raw_final_stress, dict) else {}
    summary["validation_stress_suite_hash"] = validation_stress.get("stress_suite_hash")
    summary["final_holdout_stress_suite_hash"] = final_stress.get("stress_suite_hash")
    summary["selection_universe_hash"] = lineage.get("selection_universe_hash")
    summary["candidate_metric_values_hash"] = lineage.get(
        "candidate_metric_values_hash"
    )
    summary["final_selection_contract_hash"] = lineage.get(
        "final_selection_contract_hash"
    ) or validation.get("final_selection_contract_hash")
    summary["selected_candidate_id"] = lineage.get(
        "selected_candidate_id"
    ) or validation.get("selected_candidate_id")
    summary["selected_candidate_score_hash"] = lineage.get(
        "selected_candidate_score_hash"
    ) or validation.get("selected_candidate_score_hash")
    summary["candidate_final_scores_hash"] = lineage.get(
        "candidate_final_scores_hash"
    ) or validation.get("candidate_final_scores_hash")

    _compare(
        summary,
        "manifest_hash",
        validation.get("manifest_hash"),
        lineage.get("manifest_hash"),
        "manifest_hash_mismatch",
    )
    _compare(
        summary,
        "dataset_content_hash",
        validation.get("dataset_content_hash"),
        lineage.get("dataset_content_hash"),
        "dataset_content_hash_mismatch",
    )
    if validation.get("dataset_quality_hash") or lineage.get("dataset_quality_hash"):
        _compare(
            summary,
            "dataset_quality_hash",
            validation.get("dataset_quality_hash"),
            lineage.get("dataset_quality_hash"),
            "dataset_quality_hash_mismatch",
        )
    if validation.get("dataset_adapter_provenance_hash") or lineage.get(
        "dataset_adapter_provenance_hash"
    ):
        _compare(
            summary,
            "dataset_adapter_provenance_hash",
            validation.get("dataset_adapter_provenance_hash"),
            lineage.get("dataset_adapter_provenance_hash"),
            "dataset_adapter_provenance_hash_mismatch",
        )
    _compare(
        summary,
        "candidate_profile_hash",
        validation.get("candidate_profile_hash"),
        lineage.get("candidate_profile_hash"),
        "candidate_hash_mismatch",
    )
    _verify_artifact_hash(summary, lineage, "backtest_report", required=True)
    validation_required = requires_candidate_validation(
        validation.get("research_classification")
    )
    _verify_policy_hash_binding(
        summary, validation, lineage, required=validation_required
    )
    _verify_backtest_audit_trail_binding(summary, lineage, active_manager)
    final_selection_required = bool(
        validation.get("final_selection_required")
    ) or requires_candidate_validation(validation.get("research_classification"))
    if final_selection_required:
        for field, reason in (
            ("final_selection_contract_hash", "final_selection_contract_hash_missing"),
            ("selected_candidate_score_hash", "final_selection_score_hash_missing"),
            ("candidate_final_scores_hash", "final_selection_score_hash_missing"),
        ):
            if not str(validation.get(field) or "").startswith("sha256:"):
                summary["mismatches"].append(
                    _mismatch(
                        f"validation.{field}",
                        "sha256:<required>",
                        validation.get(field),
                        reason,
                    )
                )
        if validation.get("final_selection_gate_result") != "PASS":
            summary["mismatches"].append(
                _mismatch(
                    "validation.final_selection_gate_result",
                    "PASS",
                    validation.get("final_selection_gate_result"),
                    "final_selection_gate_not_passed",
                )
            )
        if validation.get("candidate_id") != validation.get("selected_candidate_id"):
            summary["mismatches"].append(
                _mismatch(
                    "validation.selected_candidate_id",
                    validation.get("candidate_id"),
                    validation.get("selected_candidate_id"),
                    "candidate_not_selected_by_final_selection_contract",
                )
            )
        for field, reason in (
            ("final_selection_contract_hash", "final_selection_contract_hash_mismatch"),
            ("selected_candidate_score_hash", "final_selection_score_hash_mismatch"),
            ("candidate_final_scores_hash", "final_selection_score_hash_mismatch"),
            ("selected_candidate_id", "final_selection_selected_candidate_mismatch"),
        ):
            _compare(
                summary,
                f"lineage.{field}",
                validation.get(field),
                lineage.get(field),
                reason,
            )
    statistical_required = bool(
        validation.get("statistical_validation_required")
    ) or requires_candidate_validation(validation.get("research_classification"))
    if statistical_required:
        _compare(
            summary,
            "statistical_evidence_hash",
            validation.get("statistical_evidence_hash"),
            lineage.get("statistical_evidence_hash"),
            "statistical_evidence_hash_mismatch",
        )
        _compare(
            summary,
            "selection_universe_hash",
            validation.get("selection_universe_hash"),
            lineage.get("selection_universe_hash"),
            "selection_universe_hash_mismatch",
        )
        _compare(
            summary,
            "candidate_metric_values_hash",
            validation.get("candidate_metric_values_hash"),
            lineage.get("candidate_metric_values_hash"),
            "candidate_metric_values_hash_mismatch",
        )
        _compare(
            summary,
            "return_panel_hash",
            validation.get("return_panel_hash"),
            lineage.get("return_panel_hash"),
            "return_panel_hash_mismatch",
        )
    _verify_artifact_hash(
        summary,
        lineage,
        "statistical_evidence",
        required=statistical_required,
        missing_reason="statistical_evidence_missing",
    )
    if statistical_required:
        _verify_statistical_evidence_bindings(summary, validation, lineage)
        _verify_artifact_hash(
            summary,
            lineage,
            "return_panel",
            required=True,
            missing_reason="return_panel_missing",
        )
    stress_required = bool(
        validation.get("stress_suite_required")
    ) or requires_candidate_validation(validation.get("research_classification"))
    if stress_required:
        _verify_stress_suite_bindings(summary, validation, lineage)
    if final_selection_required:
        _verify_final_selection_bindings(summary, validation, lineage)
    walk_required = bool(validation.get("walk_forward_required"))
    _verify_artifact_hash(
        summary,
        lineage,
        "walk_forward_report",
        required=walk_required,
        missing_reason="walk_forward_required_but_missing",
    )
    calibration_required = bool(validation.get("execution_calibration_required"))
    validation_calibration_hash = str(
        validation.get("execution_calibration_artifact_hash") or ""
    ).strip()
    lineage_calibration_hash = str(
        lineage.get("execution_calibration_artifact_hash") or ""
    ).strip()
    if calibration_required and not validation_calibration_hash:
        summary["mismatches"].append(
            _mismatch(
                "execution_calibration_artifact_hash",
                "sha256:<required>",
                validation_calibration_hash or None,
                "calibration_hash_missing",
            )
        )
    if calibration_required and not lineage_calibration_hash:
        summary["mismatches"].append(
            _mismatch(
                "lineage.execution_calibration_artifact_hash",
                validation_calibration_hash or "sha256:<required>",
                lineage_calibration_hash or None,
                "calibration_hash_missing",
            )
        )
    if validation_calibration_hash or lineage_calibration_hash:
        _compare(
            summary,
            "execution_calibration_artifact_hash",
            validation_calibration_hash,
            lineage_calibration_hash,
            "calibration_hash_mismatch",
        )
    if validation.get("command_args_hash_expected") and validation.get(
        "command_args_hash_expected"
    ) != lineage.get("command_args_hash"):
        _compare(
            summary,
            "command_args_hash",
            validation.get("command_args_hash_expected"),
            lineage.get("command_args_hash"),
            "command_args_hash_mismatch",
        )

    if summary["mismatches"]:
        summary["reason"] = str(summary["mismatches"][0]["reason"])
    elif summary["missing_artifacts"]:
        summary["reason"] = str(summary["missing_artifacts"][0]["reason"])
    else:
        summary["ok"] = True
        summary["reason"] = "ok"
    return ReproducibilityResult(summary)


def _verify_backtest_audit_trail_binding(
    summary: dict[str, Any],
    lineage: dict[str, Any],
    manager: ResearchPathManager,
) -> None:
    report = _load_optional_artifact(lineage.get("backtest_report_path"))
    if not isinstance(report, dict):
        return
    summary["audit_trail_status"] = report.get("audit_trail_status")
    reasons = validate_audit_trail_binding(report=report, manager=manager)
    summary["audit_trail_fail_reasons"] = sorted(set(str(item) for item in reasons))
    for reason in summary["audit_trail_fail_reasons"]:
        summary["mismatches"].append(
            _mismatch("backtest_report.audit_trail", "valid_binding", reason, reason)
        )


def _verify_policy_hash_binding(
    summary: dict[str, Any],
    validation: dict[str, Any],
    lineage: dict[str, Any],
    *,
    required: bool,
) -> None:
    for field, reason in (
        ("portfolio_policy_hash", "portfolio_policy_hash_mismatch"),
        ("simulation_policy_hash", "simulation_policy_hash_mismatch"),
    ):
        validation_value = validation.get(field)
        lineage_value = lineage.get(field)
        if required and not str(validation_value or "").startswith("sha256:"):
            summary["mismatches"].append(
                _mismatch(
                    field, "sha256:<required>", validation_value, f"{field}_missing"
                )
            )
        if required and not str(lineage_value or "").startswith("sha256:"):
            summary["mismatches"].append(
                _mismatch(
                    f"lineage.{field}",
                    "sha256:<required>",
                    lineage_value,
                    f"{field}_missing",
                )
            )
        if validation_value or lineage_value:
            _compare(summary, field, validation_value, lineage_value, reason)
    report = _load_optional_artifact(lineage.get("backtest_report_path"))
    if not isinstance(report, dict):
        return
    for field, reason in (
        ("portfolio_policy_hash", "portfolio_policy_hash_mismatch"),
        ("simulation_policy_hash", "simulation_policy_hash_mismatch"),
    ):
        report_value = report.get(field)
        if required and not str(report_value or "").startswith("sha256:"):
            summary["mismatches"].append(
                _mismatch(
                    f"backtest_report.{field}",
                    "sha256:<required>",
                    report_value,
                    f"{field}_missing",
                )
            )
        if validation.get(field) or report_value:
            _compare(
                summary,
                f"backtest_report.{field}",
                validation.get(field),
                report_value,
                reason,
            )
    candidates = report.get("candidates")
    if isinstance(candidates, list):
        candidate = next(
            (
                item
                for item in candidates
                if item.get("parameter_candidate_id") == validation.get("candidate_id")
            ),
            None,
        )
        if isinstance(candidate, dict):
            for field, reason in (
                ("portfolio_policy_hash", "portfolio_policy_hash_mismatch"),
                ("simulation_policy_hash", "simulation_policy_hash_mismatch"),
            ):
                if validation.get(field) or candidate.get(field):
                    _compare(
                        summary,
                        f"backtest_report.candidate.{field}",
                        validation.get(field),
                        candidate.get(field),
                        reason,
                    )


def _verify_artifact_hash(
    summary: dict[str, Any],
    lineage: dict[str, Any],
    stem: str,
    *,
    required: bool,
    missing_reason: str | None = None,
) -> None:
    path_value = str(lineage.get(f"{stem}_path") or "").strip()
    expected = str(lineage.get(f"{stem}_hash") or "").strip()
    if not path_value or not expected:
        if required:
            summary["missing_artifacts"].append(
                {
                    "field": stem,
                    "path": path_value or None,
                    "reason": missing_reason or f"{stem}_missing",
                }
            )
        return
    path = Path(path_value).expanduser()
    if not path.exists():
        summary["missing_artifacts"].append(
            {
                "field": stem,
                "path": str(path),
                "reason": missing_reason or f"{stem}_missing",
            }
        )
        return
    try:
        payload = _load_object(path)
    except ValueError as exc:
        summary["mismatches"].append(
            {"field": stem, "reason": str(exc), "path": str(path)}
        )
        return
    if stem in {"backtest_report", "walk_forward_report"}:
        actual = sha256_prefixed(report_content_hash_payload(payload))
    else:
        actual = sha256_prefixed(
            content_hash_payload(
                {k: v for k, v in payload.items() if k != "content_hash"}
            )
        )
    embedded = str(payload.get("content_hash") or "").strip()
    if actual != expected:
        reason = f"{stem}_hash_mismatch"
        summary["mismatches"].append(
            _mismatch(f"{stem}_hash", expected, actual, reason)
        )
    elif embedded != actual:
        summary["mismatches"].append(
            _mismatch(
                f"{stem}_embedded_content_hash",
                actual,
                embedded or None,
                f"{stem}_embedded_content_hash_mismatch",
            )
        )


def _verify_statistical_evidence_bindings(
    summary: dict[str, Any],
    validation: dict[str, Any],
    lineage: dict[str, Any],
) -> None:
    path_value = str(lineage.get("statistical_evidence_path") or "").strip()
    if not path_value:
        return
    path = Path(path_value).expanduser()
    if not path.exists():
        return
    try:
        payload = _load_object(path)
    except ValueError:
        return
    report = _load_optional_artifact(lineage.get("backtest_report_path"))
    _compare(
        summary,
        "statistical_evidence.selection_universe_hash",
        validation.get("selection_universe_hash"),
        payload.get("selection_universe_hash"),
        "selection_universe_hash_mismatch",
    )
    _compare(
        summary,
        "statistical_evidence.candidate_metric_values_hash",
        validation.get("candidate_metric_values_hash"),
        payload.get("candidate_metric_values_hash"),
        "candidate_metric_values_hash_mismatch",
    )
    _compare(
        summary,
        "statistical_evidence.return_panel_hash",
        validation.get("return_panel_hash"),
        payload.get("return_panel_hash"),
        "return_panel_hash_mismatch",
    )
    if payload.get("candidate_metric_values_hash") != lineage.get(
        "candidate_metric_values_hash"
    ):
        summary["mismatches"].append(
            _mismatch(
                "lineage.candidate_metric_values_hash",
                lineage.get("candidate_metric_values_hash"),
                payload.get("candidate_metric_values_hash"),
                "candidate_metric_values_hash_mismatch",
            )
        )
    if isinstance(report, dict):
        _verify_statistical_report_bindings(
            summary, validation, lineage, payload, report
        )
        panel = _load_optional_artifact(lineage.get("return_panel_path"))
        for reason in validate_return_panel_binding(
            report=report, evidence=payload, panel=panel
        ):
            summary["mismatches"].append(
                _mismatch("return_panel", "valid_binding", reason, reason)
            )
        for reason in validate_family_registry_binding(report=report, evidence=payload):
            summary["mismatches"].append(
                _mismatch("family_trial_registry", "valid_binding", reason, reason)
            )
        for reason in validate_experiment_registry_binding(
            report=report,
            evidence=payload,
            validation=validation,
            require_complete=requires_candidate_validation(
                validation.get("research_classification")
            ),
        ):
            summary["mismatches"].append(
                _mismatch("experiment_registry", "valid_binding", reason, reason)
            )


def _verify_stress_suite_bindings(
    summary: dict[str, Any],
    validation: dict[str, Any],
    lineage: dict[str, Any],
) -> None:
    contract = validation.get("stress_suite_contract")
    contract_hash = str(validation.get("stress_suite_contract_hash") or "").strip()
    if not isinstance(contract, dict):
        summary["mismatches"].append(
            _mismatch(
                "validation.stress_suite_contract",
                "object",
                type(contract).__name__,
                "stress_suite_contract_mismatch",
            )
        )
        return
    actual_contract_hash = sha256_prefixed(contract)
    if actual_contract_hash != contract_hash:
        summary["mismatches"].append(
            _mismatch(
                "validation.stress_suite_contract_hash",
                actual_contract_hash,
                contract_hash,
                "stress_suite_contract_mismatch",
            )
        )
    final_required = (
        validation.get("final_holdout_present") is True
        or validation.get("final_holdout_required_for_validation") is True
    )
    fields = (
        ("validation_stress_suite", True),
        ("final_holdout_stress_suite", final_required),
    )
    for field, required in fields:
        evidence = validation.get(field)
        if evidence is None and not required:
            continue
        if not isinstance(evidence, dict):
            summary["mismatches"].append(
                _mismatch(
                    f"validation.{field}",
                    "object",
                    type(evidence).__name__,
                    (
                        "final_holdout_stress_suite_required_but_missing"
                        if field == "final_holdout_stress_suite"
                        else "stress_suite_required_but_missing"
                    ),
                )
            )
            continue
        embedded = str(evidence.get("stress_suite_hash") or "")
        if not embedded.startswith("sha256:"):
            summary["mismatches"].append(
                _mismatch(
                    f"validation.{field}.stress_suite_hash",
                    "sha256:<required>",
                    embedded or None,
                    (
                        "final_holdout_stress_suite_hash_missing"
                        if field == "final_holdout_stress_suite"
                        else "stress_suite_hash_missing"
                    ),
                )
            )
        else:
            actual = sha256_prefixed(
                content_hash_payload(
                    {k: v for k, v in evidence.items() if k != "stress_suite_hash"}
                )
            )
            if embedded != actual:
                summary["mismatches"].append(
                    _mismatch(
                        f"validation.{field}.stress_suite_hash",
                        actual,
                        embedded,
                        (
                            "final_holdout_stress_suite_hash_mismatch"
                            if field == "final_holdout_stress_suite"
                            else "stress_suite_hash_mismatch"
                        ),
                    )
                )
        if evidence.get("contract_hash") != contract_hash:
            summary["mismatches"].append(
                _mismatch(
                    f"validation.{field}.contract_hash",
                    contract_hash,
                    evidence.get("contract_hash"),
                    "stress_suite_contract_mismatch",
                )
            )
        if evidence.get("gate_result") != "PASS":
            summary["mismatches"].append(
                _mismatch(
                    f"validation.{field}.gate_result",
                    "PASS",
                    evidence.get("gate_result"),
                    (
                        "final_holdout_stress_suite_gate_not_passed"
                        if field == "final_holdout_stress_suite"
                        else "stress_suite_gate_not_passed"
                    ),
                )
            )
    report = _load_optional_artifact(lineage.get("backtest_report_path"))
    if not isinstance(report, dict):
        return
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        return
    candidate = next(
        (
            item
            for item in candidates
            if item.get("parameter_candidate_id") == validation.get("candidate_id")
        ),
        None,
    )
    if not isinstance(candidate, dict):
        summary["mismatches"].append(
            _mismatch(
                "backtest_report.candidate",
                validation.get("candidate_id"),
                None,
                "stress_suite_evidence_malformed",
            )
        )
        return
    _compare(
        summary,
        "backtest_report.stress_suite_contract_hash",
        validation.get("stress_suite_contract_hash"),
        candidate.get("stress_suite_contract_hash"),
        "stress_suite_contract_mismatch",
    )
    if (
        isinstance(candidate.get("stress_suite_contract"), dict)
        and candidate.get("stress_suite_contract") != contract
    ):
        summary["mismatches"].append(
            _mismatch(
                "backtest_report.stress_suite_contract",
                contract_hash,
                candidate.get("stress_suite_contract_hash"),
                "stress_suite_contract_mismatch",
            )
        )
    for field, required in fields:
        validated = validation.get(field)
        reported = candidate.get(field)
        if required and not isinstance(reported, dict):
            summary["mismatches"].append(
                _mismatch(
                    f"backtest_report.{field}",
                    "object",
                    type(reported).__name__,
                    (
                        "final_holdout_stress_suite_required_but_missing"
                        if field == "final_holdout_stress_suite"
                        else "stress_suite_required_but_missing"
                    ),
                )
            )
        if isinstance(validated, dict) and isinstance(reported, dict):
            _compare(
                summary,
                f"backtest_report.{field}.stress_suite_hash",
                validated.get("stress_suite_hash"),
                reported.get("stress_suite_hash"),
                (
                    "final_holdout_stress_suite_hash_mismatch"
                    if field == "final_holdout_stress_suite"
                    else "stress_suite_hash_mismatch"
                ),
            )


def _verify_final_selection_bindings(
    summary: dict[str, Any],
    validation: dict[str, Any],
    lineage: dict[str, Any],
) -> None:
    report = _load_optional_artifact(lineage.get("backtest_report_path"))
    if not isinstance(report, dict):
        summary["mismatches"].append(
            _mismatch(
                "backtest_report.final_selection",
                "present",
                None,
                "final_selection_contract_missing",
            )
        )
        return
    for reason in validate_final_selection_report(report):
        summary["mismatches"].append(
            _mismatch("backtest_report.final_selection", "valid", reason, reason)
        )
    for field, reason in (
        ("final_selection_contract_hash", "final_selection_contract_hash_mismatch"),
        ("selected_candidate_score_hash", "final_selection_score_hash_mismatch"),
        ("candidate_final_scores_hash", "final_selection_score_hash_mismatch"),
        ("selected_candidate_id", "final_selection_selected_candidate_mismatch"),
    ):
        if lineage.get(field) != report.get(field):
            summary["mismatches"].append(
                _mismatch(
                    f"lineage.{field}", lineage.get(field), report.get(field), reason
                )
            )
        if validation.get(field) != report.get(field):
            summary["mismatches"].append(
                _mismatch(
                    f"backtest_report.{field}",
                    validation.get(field),
                    report.get(field),
                    reason,
                )
            )
    if validation.get("candidate_id") != report.get("selected_candidate_id"):
        summary["mismatches"].append(
            _mismatch(
                "backtest_report.selected_candidate_id",
                validation.get("candidate_id"),
                report.get("selected_candidate_id"),
                "candidate_not_selected_by_final_selection_contract",
            )
        )


def _verify_statistical_report_bindings(
    summary: dict[str, Any],
    validation: dict[str, Any],
    lineage: dict[str, Any],
    evidence: dict[str, Any],
    report: dict[str, Any],
) -> None:
    candidates = report.get("candidates")
    if not isinstance(candidates, list) or not all(
        isinstance(item, dict) for item in candidates
    ):
        summary["mismatches"].append(
            _mismatch(
                "backtest_report.candidates",
                "list",
                type(candidates).__name__,
                "candidate_metric_values_hash_recompute_mismatch",
            )
        )
        return
    candidate_count = len(candidates)
    for field, value in (
        ("backtest_report.candidate_count", report.get("candidate_count")),
        ("statistical_evidence.candidate_count", evidence.get("candidate_count")),
    ):
        if _as_int(value) != candidate_count:
            summary["mismatches"].append(
                _mismatch(
                    field,
                    candidate_count,
                    value,
                    "statistical_candidate_count_mismatch",
                )
            )
    evidence_summary = evidence.get("candidate_metric_values_summary")
    if not isinstance(evidence_summary, dict):
        summary["mismatches"].append(
            _mismatch(
                "statistical_evidence.candidate_metric_values_summary",
                "object",
                type(evidence_summary).__name__,
                "statistical_metadata_mismatch",
            )
        )
    else:
        for field, expected in (
            ("candidate_count", candidate_count),
            ("metric_value_count", evidence.get("metric_value_count")),
            ("missing_metric_count", evidence.get("missing_metric_count")),
        ):
            if _as_int(evidence_summary.get(field)) != _as_int(expected):
                summary["mismatches"].append(
                    _mismatch(
                        f"statistical_evidence.candidate_metric_values_summary.{field}",
                        expected,
                        evidence_summary.get(field),
                        "statistical_metadata_mismatch",
                    )
                )
    recomputed = recompute_candidate_metric_values_hash_from_report(
        report=report, evidence=evidence
    )
    if recomputed is None:
        summary["mismatches"].append(
            _mismatch(
                "candidate_metric_values_hash",
                "sha256:<recomputed>",
                None,
                "candidate_metric_values_hash_recompute_mismatch",
            )
        )
        return
    for field, value in (
        (
            "statistical_evidence.candidate_metric_values_hash",
            evidence.get("candidate_metric_values_hash"),
        ),
        (
            "validation.candidate_metric_values_hash",
            validation.get("candidate_metric_values_hash"),
        ),
        (
            "lineage.candidate_metric_values_hash",
            lineage.get("candidate_metric_values_hash"),
        ),
        (
            "backtest_report.candidate_metric_values_hash",
            report.get("candidate_metric_values_hash"),
        ),
    ):
        if str(value or "").strip() != recomputed:
            summary["mismatches"].append(
                _mismatch(
                    field,
                    recomputed,
                    value,
                    "candidate_metric_values_hash_recompute_mismatch",
                )
            )
    for field, value in (
        ("statistical_evidence.return_panel_hash", evidence.get("return_panel_hash")),
        ("validation.return_panel_hash", validation.get("return_panel_hash")),
        ("lineage.return_panel_hash", lineage.get("return_panel_hash")),
        ("backtest_report.return_panel_hash", report.get("return_panel_hash")),
    ):
        expected = str(evidence.get("return_panel_hash") or "").strip()
        if str(value or "").strip() != expected:
            summary["mismatches"].append(
                _mismatch(field, expected, value, "return_panel_hash_mismatch")
            )


def _load_optional_artifact(path_value: object) -> dict[str, Any] | None:
    text = str(path_value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.exists():
        return None
    try:
        return _load_object(path)
    except ValueError:
        return None


def _compare(
    summary: dict[str, Any], field: str, expected: object, actual: object, reason: str
) -> None:
    if str(expected or "").strip() != str(actual or "").strip():
        summary["mismatches"].append(_mismatch(field, expected, actual, reason))


def _mismatch(
    field: str, expected: object, actual: object, reason: str
) -> dict[str, object]:
    return {"field": field, "expected": expected, "actual": actual, "reason": reason}


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    try:
        numeric = cast(str | bytes | bytearray | SupportsInt | SupportsIndex, value)
        return int(numeric)
    except (TypeError, ValueError):
        return None


def _load_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid_json: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload_not_object")
    return payload


def _redacted_mapping(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in sorted(values.items()):
        lowered = str(key).lower()
        if any(fragment in lowered for fragment in SECRET_KEY_FRAGMENTS):
            out[str(key)] = (
                "<redacted-present>" if str(value or "") else "<redacted-empty>"
            )
        else:
            out[str(key)] = value
    return out
