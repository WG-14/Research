from __future__ import annotations

import copy
import math
from typing import Any

from .experiment_manifest import FinalSelectionContract
from .hashing import sha256_prefixed


FINAL_SELECTION_SCHEMA_VERSION = 2
SELECTION_ARTIFACT_SCHEMA_VERSION = 2
FINAL_HOLDOUT_CONFIRMATION_SCHEMA_VERSION = 2
FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION = 1
SELECTION_UNIVERSE_HASH_SEMANTICS = (
    "candidate_identity_contract_and_final_score_hashes_v1"
)
LEGACY_IMPLICIT_FINAL_RANK_WARNING = "legacy_implicit_final_rank_policy_v1"


def is_computed_candidate(candidate: dict[str, Any]) -> bool:
    return (
        candidate.get("metrics_v2_source") == "computed"
        and candidate.get("candidate_failed_before_complete_metrics") is False
        and candidate.get("evaluation_status") == "completed"
        and candidate.get("metrics_status") == "complete"
    )


def apply_final_selection_contract(
    *,
    contract: FinalSelectionContract | dict[str, Any] | None,
    candidates: list[dict[str, Any]],
    report_context: dict[str, Any],
    validation_required: bool,
) -> dict[str, Any]:
    contract_payload = _contract_payload(contract)
    if contract_payload is None:
        reasons = (
            ["final_selection_contract_missing"]
            if validation_required
            else [LEGACY_IMPLICIT_FINAL_RANK_WARNING]
        )
        return {
            "final_selection_schema_version": FINAL_SELECTION_SCHEMA_VERSION,
            "final_selection_contract": None,
            "final_selection_contract_hash": None,
            "candidate_universe": None,
            "selected_candidate_id": None,
            "selected_candidate_score_hash": None,
            "candidate_final_scores_hash": None,
            "candidate_final_scores": [],
            "gate_result": "FAIL" if validation_required else "WARN",
            "fail_reasons": reasons,
        }

    contract_hash = sha256_prefixed(contract_payload)
    selection_inputs = [
        final_selection_candidate_input(candidate, contract_payload)
        for candidate in candidates
    ]
    candidate_ids = [
        str(candidate.get("parameter_candidate_id") or "")
        for candidate in selection_inputs
    ]
    duplicate_ids = sorted(
        {
            candidate_id
            for candidate_id in candidate_ids
            if candidate_ids.count(candidate_id) > 1
        }
    )
    if duplicate_ids:
        return {
            "final_selection_schema_version": FINAL_SELECTION_SCHEMA_VERSION,
            "final_selection_contract": contract_payload,
            "final_selection_contract_hash": contract_hash,
            "candidate_universe": contract_payload.get("candidate_universe"),
            "selected_candidate_id": None,
            "selected_candidate_score_hash": None,
            "candidate_final_scores_hash": None,
            "candidate_final_scores": [],
            "gate_result": "FAIL",
            "fail_reasons": ["final_selection_duplicate_candidate_id"],
        }
    ranking = list(contract_payload.get("ranking") or [])
    scored = [
        _score_candidate(
            contract=contract_payload,
            ranking=ranking,
            candidate=candidate,
            report_context=report_context,
        )
        for candidate in selection_inputs
    ]
    eligible = [item for item in scored if item["eligible"]]
    selected: dict[str, Any] | None = None
    if eligible:
        selected = min(eligible, key=lambda item: tuple(item["_sort_key"]))
    # The score list is evidence, not presentation. Canonicalize it by the
    # final-selection rank tuple and candidate id so its hash cannot inherit
    # input order or the legacy _candidate_rank_key ordering used elsewhere.
    scored = sorted(
        scored,
        key=lambda item: (
            0 if item["eligible"] else 1,
            tuple(item["_sort_key"]),
            str(item["candidate_id"]),
        ),
    )
    public_scores = [
        {key: value for key, value in item.items() if key != "_sort_key"}
        for item in scored
    ]
    for item in public_scores:
        item["score_hash"] = sha256_prefixed(
            {key: value for key, value in item.items() if key != "score_hash"}
        )
    selected_public = None
    if selected is not None:
        selected_public = next(
            item
            for item in public_scores
            if item["candidate_id"] == selected["candidate_id"]
        )
    fail_reasons = sorted(
        {
            str(reason)
            for item in public_scores
            if not item["eligible"]
            for reason in item.get("eligibility_reasons") or []
        }
    )
    if not eligible:
        fail_reasons = sorted(
            set(fail_reasons) | {"final_selection_no_eligible_candidates"}
        )
    scores_hash = sha256_prefixed(public_scores) if public_scores else None
    return {
        "final_selection_schema_version": FINAL_SELECTION_SCHEMA_VERSION,
        "final_selection_contract": contract_payload,
        "final_selection_contract_hash": contract_hash,
        "candidate_universe": contract_payload.get("candidate_universe"),
        "selected_candidate_id": selected_public.get("candidate_id")
        if selected_public
        else None,
        "selected_candidate_score_hash": selected_public.get("score_hash")
        if selected_public
        else None,
        "candidate_final_scores_hash": scores_hash,
        "candidate_final_scores": public_scores,
        "gate_result": "PASS" if selected_public is not None else "FAIL",
        "fail_reasons": [] if selected_public is not None else fail_reasons,
    }


