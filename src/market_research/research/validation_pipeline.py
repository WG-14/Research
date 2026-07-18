"""Research-only validation summary writer.

Validation aggregates research evidence. It intentionally has no declaration,
profile, replay, or account-execution stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from market_research.paths import ResearchPathManager
from market_research.storage_io import write_json_atomic

from .experiment_manifest import ExperimentManifest
from .experiment_registry import (
    experiment_registry_path,
    research_identity_from_manifest,
    validate_experiment_registry_binding,
)
from .final_selection import (
    validate_confirmation_artifact,
    validate_selection_artifact_binding,
)
from .hashing import report_content_hash_payload, sha256_prefixed
from .hypothesis_contract import (
    HYPOTHESIS_LINEAGE_SCHEMA_VERSION,
    parse_hypothesis_spec,
    validate_hypothesis_lineage_target,
)
from .knowledge_registry import (
    KnowledgeRegistryError,
    freeze_validation_admission,
    validation_admission_binding_reasons,
)
from .validation_protocol import (
    run_final_holdout_confirmation,
    run_research_backtest,
    run_research_walk_forward,
)
from .strategy_registry import StrategyRegistry
from .research_decision_report import build_research_decision_report
from .research_classification import requires_candidate_validation


class ValidationRunError(ValueError):
    pass


VALIDATION_STAGE_ORDER = (
    "readiness",
    "dataset_quality",
    "backtest",
    "final_holdout",
    "stress_suite",
    "statistical_validation",
    "walk_forward",
    "final_selection",
    "research_candidate_report",
)


def validate_validated_research_result(
    report: object,
    *,
    manager: ResearchPathManager | None = None,
) -> list[str]:
    """Validate the terminal schema-3 result used for approval and packaging."""

    if not isinstance(report, dict):
        return ["validated_research_result_must_be_object"]
    reasons: list[str] = []
    if (
        report.get("schema_version") != 3
        or report.get("artifact_type") != "validated_research_result"
    ):
        reasons.append("validated_research_result_contract_invalid")
    try:
        validation_bound = requires_candidate_validation(
            report.get("research_classification")
        )
    except ValueError:
        validation_bound = False
    if not validation_bound:
        reasons.append("validated_research_result_classification_invalid")
    reasons.extend(_validated_hypothesis_lineage_reasons(report))
    # A terminal schema-3 result always requires schema-2 hypothesis lineage,
    # so its pre-validation admission is part of the same non-optional
    # authority contract.  Treating an absent marker as legacy would permit a
    # caller to strip every admission field, recompute the ordinary report
    # content hash, and bypass canonical preregistration verification.  Legacy
    # artifacts remain available through the explicit read-only migration
    # paths; they are not promotable validated results.
    if report.get("validation_admission_binding_schema_version") != 1:
        reasons.append("validation_admission_binding_schema_invalid")
    reasons.extend(validation_admission_binding_reasons(report, manager=manager))
    if report.get("end_to_end_validation_result") != "PASS":
        reasons.append("validated_research_result_terminal_gate_not_passed")
    blocking = report.get("validation_blocking_reasons")
    if not isinstance(blocking, list) or blocking:
        reasons.append("validated_research_result_blocking_reasons_present")

    stages = report.get("validation_stages")
    if not isinstance(stages, list) or not all(
        isinstance(stage, dict) for stage in stages
    ):
        reasons.append("validated_research_result_stages_invalid")
        stage_status: dict[str, str] = {}
    else:
        names = [str(stage.get("name") or "") for stage in stages]
        if names != list(VALIDATION_STAGE_ORDER) or len(set(names)) != len(names):
            reasons.append("validated_research_result_stages_invalid")
        stage_status = {
            str(stage.get("name") or ""): str(stage.get("status") or "")
            for stage in stages
        }
    for name in (
        "readiness",
        "dataset_quality",
        "final_holdout",
        "stress_suite",
        "statistical_validation",
        "final_selection",
        "research_candidate_report",
    ):
        if stage_status.get(name) != "PASS":
            reasons.append(f"validated_research_result_stage_not_passed:{name}")
    backtest = stage_status.get("backtest")
    walk_forward = stage_status.get("walk_forward")
    if (backtest, walk_forward) not in {
        ("PASS", "NOT_REQUIRED"),
        ("NOT_RUN", "PASS"),
    }:
        reasons.append("validated_research_result_execution_stage_invalid")
    for field in (
        "dataset_quality_gate_status",
        "stress_suite_gate_result",
        "statistical_gate_result",
        "final_selection_gate_result",
        "validation_eligibility_gate_result",
        "gate_result",
    ):
        if report.get(field) != "PASS":
            reasons.append(f"validated_research_result_gate_not_passed:{field}")
    confirmation = report.get("final_holdout_confirmation")
    if (
        not isinstance(confirmation, dict)
        or confirmation.get("confirmation_gate_result") != "PASS"
    ):
        reasons.append("validated_research_result_confirmation_not_passed")
    selected_id = str(report.get("selected_candidate_id") or "")
    selected = report.get("selected_candidate")
    if (
        not selected_id
        or not isinstance(selected, dict)
        or str(
            selected.get("parameter_candidate_id") or selected.get("candidate_id") or ""
        )
        != selected_id
    ):
        reasons.append("validated_research_result_selected_candidate_mismatch")
    return sorted(set(reasons))


def _validated_hypothesis_lineage_reasons(report: dict[str, Any]) -> list[str]:
    try:
        spec = parse_hypothesis_spec(report.get("hypothesis_spec"))
    except (TypeError, ValueError):
        return ["validated_research_result_hypothesis_lineage_invalid"]
    reasons: list[str] = []
    if spec.schema_version != HYPOTHESIS_LINEAGE_SCHEMA_VERSION:
        reasons.append("validated_research_result_hypothesis_lineage_required")
        return reasons
    try:
        validate_hypothesis_lineage_target(
            spec,
            market=str(report.get("market") or ""),
            interval=str(report.get("interval") or ""),
        )
    except ValueError:
        reasons.append("validated_research_result_hypothesis_lineage_target_mismatch")
    if spec.contract_hash() != report.get("hypothesis_contract_hash"):
        reasons.append("validated_research_result_hypothesis_contract_hash_mismatch")
    if spec.lineage_hash() != report.get("hypothesis_lineage_hash"):
        reasons.append("validated_research_result_hypothesis_lineage_hash_mismatch")
    question_ref = spec.research_question_ref
    if question_ref is None or (
        report.get("research_question_id") != question_ref.question_id
        or report.get("research_question_version") != question_ref.version
        or report.get("research_question_hash") != question_ref.question_hash
    ):
        reasons.append("validated_research_result_research_question_ref_mismatch")
    if report.get("observation_hashes") != [
        item.observation_hash for item in spec.observation_refs
    ]:
        reasons.append("validated_research_result_observation_hashes_mismatch")
    if (
        report.get("hypothesis_id") != spec.hypothesis_id
        or report.get("hypothesis_version") != spec.version
    ):
        reasons.append("validated_research_result_hypothesis_identity_mismatch")
    return reasons


def validation_next_action_payload(reasons: Any) -> dict[str, str]:
    del reasons
    return {
        "next_required_action": "inspect_research_validation_summary",
        "recommended_command": "research-validate",
    }


def aggregate_validation_gates(
    *,
    manifest: ExperimentManifest,
    selection_report: dict[str, Any],
    selection_artifact: dict[str, Any] | None,
    selected_candidate: dict[str, Any] | None,
    final_holdout_confirmation: dict[str, Any] | None,
    manager: ResearchPathManager | None = None,
) -> tuple[str, dict[str, str], list[str]]:
    """Derive the terminal result from the authoritative stage evidence."""
    walk_forward_required = bool(manifest.acceptance_gate.walk_forward_required)
    stress_required = bool(
        manifest.stress_suite and manifest.stress_suite.required_for_validation
    )
    statistical_required = bool(
        manifest.statistical_validation
        and manifest.statistical_validation.required_for_validation
    )
    final_selection_required = bool(
        manifest.final_selection and manifest.final_selection.required_for_validation
    )
    final_holdout_required = bool(
        manifest.acceptance_gate.final_holdout_required_for_validation
    )

    reasons: list[str] = []
    artifact_reasons = (
        validate_selection_artifact_binding(
            report=selection_report,
            selection_artifact=selection_artifact,
            selected_candidate=selected_candidate,
        )
        if isinstance(selection_artifact, dict)
        else ["selection_artifact_missing"]
    )
    reasons.extend(artifact_reasons)

    eligibility = str(selection_report.get("validation_eligibility_gate_result") or "")
    if eligibility != "PASS":
        blocking = [
            str(item)
            for item in selection_report.get("validation_blocking_reasons") or []
        ]
        reasons.extend(blocking or ["validation_eligibility_gate_not_passed"])

    dataset_quality = str(selection_report.get("dataset_quality_gate_status") or "")
    stress = str(selection_report.get("stress_suite_gate_result") or "")
    statistical = str(selection_report.get("statistical_gate_result") or "")
    walk_forward = str(selection_report.get("walk_forward_gate_result") or "")
    final_selection = str(selection_report.get("final_selection_gate_result") or "")

    if dataset_quality == "FAIL":
        reasons.extend(
            str(item)
            for item in selection_report.get("dataset_quality_gate_reasons") or []
        )
    if stress_required and stress != "PASS":
        reasons.extend(
            str(item)
            for item in selection_report.get("stress_suite_fail_reasons") or []
        )
        reasons.append("stress_suite_gate_not_passed")
    if statistical_required and statistical != "PASS":
        reasons.extend(
            str(item)
            for item in selection_report.get("statistical_gate_fail_reasons") or []
        )
        reasons.append("statistical_gate_not_passed")
    if walk_forward_required and walk_forward != "PASS":
        reasons.append("walk_forward_gate_not_passed")
    if final_selection_required and final_selection != "PASS":
        reasons.extend(
            str(item)
            for item in selection_report.get("final_selection_fail_reasons") or []
        )
        reasons.append("final_selection_gate_not_passed")
    if selected_candidate is None:
        reasons.append("selected_candidate_missing")

    if final_holdout_required:
        if manifest.dataset.split.final_holdout is None:
            reasons.append("final_holdout_required_but_missing")
            final_holdout_status = "INSUFFICIENT_EVIDENCE"
        elif not isinstance(final_holdout_confirmation, dict):
            reasons.append("final_holdout_confirmation_missing")
            final_holdout_status = "INSUFFICIENT_EVIDENCE"
        else:
            confirmation_reasons = validate_confirmation_artifact(
                final_holdout_confirmation,
                selection_artifact=selection_artifact or {},
            )
            confirmation_reasons.extend(
                validate_experiment_registry_binding(
                    report=final_holdout_confirmation,
                    require_complete=True,
                    expected_registry_path=(
                        experiment_registry_path(manager=manager)
                        if manager is not None
                        else None
                    ),
                )
            )
            if confirmation_reasons:
                reasons.extend(confirmation_reasons)
                reasons.append("final_holdout_confirmation_invalid")
                final_holdout_status = "FAIL"
            else:
                final_holdout_status = str(
                    final_holdout_confirmation.get("confirmation_gate_result") or ""
                )
            if final_holdout_status != "PASS":
                reasons.extend(
                    str(item)
                    for item in (
                        final_holdout_confirmation.get("confirmation_gate_fail_reasons")
                        or final_holdout_confirmation.get("confirmation_gate_reasons")
                        or []
                    )
                )
                reasons.append("final_holdout_confirmation_not_passed")
    else:
        final_holdout_status = (
            str(final_holdout_confirmation.get("confirmation_gate_result") or "")
            if isinstance(final_holdout_confirmation, dict)
            else "NOT_REQUIRED"
        )

    stage_status = {
        "readiness": "PASS",
        "dataset_quality": dataset_quality or "INSUFFICIENT_EVIDENCE",
        "backtest": "NOT_RUN" if walk_forward_required else "PASS",
        "final_holdout": final_holdout_status or "INSUFFICIENT_EVIDENCE",
        "stress_suite": stress
        or ("INSUFFICIENT_EVIDENCE" if stress_required else "NOT_REQUIRED"),
        "statistical_validation": (
            statistical
            or ("INSUFFICIENT_EVIDENCE" if statistical_required else "NOT_REQUIRED")
        ),
        "walk_forward": walk_forward
        or ("INSUFFICIENT_EVIDENCE" if walk_forward_required else "NOT_REQUIRED"),
        "final_selection": (
            final_selection
            or (
                "INSUFFICIENT_EVIDENCE"
                if final_selection_required or selected_candidate is None
                else "PASS"
            )
        ),
    }

    required_stage_names = ["dataset_quality", "final_selection"]
    if stress_required:
        required_stage_names.append("stress_suite")
    if statistical_required:
        required_stage_names.append("statistical_validation")
    if walk_forward_required:
        required_stage_names.append("walk_forward")
    else:
        required_stage_names.append("backtest")
    if final_holdout_required:
        required_stage_names.append("final_holdout")

    required_statuses = [stage_status[name] for name in required_stage_names]
    if (
        any(status == "FAIL" for status in required_statuses)
        or eligibility == "FAIL"
        or artifact_reasons
    ):
        result = "FAIL"
    elif any(status != "PASS" for status in required_statuses) or eligibility != "PASS":
        result = "INSUFFICIENT_EVIDENCE"
    else:
        result = "PASS"
    stage_status["research_candidate_report"] = result
    return result, stage_status, sorted(set(reasons))


def run_research_validation(
    *,
    manifest: ExperimentManifest,
    db_path: str | Path | None,
    manager: Any,
    manifest_path: str,
    mode: str = "strict",
    execution_calibration: dict[str, Any] | None = None,
    execution_calibration_path: str | None = None,
    candidate_id: str | None = None,
    out_path: str | Path | None = None,
    generated_at: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    strategy_registry: StrategyRegistry,
    run_id: str | None = None,
) -> dict[str, Any]:
    if mode != "strict":
        raise ValidationRunError("validation_run_mode_unsupported")
    validation_admission: dict[str, Any] | None = None
    if (
        manifest.hypothesis_spec is not None
        and manifest.hypothesis_spec.schema_version == HYPOTHESIS_LINEAGE_SCHEMA_VERSION
    ):
        try:
            validation_admission = freeze_validation_admission(
                manager=manager,
                manifest=manifest,
                admitted_at=generated_at,
            )
        except KnowledgeRegistryError as exc:
            raise ValidationRunError(f"validation_admission_failed:{exc}") from exc
    walk_forward_required = bool(
        getattr(manifest.acceptance_gate, "walk_forward_required", False)
    )
    selection_report = (
        run_research_walk_forward(
            manifest=manifest,
            db_path=db_path,
            manager=manager,
            execution_calibration=execution_calibration,
            manifest_path=manifest_path,
            command_args={"manifest": manifest_path, "run_id": run_id},
            generated_at=generated_at,
            progress_callback=progress_callback,
            strategy_registry=strategy_registry,
        )
        if walk_forward_required
        else run_research_backtest(
            manifest=manifest,
            db_path=db_path,
            manager=manager,
            execution_calibration=execution_calibration,
            manifest_path=manifest_path,
            command_args={"manifest": manifest_path, "run_id": run_id},
            generated_at=generated_at,
            progress_callback=progress_callback,
            strategy_registry=strategy_registry,
        )
    )
    artifact = selection_report.get("selection_artifact")
    selected_id = (
        str(artifact.get("selected_candidate_id") or "")
        if isinstance(artifact, dict)
        else ""
    )
    if candidate_id is not None and str(candidate_id) != selected_id:
        raise ValidationRunError("candidate_id_does_not_match_frozen_selection")
    candidates = [
        item
        for item in selection_report.get("candidates") or []
        if isinstance(item, dict)
    ]
    selected = next(
        (
            item
            for item in candidates
            if str(item.get("parameter_candidate_id") or "") == selected_id
        ),
        None,
    )
    confirmation = (
        run_final_holdout_confirmation(
            manifest=manifest,
            selection_report=selection_report,
            db_path=db_path,
            manager=manager,
            generated_at=generated_at,
            progress_callback=progress_callback,
            strategy_registry=strategy_registry,
        )
        if selected is not None and manifest.dataset.split.final_holdout is not None
        else None
    )
    status, stage_status, blocking_reasons = aggregate_validation_gates(
        manifest=manifest,
        selection_report=selection_report,
        selection_artifact=artifact if isinstance(artifact, dict) else None,
        selected_candidate=selected,
        final_holdout_confirmation=confirmation,
        manager=manager,
    )
    stages = [
        {"name": name, "status": stage_status.get(name, "INSUFFICIENT_EVIDENCE")}
        for name in VALIDATION_STAGE_ORDER
    ]
    reproduction_binding_material = {
        "schema_version": 1,
        "selection_artifact_hash": artifact.get("content_hash")
        if isinstance(artifact, dict)
        else None,
        "final_holdout_confirmation_hash": confirmation.get("content_hash")
        if confirmation
        else None,
    }
    if validation_admission is not None:
        reproduction_binding_material.update(
            {
                "validation_admission_record_hash": validation_admission[
                    "admission_record_hash"
                ],
                "validation_admission_row_hash": validation_admission[
                    "admission_row_hash"
                ],
            }
        )
    reproduction_binding = {
        **reproduction_binding_material,
        "content_hash": sha256_prefixed(
            reproduction_binding_material, label="selection_confirmation_reproduction"
        ),
    }
    hypothesis_identity = research_identity_from_manifest(manifest)
    # The validation summary is the canonical approval/package input.  Preserve
    # the complete authoritative selection report and extend it with terminal
    # validation and holdout evidence instead of emitting a lossy parallel
    # schema that cannot be independently verified by downstream consumers.
    summary = {
        **{
            key: value
            for key, value in selection_report.items()
            if key != "content_hash"
        },
        "schema_version": 3,
        "artifact_type": "validated_research_result",
        "experiment_id": manifest.experiment_id,
        "run_id": run_id,
        "manifest_hash": manifest.manifest_hash(),
        "hypothesis_id": hypothesis_identity["hypothesis_id"],
        "hypothesis_version": hypothesis_identity["hypothesis_version"],
        "hypothesis_contract_hash": hypothesis_identity["hypothesis_contract_hash"],
        "hypothesis_semantic_fingerprint": hypothesis_identity[
            "hypothesis_semantic_fingerprint"
        ],
        "hypothesis_lineage_hash": hypothesis_identity["hypothesis_lineage_hash"],
        "research_question_id": hypothesis_identity["research_question_id"],
        "research_question_version": hypothesis_identity["research_question_version"],
        "research_question_hash": hypothesis_identity["research_question_hash"],
        "observation_hashes": hypothesis_identity["observation_hashes"],
        "hypothesis": manifest.hypothesis,
        "hypothesis_spec": manifest.hypothesis_spec.as_dict()
        if manifest.hypothesis_spec is not None
        else None,
        "market": manifest.market,
        "instrument_evidence": manifest.instrument_evidence(),
        "interval": manifest.interval,
        "strategy_name": manifest.strategy_name,
        "strategy_version": manifest.strategy_version,
        "strategy_spec": selection_report.get("strategy_spec"),
        "strategy_spec_hash": selection_report.get("strategy_spec_hash"),
        "execution_timing_policy": selection_report.get("execution_timing_policy"),
        "execution_model": selection_report.get("execution_model"),
        "cost_assumption_contract": selection_report.get("cost_assumption_contract"),
        "data_limitations": selection_report.get("data_limitations"),
        "execution_limitations": selection_report.get("execution_limitations") or [],
        "statistical_evidence_limitations": selection_report.get(
            "statistical_evidence_limitations"
        )
        or [],
        "allowed_live_regimes": selection_report.get("allowed_live_regimes") or [],
        "blocked_live_regimes": selection_report.get("blocked_live_regimes") or [],
        "validation_stages": stages,
        "selection_report_hash": selection_report.get("content_hash"),
        "backtest_report_hash": selection_report.get("content_hash")
        if not walk_forward_required
        else None,
        "walk_forward_report_hash": selection_report.get("content_hash")
        if walk_forward_required
        else None,
        "selection_artifact_hash": artifact.get("content_hash")
        if isinstance(artifact, dict)
        else None,
        "final_holdout_confirmation_hash": confirmation.get("content_hash")
        if confirmation
        else None,
        "final_holdout_confirmation": confirmation,
        "final_selection_gate_result": selection_report.get(
            "final_selection_gate_result"
        ),
        "selected_candidate_id": selected_id or None,
        "candidates": candidates,
        "selection_artifact": artifact,
        "reproduction_binding": reproduction_binding,
        "selected_candidate": selected,
        "validation_blocking_reasons": blocking_reasons,
        "end_to_end_validation_result": status,
    }
    if validation_admission is not None:
        summary.update(
            {
                "validation_admission_binding_schema_version": 1,
                "knowledge_registry_path": validation_admission["path"],
                "validation_admission_record_hash": validation_admission[
                    "admission_record_hash"
                ],
                "validation_admission_row_hash": validation_admission[
                    "admission_row_hash"
                ],
                "validation_admission": validation_admission["admission"],
            }
        )
    decision_report = build_research_decision_report(
        manifest=manifest,
        selection_report=selection_report,
        selected_candidate=selected,
        final_holdout_confirmation=confirmation,
        validation_result=status,
        validation_stages=stages,
        blocking_reasons=blocking_reasons,
        run_id=run_id,
    )
    summary["research_candidate_report_hash"] = decision_report["content_hash"]
    report_root = manager.report_path("research", manifest.experiment_id)
    target = (
        manager.external_output_path(out_path, label="research validation output")
        if out_path
        else report_root / "validation_summary.json"
    )
    candidate_target = report_root / "research_candidate_report.json"
    selected_target = report_root / "selected_candidate.json"
    summary["validation_run_path"] = str(target.resolve())
    summary["research_candidate_report_path"] = str(candidate_target.resolve())
    summary["selected_candidate_path"] = str(selected_target.resolve())
    summary["content_hash"] = sha256_prefixed(report_content_hash_payload(summary))
    write_json_atomic(target, summary)
    write_json_atomic(candidate_target, decision_report)
    write_json_atomic(selected_target, selected or {})
    return summary
