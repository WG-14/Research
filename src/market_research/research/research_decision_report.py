"""Coherent, deterministic research decision report assembled from validated evidence."""

from __future__ import annotations

from typing import Any

from .hashing import content_hash_payload, sha256_prefixed


class ResearchDecisionReportError(ValueError):
    pass


REPORT_SECTIONS = (
    "hypothesis_and_experiment_conditions",
    "data_quality",
    "core_performance",
    "trade_analysis",
    "cost_analysis",
    "market_regime_analysis",
    "parameter_robustness",
    "out_of_sample_results",
    "failure_periods",
    "known_limitations",
    "research_conclusion",
)


def build_research_decision_report(
    *, manifest: Any, selection_report: dict[str, Any], selected_candidate: dict[str, Any] | None,
    final_holdout_confirmation: dict[str, Any] | None, validation_result: str,
    validation_stages: list[dict[str, Any]], blocking_reasons: list[str], run_id: str | None,
) -> dict[str, Any]:
    """Project scattered evidence into the fixed review contract without granting approval."""

    selected = selected_candidate or {}
    primary = _primary_scenario(selected)
    holdout = _holdout_result(final_holdout_confirmation, selected)
    report = {
        "schema_version": 1,
        "artifact_type": "research_decision_report",
        "experiment_id": manifest.experiment_id,
        "run_id": run_id,
        "manifest_hash": manifest.manifest_hash(),
        "selection_report_hash": selection_report.get("content_hash"),
        "selected_candidate_id": selected.get("parameter_candidate_id") or selected.get("candidate_id"),
        "validation_result": validation_result,
        "sections": {
            "hypothesis_and_experiment_conditions": {
                "hypothesis": manifest.hypothesis,
                "hypothesis_spec": manifest.hypothesis_spec.as_dict() if manifest.hypothesis_spec is not None else None,
                "strategy_name": manifest.strategy_name,
                "strategy_version": manifest.strategy_version,
                "market": manifest.market,
                "interval": manifest.interval,
                "dataset_splits": selection_report.get("dataset_splits") or {},
                "parameter_space_hash": selection_report.get("parameter_space_hash"),
                "execution_timing_policy": selection_report.get("execution_timing_policy"),
                "portfolio_policy": selection_report.get("portfolio_policy"),
                "risk_policy": selection_report.get("risk_policy") or getattr(manifest, "risk_policy").as_dict(),
            },
            "data_quality": {
                "status": selection_report.get("dataset_quality_gate_status"),
                "reasons": list(selection_report.get("dataset_quality_gate_reasons") or []),
                "reports": selection_report.get("dataset_quality_reports") or {},
                "dataset_snapshot_id": selection_report.get("dataset_snapshot_id"),
                "dataset_content_hash": selection_report.get("dataset_content_hash"),
                "dataset_artifact": selection_report.get("dataset_artifact"),
            },
            "core_performance": {
                "validation_metrics": primary.get("validation_metrics") or selected.get("validation_metrics") or {},
                "validation_metrics_v2": primary.get("validation_metrics_v2") or selected.get("validation_metrics_v2") or {},
                "final_holdout_metrics": holdout.get("metrics") or {},
                "final_holdout_metrics_v2": holdout.get("metrics_v2") or {},
                "benchmark_metrics": selection_report.get("benchmark_metrics") or {},
            },
            "trade_analysis": {
                "closed_trade_diagnostics": selection_report.get("closed_trade_diagnostics_summary") or {},
                "execution_event_summary": primary.get("execution_event_summary") or selected.get("execution_event_summary") or {},
                "participation_summary": selected.get("participation_summary") or {},
            },
            "cost_analysis": {
                "cost_assumption_contract": selection_report.get("cost_assumption_contract"),
                "base_cost_assumption": selection_report.get("base_cost_assumption"),
                "cost_authority_source": selection_report.get("cost_authority_source"),
                "cost_sensitivity": selected.get("cost_sensitivity") or {},
                "scenario_results": _scenario_cost_results(selected),
            },
            "market_regime_analysis": {
                "classifier_version": selection_report.get("regime_classifier_version"),
                "bucket_performance": selection_report.get("market_regime_bucket_performance") or [],
                "coverage": selection_report.get("market_regime_coverage") or {},
                "gate_result": selection_report.get("regime_gate_result"),
                "allowed_regimes": list(selection_report.get("allowed_live_regimes") or []),
                "blocked_regimes": list(selection_report.get("blocked_live_regimes") or []),
            },
            "parameter_robustness": {
                "stress_suite": selected.get("validation_stress_suite") or selection_report.get("best_validation_stress_suite") or {},
                "stress_gate_result": selection_report.get("stress_suite_gate_result"),
                "stress_fail_reasons": list(selection_report.get("stress_suite_fail_reasons") or []),
                "statistical_gate_result": selection_report.get("statistical_gate_result"),
                "statistical_fail_reasons": list(selection_report.get("statistical_gate_fail_reasons") or []),
                "walk_forward_gate_result": selection_report.get("walk_forward_gate_result"),
                "walk_forward_metrics": selected.get("walk_forward_metrics") or {},
                "candidate_final_scores": selection_report.get("candidate_final_scores") or [],
            },
            "out_of_sample_results": {
                "selection_artifact_hash": selection_report.get("selection_artifact_hash"),
                "confirmation_hash": (
                    final_holdout_confirmation.get("content_hash")
                    if isinstance(final_holdout_confirmation, dict) else None
                ),
                "confirmation_gate_result": (
                    final_holdout_confirmation.get("confirmation_gate_result")
                    if isinstance(final_holdout_confirmation, dict) else "NOT_RUN"
                ),
                "metrics": holdout.get("metrics") or {},
                "metrics_v2": holdout.get("metrics_v2") or {},
            },
            "failure_periods": _failure_periods(selection_report, selected),
            "known_limitations": {
                "data": selection_report.get("data_limitations") or {},
                "execution": list(selection_report.get("execution_limitations") or []),
                "statistical": list(selection_report.get("statistical_evidence_limitations") or []),
                "stress": _stress_limitations(selected, selection_report),
            },
            "research_conclusion": {
                "automated_evidence_conclusion": _automated_conclusion(validation_result),
                "validation_result": validation_result,
                "validation_stages": validation_stages,
                "blocking_reasons": sorted(set(blocking_reasons)),
                "human_research_decision": "NOT_REVIEWED",
                "operational_permission": False,
            },
        },
    }
    reasons = validate_research_decision_report(report, verify_hash=False)
    if reasons:
        raise ResearchDecisionReportError("research_decision_report_invalid:" + ",".join(reasons))
    report["content_hash"] = sha256_prefixed(content_hash_payload(report), label="research_decision_report")
    return report