def final_selection_candidate_input(
    candidate: dict[str, Any],
    contract: FinalSelectionContract | dict[str, Any],
) -> dict[str, Any]:
    """Return the bounded candidate projection that can reproduce selection.

    Compact persisted reports must carry every value that the selection
    contract consumed, without retaining unrelated curves, streams, or later
    confirmatory holdout evidence.
    """

    existing = candidate.get("final_selection_input")
    if isinstance(existing, dict):
        return copy.deepcopy(existing)
    contract_payload = _contract_payload(contract) or {}
    fixed_fields = (
        "parameter_candidate_id",
        "acceptance_gate_result",
        "aggregate_acceptance_gate_result",
        "primary_metric_source",
        "primary_metric_source_semantics",
        "primary_metric_scenario_role",
        "primary_metric_scenario_id",
        "aggregate_gate_source",
        "candidate_failed_before_complete_metrics",
        "evaluation_status",
        "metrics_status",
        "metrics_v2_source",
        "statistical_gate_result",
        "stress_suite_gate_result",
        "execution_calibration_policy_result",
        "metrics_schema_version",
    )
    projection = {
        field: copy.deepcopy(candidate[field])
        for field in fixed_fields
        if field in candidate
    }
    status_fields = (
        "metrics_status",
        "metrics_v2_source",
        "candidate_failed_before_complete_metrics",
    )
    for metrics_field in ("train_metrics_v2", "validation_metrics_v2"):
        metrics = candidate.get(metrics_field)
        if isinstance(metrics, dict):
            compact = {
                field: copy.deepcopy(metrics[field])
                for field in status_fields
                if field in metrics
            }
            if compact:
                projection[metrics_field] = compact
    for rule in contract_payload.get("ranking") or []:
        if not isinstance(rule, dict):
            continue
        metric = str(rule.get("metric") or "")
        if not metric or metric == "parameter_candidate_id":
            continue
        prefixes = (
            ("validation.metrics_v2.", "validation_metrics_v2."),
            ("validation.stress.", "validation_stress_suite."),
            ("validation.benchmark.", "benchmark_metrics.validation."),
        )
        source_path = metric
        target_path = metric
        for prefix, replacement in prefixes:
            if metric.startswith(prefix):
                source_path = replacement + metric[len(prefix) :]
                target_path = source_path
                break
        _copy_nested_selection_value(
            source=candidate,
            source_path=source_path,
            target=projection,
            target_path=target_path,
        )
    return projection


def _copy_nested_selection_value(
    *,
    source: dict[str, Any],
    source_path: str,
    target: dict[str, Any],
    target_path: str,
) -> None:
    source_parts = source_path.split(".")
    value: Any = source
    for part in source_parts:
        if not isinstance(value, dict) or part not in value:
            return
        value = value[part]
    target_parts = target_path.split(".")
    cursor = target
    for part in target_parts[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[target_parts[-1]] = copy.deepcopy(value)


def _selection_candidate_bindings(
    *,
    candidates: list[dict[str, Any]],
    candidate_scores: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    scores_by_candidate_id = {
        str(score.get("candidate_id") or ""): score for score in candidate_scores
    }
    candidate_ids = [
        str(
            candidate.get("parameter_candidate_id")
            or candidate.get("candidate_id")
            or ""
        )
        for candidate in candidates
    ]
    if (
        any(not candidate_id for candidate_id in candidate_ids)
        or len(set(candidate_ids)) != len(candidate_ids)
        or len(scores_by_candidate_id) != len(candidate_scores)
        or set(scores_by_candidate_id) != set(candidate_ids)
    ):
        raise ValueError("selection_result_candidate_score_universe_mismatch")
    bindings: list[dict[str, Any]] = []
    for candidate_id, candidate in sorted(
        zip(candidate_ids, candidates, strict=True), key=lambda item: item[0]
    ):
        candidate_binding = selection_candidate_binding_summary(candidate)
        bindings.append(
            {
                "candidate_id": candidate_id,
                "parameter_values_hash": candidate_binding["parameter_values_hash"],
                "effective_strategy_parameters_hash": candidate_binding[
                    "effective_strategy_parameters_hash"
                ],
                "compiled_strategy_contract_hash": candidate_binding[
                    "compiled_strategy_contract_hash"
                ],
                "selection_score_hash": scores_by_candidate_id[candidate_id].get(
                    "score_hash"
                ),
            }
        )
    return bindings


def _selection_validation_evidence_hash(
    *, selected: dict[str, Any], selected_id: str
) -> str:
    # Selection evidence must never depend on confirmatory holdout fields.  The
    # persisted candidate report removes those fields recursively, so use the
    # same canonical view both before and after holdout execution.
    selected_payload = _selection_only_payload(selected)
    return sha256_prefixed(
        {
            "candidate_id": selected_id,
            "validation_metrics": selected_payload.get("validation_metrics"),
            "validation_metrics_v2": selected_payload.get("validation_metrics_v2"),
            "validation_stress_suite": selected_payload.get("validation_stress_suite"),
            "walk_forward_metrics": selected_payload.get("walk_forward_metrics"),
            "acceptance_gate_result": selected_payload.get("acceptance_gate_result"),
        }
    )


def _selection_parameter_values(candidate: dict[str, Any]) -> dict[str, Any]:
    values = (
        candidate.get("parameter_values_raw") or candidate.get("parameter_values") or {}
    )
    return dict(values) if isinstance(values, dict) else {}


def _selection_only_payload(value: Any) -> Any:
    """Canonicalize pre-holdout evidence by removing confirmatory fields."""

    if isinstance(value, dict):
        return {
            key: _selection_only_payload(item)
            for key, item in sorted(value.items())
            if "final_holdout" not in str(key)
        }
    if isinstance(value, (list, tuple)):
        return [_selection_only_payload(item) for item in value]
    return value


def selection_candidate_binding_summary(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Return the compact candidate evidence needed to verify selection later."""

    existing = candidate.get("selection_binding")
    if isinstance(existing, dict):
        return dict(existing)
    candidate_id = str(
        candidate.get("parameter_candidate_id") or candidate.get("candidate_id") or ""
    )
    primary_id = str(candidate.get("primary_scenario_id") or "")
    scenarios = candidate.get("scenario_results") or candidate.get("scenarios")
    primary = (
        next(
            (
                scenario
                for scenario in scenarios
                if isinstance(scenario, dict)
                and str(scenario.get("scenario_id") or "") == primary_id
            ),
            None,
        )
        if primary_id and isinstance(scenarios, list)
        else None
    )
    return {
        "candidate_id": candidate_id,
        "parameter_values_hash": sha256_prefixed(
            _selection_parameter_values(candidate)
        ),
        "effective_strategy_parameters_hash": candidate.get(
            "effective_strategy_parameters_hash"
        ),
        "compiled_strategy_contract_hash": candidate.get(
            "compiled_strategy_contract_hash"
        ),
        "validation_evidence_hash": _selection_validation_evidence_hash(
            selected=candidate,
            selected_id=candidate_id,
        ),
        "primary_scenario_id": primary_id or None,
        "primary_scenario_compiled_strategy_contract_hash": (
            primary.get("compiled_strategy_contract_hash")
            if isinstance(primary, dict)
            else None
        ),
    }


def build_selection_artifact(
    *,
    manifest_hash: str,
    selection_result: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Freeze the pre-holdout selection and all evidence that may affect it."""
    selected_id = str(selection_result.get("selected_candidate_id") or "")
    selected = next(
        (
            candidate
            for candidate in candidates
            if str(candidate.get("parameter_candidate_id") or "") == selected_id
        ),
        None,
    )
    if selected is None:
        return None
    candidate_scores = selection_result.get("candidate_final_scores")
    if not isinstance(candidate_scores, list) or not all(
        isinstance(score, dict) for score in candidate_scores
    ):
        raise ValueError("selection_result_candidate_scores_missing")
    candidate_scores_hash = sha256_prefixed(candidate_scores)
    if candidate_scores_hash != selection_result.get("candidate_final_scores_hash"):
        raise ValueError("selection_result_candidate_scores_hash_mismatch")
    selection_candidates = _selection_candidate_bindings(
        candidates=candidates,
        candidate_scores=candidate_scores,
    )
    material = {
        "schema_version": SELECTION_ARTIFACT_SCHEMA_VERSION,
        "artifact_type": "pre_holdout_candidate_selection",
        "manifest_hash": manifest_hash,
        "selected_candidate_id": selected_id,
        "parameter_values_hash": sha256_prefixed(_selection_parameter_values(selected)),
        "effective_strategy_parameters_hash": selected.get(
            "effective_strategy_parameters_hash"
        ),
        "compiled_strategy_contract_hash": selected.get(
            "compiled_strategy_contract_hash"
        ),
        "selection_universe_hash_semantics": SELECTION_UNIVERSE_HASH_SEMANTICS,
        "selection_universe_hash": sha256_prefixed(selection_candidates),
        "validation_evidence_hash": _selection_validation_evidence_hash(
            selected=selected,
            selected_id=selected_id,
        ),
        "final_selection_contract_hash": selection_result.get(
            "final_selection_contract_hash"
        ),
        "candidate_scores_hash": candidate_scores_hash,
    }
    return {
        **material,
        "content_hash": sha256_prefixed(material, label="selection_artifact"),
    }


def validate_selection_artifact(artifact: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if artifact.get("schema_version") != SELECTION_ARTIFACT_SCHEMA_VERSION:
        reasons.append("selection_artifact_schema_version_unsupported")
    if artifact.get("artifact_type") != "pre_holdout_candidate_selection":
        reasons.append("selection_artifact_type_invalid")
    if (
        artifact.get("selection_universe_hash_semantics")
        != SELECTION_UNIVERSE_HASH_SEMANTICS
    ):
        reasons.append("selection_artifact_universe_hash_semantics_invalid")
    required_hashes = (
        "manifest_hash",
        "parameter_values_hash",
        "effective_strategy_parameters_hash",
        "compiled_strategy_contract_hash",
        "selection_universe_hash",
        "validation_evidence_hash",
        "final_selection_contract_hash",
        "candidate_scores_hash",
        "content_hash",
    )
    for field in required_hashes:
        if not str(artifact.get(field) or "").startswith("sha256:"):
            reasons.append(f"selection_artifact_{field}_missing")
    expected = sha256_prefixed(
        {key: value for key, value in artifact.items() if key != "content_hash"},
        label="selection_artifact",
    )
    if artifact.get("content_hash") != expected:
        reasons.append("selection_artifact_content_hash_mismatch")
    if not str(artifact.get("selected_candidate_id") or ""):
        reasons.append("selection_artifact_selected_candidate_id_missing")
    return sorted(set(reasons))


def validate_selection_artifact_binding(
    *,
    report: dict[str, Any],
    selection_artifact: dict[str, Any],
    selected_candidate: dict[str, Any] | None = None,
) -> list[str]:
    """Bind a self-valid selection receipt to its authoritative report candidate.

    A content-valid receipt is insufficient on its own: a stale receipt and its
    matching confirmation must not be combined with a different report or
    compiled strategy contract.
    """

    reasons = validate_selection_artifact(selection_artifact)
    embedded = report.get("selection_artifact")
    if isinstance(embedded, dict) and embedded.get(
        "content_hash"
    ) != selection_artifact.get("content_hash"):
        reasons.append("report_selection_artifact_hash_mismatch")
    report_artifact_hash = report.get("selection_artifact_hash")
    if (
        report_artifact_hash is not None
        and report_artifact_hash != selection_artifact.get("content_hash")
    ):
        reasons.append("report_selection_artifact_hash_mismatch")

    for report_field, artifact_field, reason in (
        ("manifest_hash", "manifest_hash", "selection_artifact_manifest_hash_mismatch"),
        (
            "selected_candidate_id",
            "selected_candidate_id",
            "selection_artifact_selected_candidate_mismatch",
        ),
        (
            "final_selection_contract_hash",
            "final_selection_contract_hash",
            "selection_artifact_final_selection_contract_hash_mismatch",
        ),
        (
            "candidate_final_scores_hash",
            "candidate_scores_hash",
            "selection_artifact_candidate_scores_hash_mismatch",
        ),
    ):
        report_value = report.get(report_field)
        if report_value is not None and report_value != selection_artifact.get(
            artifact_field
        ):
            reasons.append(reason)

    selected_id = str(selection_artifact.get("selected_candidate_id") or "")
    candidates = report.get("candidates")
    candidate_rows = (
        [item for item in candidates if isinstance(item, dict)]
        if isinstance(candidates, list)
        else []
    )
    selected = selected_candidate
    if selected is not None:
        provided_id = str(
            selected.get("parameter_candidate_id") or selected.get("candidate_id") or ""
        )
        if provided_id != selected_id:
            reasons.append("selection_artifact_selected_candidate_mismatch")
    else:
        selected = next(
            (
                candidate
                for candidate in candidate_rows
                if str(
                    candidate.get("parameter_candidate_id")
                    or candidate.get("candidate_id")
                    or ""
                )
                == selected_id
            ),
            None,
        )
    if selected is None:
        reasons.append("selection_artifact_candidate_missing")
        return sorted(set(reasons))

    persisted_binding = selected.get("selection_binding")
    if not isinstance(persisted_binding, dict) and (
        "parameter_values_raw" not in selected and "parameter_values" not in selected
    ):
        reasons.append("selection_artifact_parameter_values_missing")
    candidate_binding = selection_candidate_binding_summary(selected)
    if candidate_binding.get("candidate_id") != selected_id:
        reasons.append("selection_artifact_selected_candidate_mismatch")
    for binding_field, artifact_field, reason in (
        (
            "parameter_values_hash",
            "parameter_values_hash",
            "selection_artifact_parameter_hash_mismatch",
        ),
        (
            "effective_strategy_parameters_hash",
            "effective_strategy_parameters_hash",
            "selection_artifact_effective_parameter_hash_mismatch",
        ),
        (
            "compiled_strategy_contract_hash",
            "compiled_strategy_contract_hash",
            "selection_artifact_compiled_contract_hash_mismatch",
        ),
    ):
        if candidate_binding.get(binding_field) != selection_artifact.get(
            artifact_field
        ):
            reasons.append(reason)

    if candidate_binding.get("validation_evidence_hash") != selection_artifact.get(
        "validation_evidence_hash"
    ):
        reasons.append("selection_artifact_validation_evidence_hash_mismatch")

    primary_id = str(candidate_binding.get("primary_scenario_id") or "")
    primary_contract_hash = candidate_binding.get(
        "primary_scenario_compiled_strategy_contract_hash"
    )
    if primary_id and primary_contract_hash is None:
        reasons.append("selection_artifact_primary_scenario_missing")
    elif primary_id and primary_contract_hash != selection_artifact.get(
        "compiled_strategy_contract_hash"
    ):
        reasons.append(
            "selection_artifact_primary_scenario_compiled_contract_hash_mismatch"
        )

    candidate_scores = report.get("candidate_final_scores")
    if isinstance(candidate_scores, list) and all(
        isinstance(score, dict) for score in candidate_scores
    ):
        candidate_scores_hash = sha256_prefixed(candidate_scores)
        if candidate_scores_hash != selection_artifact.get("candidate_scores_hash"):
            reasons.append("selection_artifact_candidate_scores_hash_mismatch")
        try:
            bindings = _selection_candidate_bindings(
                candidates=candidate_rows,
                candidate_scores=candidate_scores,
            )
        except ValueError:
            reasons.append("selection_artifact_candidate_universe_mismatch")
        else:
            if sha256_prefixed(bindings) != selection_artifact.get(
                "selection_universe_hash"
            ):
                reasons.append("selection_artifact_candidate_universe_hash_mismatch")
    elif report.get("final_selection_required"):
        reasons.append("selection_artifact_candidate_scores_missing")

    return sorted(set(reasons))


def validate_confirmation_artifact(
    confirmation: dict[str, Any],
    *,
    selection_artifact: dict[str, Any],
) -> list[str]:
    reasons = validate_selection_artifact(selection_artifact)
    if (
        confirmation.get("schema_version") != FINAL_HOLDOUT_CONFIRMATION_SCHEMA_VERSION
        or confirmation.get("artifact_type") != "final_holdout_confirmation"
    ):
        reasons.append("final_holdout_confirmation_contract_invalid")
    if confirmation.get("selection_artifact_hash") != selection_artifact.get(
        "content_hash"
    ):
        reasons.append("final_holdout_confirmation_selection_hash_mismatch")
    if confirmation.get("manifest_hash") != selection_artifact.get("manifest_hash"):
        reasons.append("final_holdout_confirmation_manifest_hash_mismatch")
    if confirmation.get("selected_candidate_id") != selection_artifact.get(
        "selected_candidate_id"
    ):
        reasons.append("final_holdout_confirmation_selected_candidate_mismatch")
    candidate_results = confirmation.get("candidate_results")
    if not isinstance(candidate_results, list) or len(candidate_results) != 1:
        reasons.append("final_holdout_confirmation_candidate_count_invalid")
    else:
        candidate_id = (
            str(candidate_results[0].get("candidate_id") or "")
            if isinstance(candidate_results[0], dict)
            else ""
        )
        if candidate_id != str(selection_artifact.get("selected_candidate_id") or ""):
            reasons.append("final_holdout_confirmation_candidate_mismatch")
        compiled_hash = (
            str(candidate_results[0].get("compiled_strategy_contract_hash") or "")
            if isinstance(candidate_results[0], dict)
            else ""
        )
        if compiled_hash != str(
            selection_artifact.get("compiled_strategy_contract_hash") or ""
        ):
            reasons.append("final_holdout_confirmation_compiled_contract_hash_mismatch")
    if (
        confirmation.get("final_holdout_result_hash_schema_version")
        != FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION
    ):
        reasons.append("final_holdout_result_hash_schema_version_invalid")
    recorded_result_hash = str(confirmation.get("final_holdout_result_hash") or "")
    if not recorded_result_hash.startswith("sha256:"):
        reasons.append("final_holdout_result_hash_missing")
    elif recorded_result_hash != compute_final_holdout_result_hash(confirmation):
        reasons.append("final_holdout_result_hash_mismatch")
    recorded = confirmation.get("content_hash")
    material = {
        key: value
        for key, value in confirmation.items()
        if key not in {"content_hash", "confirmation_artifact_path"}
    }
    if recorded != sha256_prefixed(material, label="final_holdout_confirmation"):
        reasons.append("final_holdout_confirmation_content_hash_mismatch")
    return sorted(set(reasons))


def compute_final_holdout_result_hash(payload: dict[str, Any]) -> str:
    """Hash the non-circular final-holdout result fixed by registry completion."""

    return sha256_prefixed(
        {
            "schema_version": FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION,
            "selection_artifact_hash": payload.get("selection_artifact_hash"),
            "selected_candidate_id": payload.get("selected_candidate_id"),
            "candidate_results": payload.get("candidate_results"),
            "confirmation_gate_result": payload.get("confirmation_gate_result"),
            "confirmation_gate_fail_reasons": payload.get(
                "confirmation_gate_fail_reasons"
            ),
        },
        label="final_holdout_result",
    )


def validate_final_selection_report(report: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if report.get("final_selection_required") and not isinstance(
        report.get("final_selection_contract"), dict
    ):
        return ["final_selection_contract_missing"]
    if (
        not report.get("final_selection_required")
        and report.get("final_selection_gate_result") == "WARN"
    ):
        return []
    contract = report.get("final_selection_contract")
    if not isinstance(contract, dict):
        return ["final_selection_contract_missing"]
    if not str(report.get("final_selection_contract_hash") or "").startswith("sha256:"):
        reasons.append("final_selection_contract_hash_missing")
    elif sha256_prefixed(contract) != report.get("final_selection_contract_hash"):
        reasons.append("final_selection_contract_hash_mismatch")
    candidates = report.get("candidates")
    if not isinstance(candidates, list) or not all(
        isinstance(item, dict) for item in candidates
    ):
        return sorted(set(reasons) | {"final_selection_score_hash_mismatch"})
    recomputed = apply_final_selection_contract(
        contract=contract,
        candidates=list(candidates),
        report_context=report,
        validation_required=bool(report.get("final_selection_required")),
    )
    for field, missing_reason, mismatch_reason in (
        (
            "candidate_final_scores_hash",
            "final_selection_score_hash_missing",
            "final_selection_score_hash_mismatch",
        ),
        (
            "selected_candidate_score_hash",
            "final_selection_score_hash_missing",
            "final_selection_score_hash_mismatch",
        ),
    ):
        expected = report.get(field)
        if not str(expected or "").startswith("sha256:"):
            reasons.append(missing_reason)
        elif expected != recomputed.get(field):
            reasons.append(mismatch_reason)
    if report.get("final_selection_contract_hash") != recomputed.get(
        "final_selection_contract_hash"
    ):
        reasons.append("final_selection_contract_hash_mismatch")
    if report.get("selected_candidate_id") != recomputed.get("selected_candidate_id"):
        reasons.append("final_selection_selected_candidate_mismatch")
    if report.get("best_candidate_id") != recomputed.get("selected_candidate_id"):
        reasons.append("final_selection_selected_candidate_mismatch")
    if (
        report.get("final_selection_gate_result") != "PASS"
        or recomputed.get("gate_result") != "PASS"
    ):
        reasons.append("final_selection_gate_not_passed")
    selection_artifact = report.get("selection_artifact")
    if (
        report.get("final_selection_gate_result") == "PASS"
        or selection_artifact is not None
    ):
        if not isinstance(selection_artifact, dict):
            reasons.append("selection_artifact_missing")
        else:
            reasons.extend(
                validate_selection_artifact_binding(
                    report=report,
                    selection_artifact=selection_artifact,
                )
            )
    return sorted(set(reasons))


def _contract_payload(
    contract: FinalSelectionContract | dict[str, Any] | None,
) -> dict[str, Any] | None:
    if contract is None:
        return None
    if isinstance(contract, FinalSelectionContract):
        return contract.as_dict()
    if isinstance(contract, dict):
        return dict(contract)
    return None


def _score_candidate(
    *,
    contract: dict[str, Any],
    ranking: list[Any],
    candidate: dict[str, Any],
    report_context: dict[str, Any],
) -> dict[str, Any]:
    reasons = _candidate_universe_reasons(contract=contract, candidate=candidate)
    metric_source_reasons = _metric_source_semantics_reasons(candidate)
    reasons.extend(metric_source_reasons)
    reasons.extend(_fallback_metrics_reasons(candidate))
    reasons.extend(
        _must_pass_reasons(
            contract=contract, candidate=candidate, report_context=report_context
        )
    )
    components: list[dict[str, Any]] = []
    sort_key: list[Any] = []
    rank_tuple: list[Any] = []
    for rule in ranking:
        if not isinstance(rule, dict):
            reasons.append("final_selection_ranking_rule_malformed")
            continue
        metric = str(rule.get("metric") or "")
        order = str(rule.get("order") or "asc")
        required = bool(rule.get("required", True))
        null_policy = str(
            rule.get("null_policy") or contract.get("null_metric_policy") or ""
        )
        unsupported_reason = _unsupported_metric_reason(metric)
        value, source = _metric_value(candidate=candidate, metric=metric)
        if unsupported_reason is not None and required:
            reasons.append(unsupported_reason)
        elif value is None and required:
            reasons.append(f"final_selection_required_metric_missing:{metric}")
        component = {
            "metric": metric,
            "value": _json_scalar(value),
            "order": order,
            "required": required,
            "null_policy": null_policy,
            "source": source,
            "primary_metric_source_semantics": candidate.get(
                "primary_metric_source_semantics"
            ),
            "primary_metric_scenario_role": candidate.get(
                "primary_metric_scenario_role"
            ),
            "primary_metric_scenario_id": candidate.get("primary_metric_scenario_id"),
            "aggregate_gate_source": candidate.get("aggregate_gate_source"),
        }
        components.append(component)
        sort_value = _sort_value(value=value, order=order, required=required)
        sort_key.append(sort_value)
        rank_tuple.append(_json_scalar(sort_value))
    return {
        "candidate_id": str(candidate.get("parameter_candidate_id") or ""),
        "eligible": not reasons,
        "eligibility_reasons": sorted(set(reasons)),
        "rank_tuple": rank_tuple,
        "rank_components": components,
        "selection_metric_policy": {
            "primary_metric_source": candidate.get("primary_metric_source"),
            "primary_metric_source_semantics": candidate.get(
                "primary_metric_source_semantics"
            ),
            "primary_metric_scenario_role": candidate.get(
                "primary_metric_scenario_role"
            ),
            "primary_metric_scenario_id": candidate.get("primary_metric_scenario_id"),
            "aggregate_gate_source": candidate.get("aggregate_gate_source"),
            "candidate_eligibility_gate": "aggregate_acceptance_gate_result",
        },
        "_sort_key": sort_key,
    }


def _candidate_universe_reasons(
    *, contract: dict[str, Any], candidate: dict[str, Any]
) -> list[str]:
    universe = contract.get("candidate_universe")
    if universe != "acceptance_gate_passed_required_scenarios":
        return ["final_selection_candidate_universe_unsupported"]
    aggregate_gate = candidate.get(
        "aggregate_acceptance_gate_result", candidate.get("acceptance_gate_result")
    )
    if aggregate_gate != "PASS":
        return ["final_selection_acceptance_gate_not_passed"]
    return []


def _metric_source_semantics_reasons(candidate: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if (
        candidate.get("primary_metric_source_semantics")
        != "primary_base_scenario_alias"
    ):
        reasons.append("final_selection_primary_metric_source_semantics_missing")
    if candidate.get("primary_metric_scenario_role") != "base":
        reasons.append("final_selection_primary_metric_scenario_role_missing")
    if candidate.get("aggregate_gate_source") != "required_scenario_policy":
        reasons.append("final_selection_aggregate_gate_source_missing")
    return reasons


def _fallback_metrics_reasons(candidate: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if not is_computed_candidate(candidate):
        reasons.append("final_selection_candidate_not_computed_complete")
    if bool(candidate.get("candidate_failed_before_complete_metrics")):
        reasons.append("final_selection_candidate_failed_before_complete_metrics")
    if candidate.get("metrics_status") == "unavailable":
        reasons.append("final_selection_metrics_unavailable")
    if candidate.get("metrics_v2_source") == "failure_fallback":
        reasons.append("final_selection_metrics_failure_fallback")
    if candidate.get("evaluation_status") != "completed":
        reasons.append("final_selection_evaluation_not_completed")
    if candidate.get("metrics_status") != "complete":
        reasons.append("final_selection_metrics_not_complete")
    if candidate.get("metrics_v2_source") != "computed":
        reasons.append("final_selection_metrics_not_computed")
    for split_key in ("train_metrics_v2", "validation_metrics_v2"):
        metrics = candidate.get(split_key)
        if not isinstance(metrics, dict):
            continue
        if metrics.get("metrics_status") == "unavailable":
            reasons.append(f"final_selection_{split_key}_unavailable")
        if metrics.get("metrics_v2_source") == "failure_fallback":
            reasons.append(f"final_selection_{split_key}_failure_fallback")
        if bool(metrics.get("candidate_failed_before_complete_metrics")):
            reasons.append(
                f"final_selection_{split_key}_candidate_failed_before_complete_metrics"
            )
    return sorted(set(reasons))


def _must_pass_reasons(
    *,
    contract: dict[str, Any],
    candidate: dict[str, Any],
    report_context: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    raw_must_pass = contract.get("must_pass")
    must_pass = raw_must_pass if isinstance(raw_must_pass, dict) else {}
    for field, expected in must_pass.items():
        actual = _must_pass_value(
            field=str(field), candidate=candidate, report_context=report_context
        )
        if actual != expected:
            reasons.append(f"final_selection_must_pass_failed:{field}")
    return reasons


def _must_pass_value(
    *, field: str, candidate: dict[str, Any], report_context: dict[str, Any]
) -> Any:
    if field == "dataset_quality_gate_status":
        return report_context.get("dataset_quality_gate_status")
    if field == "statistical_gate_result":
        return candidate.get("statistical_gate_result") or report_context.get(
            "statistical_gate_result"
        )
    if field == "stress_suite_gate_result":
        return candidate.get("stress_suite_gate_result") or report_context.get(
            "stress_suite_gate_result"
        )
    if field == "execution_calibration_policy_result":
        value = candidate.get("execution_calibration_policy_result")
        return value.get("status") if isinstance(value, dict) else value
    if field == "metrics_schema_version":
        return candidate.get("metrics_schema_version")
    return candidate.get(field, report_context.get(field))


def _metric_value(*, candidate: dict[str, Any], metric: str) -> tuple[Any, str]:
    if metric == "parameter_candidate_id":
        return str(
            candidate.get("parameter_candidate_id") or ""
        ), "candidate.parameter_candidate_id"
    prefixes = {
        "validation.metrics_v2.": "validation_metrics_v2",
        "validation.stress.": "validation_stress_suite",
        "validation.benchmark.": "benchmark_metrics.validation",
    }
    for prefix, source_key in prefixes.items():
        if metric.startswith(prefix):
            source = _source_payload(candidate, source_key)
            if (
                isinstance(source, dict)
                and source_key.endswith("metrics_v2")
                and (
                    source.get("metrics_status") == "unavailable"
                    or source.get("metrics_v2_source") == "failure_fallback"
                )
            ):
                return None, source_key
            value = _nested_value(source, metric[len(prefix) :])
            return value, source_key
    return _nested_value(candidate, metric), "candidate"


def _source_payload(candidate: dict[str, Any], source_key: str) -> Any:
    if source_key.startswith("benchmark_metrics."):
        metrics = candidate.get("benchmark_metrics")
        if not isinstance(metrics, dict):
            return None
        split = source_key.split(".", 1)[1]
        return metrics.get(split)
    return candidate.get(source_key)


def _nested_value(payload: Any, dotted: str) -> Any:
    current = payload
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    if isinstance(current, (int, float)) and not isinstance(current, bool):
        value = float(current)
        return value if math.isfinite(value) else None
    if isinstance(current, str):
        return current
    if isinstance(current, bool):
        return current
    return None


def _unsupported_metric_reason(metric: str) -> str | None:
    if (
        metric.endswith("sharpe_ratio")
        or ".sharpe_ratio" in metric
        or metric == "sharpe_ratio"
    ):
        return "final_selection_sharpe_unavailable_without_period_return_series"
    if (
        metric.endswith("sortino_ratio")
        or ".sortino_ratio" in metric
        or metric == "sortino_ratio"
    ):
        return "final_selection_sortino_unavailable_without_period_return_series"
    return None


def _sort_value(*, value: Any, order: str, required: bool) -> Any:
    if value is None:
        return math.inf
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        value = 1.0 if value else 0.0
    numeric = float(value)
    if not math.isfinite(numeric):
        return math.inf
    return numeric if order == "asc" else -numeric


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    return value