def validate_research_decision_report(report: object, *, verify_hash: bool = True) -> list[str]:
    if not isinstance(report, dict):
        return ["report_must_be_object"]
    reasons: list[str] = []
    if report.get("schema_version") != 1 or report.get("artifact_type") != "research_decision_report":
        reasons.append("report_schema_invalid")
    sections = report.get("sections")
    if not isinstance(sections, dict):
        reasons.append("report_sections_missing")
    else:
        missing = sorted(set(REPORT_SECTIONS) - set(sections))
        unknown = sorted(set(sections) - set(REPORT_SECTIONS))
        if missing:
            reasons.append("report_sections_missing:" + ",".join(missing))
        if unknown:
            reasons.append("report_sections_unknown:" + ",".join(unknown))
        if any(not isinstance(sections.get(name), dict) for name in REPORT_SECTIONS):
            reasons.append("report_section_payload_invalid")
        conclusion = sections.get("research_conclusion")
        if isinstance(conclusion, dict):
            if conclusion.get("operational_permission") is not False:
                reasons.append("report_must_not_grant_operational_permission")
            if conclusion.get("human_research_decision") != "NOT_REVIEWED":
                reasons.append("report_automated_result_must_not_claim_human_review")
    if verify_hash:
        recorded = report.get("content_hash")
        material = {key: value for key, value in report.items() if key != "content_hash"}
        expected = sha256_prefixed(content_hash_payload(material), label="research_decision_report")
        if recorded != expected:
            reasons.append("report_content_hash_mismatch")
    return sorted(set(reasons))


def _primary_scenario(selected: dict[str, Any]) -> dict[str, Any]:
    primary_id = str(selected.get("primary_scenario_id") or "")
    return next(
        (
            item for item in selected.get("scenario_results") or selected.get("scenarios") or []
            if isinstance(item, dict) and str(item.get("scenario_id") or "") == primary_id
        ),
        {},
    )


def _holdout_result(confirmation: dict[str, Any] | None, selected: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(confirmation, dict):
        return {}
    selected_id = str(selected.get("parameter_candidate_id") or selected.get("candidate_id") or "")
    return next(
        (
            item for item in confirmation.get("candidate_results") or []
            if isinstance(item, dict) and str(item.get("candidate_id") or "") == selected_id
        ),
        {},
    )


def _scenario_cost_results(selected: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "scenario_id": item.get("scenario_id"),
            "scenario_role": item.get("scenario_role"),
            "cost_model": item.get("cost_model"),
            "validation_metrics": item.get("validation_metrics"),
        }
        for item in selected.get("scenario_results") or selected.get("scenarios") or []
        if isinstance(item, dict)
    ]


def _failure_periods(selection_report: dict[str, Any], selected: dict[str, Any]) -> dict[str, Any]:
    diagnostics = selection_report.get("closed_trade_diagnostics_summary") or {}
    stress = selected.get("validation_stress_suite") or selection_report.get("best_validation_stress_suite") or {}
    walk_forward = selected.get("walk_forward_metrics") or {}
    return {
        "top_losing_trades": list(diagnostics.get("top_losing_trades") or []),
        "loss_by_entry_exit_regime": diagnostics.get("loss_by_entry_exit_regime") or {},
        "period_ablation": stress.get("period_ablation") if isinstance(stress, dict) else None,
        "walk_forward_windows": walk_forward.get("windows") if isinstance(walk_forward, dict) else [],
        "empty_semantics": "no_failure_period_evidence_identified",
    }


def _stress_limitations(selected: dict[str, Any], selection_report: dict[str, Any]) -> list[str]:
    limitations: set[str] = set()
    for value in (
        selected.get("validation_stress_suite"),
        selected.get("final_holdout_stress_suite"),
        selection_report.get("best_validation_stress_suite"),
    ):
        if isinstance(value, dict):
            limitations.update(str(item) for item in value.get("limitations") or [])
    return sorted(limitations)


def _automated_conclusion(result: str) -> str:
    return {
        "PASS": "AUTOMATED_RESEARCH_EVIDENCE_PASSED",
        "FAIL": "AUTOMATED_RESEARCH_EVIDENCE_FAILED",
        "INSUFFICIENT_EVIDENCE": "AUTOMATED_RESEARCH_EVIDENCE_INSUFFICIENT",
    }.get(result, "AUTOMATED_RESEARCH_EVIDENCE_UNKNOWN")
