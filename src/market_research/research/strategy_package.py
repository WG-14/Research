"""Deterministic offline Strategy Research Package export."""

from __future__ import annotations

from typing import Any

from market_research.paths import ResearchPathManager

from .execution_evidence import REQUIRED_FIELDS, REQUIRED_FIELDS_V2, REQUIRED_FIELDS_V3
from .execution_invariants import (
    CAUSAL_TIMELINE_VALIDATOR,
    MARKET_KNOWLEDGE_TIME_POLICY,
)
from .experiment_registry import (
    experiment_registry_path,
    validate_experiment_registry_binding,
)
from .final_selection import (
    validate_confirmation_artifact,
    validate_final_selection_report,
    validate_selection_artifact_binding,
)
from .hashing import report_content_hash_payload, sha256_prefixed
from .hypothesis_contract import (
    HYPOTHESIS_LINEAGE_SCHEMA_VERSION,
    parse_hypothesis_spec,
)
from .governance import governance_registry_path, validate_strategy_approval
from .strategy_compiler import (
    StrategyCompilationError,
    validate_compiled_strategy_contract,
)
from .strategy_contract import is_sha256_hash
from .validation_pipeline import (
    resolve_bound_selected_candidate,
    validate_validated_research_result,
)


class StrategyPackageError(ValueError):
    pass


_CONTRACT_FIELDS = (
    "strategy_spec_hash",
    "decision_contract_version",
    "data_requirements",
    "execution_timing_policy",
    "execution_model",
    "cost_assumption",
    "partial_fill_assumptions",
    "order_failure_assumptions",
    "portfolio_policy",
    "risk_policy",
    "execution_limitations",
    "suspension_or_invalidation_criteria",
)

_EVIDENCE_BINDING_FIELDS = (
    "strategy_registry_hash",
    "strategy_plugin_contract_hash",
    "compiled_strategy_contract_hash",
    "capability_contract_hash",
)


def _complete_semantic_contract(
    *,
    report: dict[str, Any],
    merged: dict[str, Any],
    primary: dict[str, Any],
    confirmation: dict[str, Any],
    compiled_payload: dict[str, Any],
    selected_id: str,
    strategy_name: str,
    strategy_version: str,
) -> dict[str, Any]:
    hypothesis = report.get("hypothesis_spec")
    if not isinstance(hypothesis, dict):
        raise StrategyPackageError("strategy_package_hypothesis_spec_missing")
    try:
        parsed_hypothesis = parse_hypothesis_spec(hypothesis)
    except ValueError as exc:
        raise StrategyPackageError(
            "strategy_package_hypothesis_lineage_invalid"
        ) from exc
    if parsed_hypothesis.schema_version != HYPOTHESIS_LINEAGE_SCHEMA_VERSION:
        raise StrategyPackageError("strategy_package_hypothesis_lineage_required")
    if parsed_hypothesis.contract_hash() != report.get("hypothesis_contract_hash"):
        raise StrategyPackageError("strategy_package_hypothesis_contract_hash_mismatch")
    if parsed_hypothesis.lineage_hash() != report.get("hypothesis_lineage_hash"):
        raise StrategyPackageError("strategy_package_hypothesis_lineage_hash_mismatch")
    strategy_spec = merged.get("strategy_spec")
    if not isinstance(strategy_spec, dict):
        raise StrategyPackageError("strategy_package_strategy_spec_missing")
    if sha256_prefixed(strategy_spec) != merged.get("strategy_spec_hash"):
        raise StrategyPackageError("strategy_package_strategy_spec_hash_mismatch")
    if (
        strategy_spec.get("strategy_name") != strategy_name
        or strategy_spec.get("strategy_version") != strategy_version
    ):
        raise StrategyPackageError("strategy_package_strategy_spec_identity_mismatch")
    rule_spec = strategy_spec.get("rule_spec")
    features = strategy_spec.get("feature_definitions")
    if not isinstance(rule_spec, dict):
        raise StrategyPackageError("strategy_package_rule_spec_missing")
    if (
        not isinstance(features, list)
        or not features
        or not all(isinstance(item, dict) for item in features)
    ):
        raise StrategyPackageError("strategy_package_feature_definitions_missing")
    market = str(report.get("market") or "").strip()
    interval = str(report.get("interval") or "").strip()
    if not market or not interval:
        raise StrategyPackageError("strategy_package_target_asset_missing")
    target_asset: dict[str, object] = {"market": market, "interval": interval}
    instrument_evidence = report.get("instrument_evidence")
    if instrument_evidence is not None:
        if not isinstance(instrument_evidence, dict):
            raise StrategyPackageError("strategy_package_instrument_evidence_invalid")
        required_instrument_fields = {
            "instrument_id",
            "instrument_version_id",
            "instrument_contract_hash",
            "trading_currency",
            "price_tick",
            "quantity_step",
            "trading_unit",
            "corporate_action_set_hash",
            "corporate_action_policy_hash",
        }
        if any(
            not str(instrument_evidence.get(field) or "").strip()
            for field in required_instrument_fields
        ):
            raise StrategyPackageError(
                "strategy_package_instrument_evidence_incomplete"
            )
        for field in (
            "instrument_contract_hash",
            "corporate_action_set_hash",
            "corporate_action_policy_hash",
        ):
            if not str(instrument_evidence[field]).startswith("sha256:"):
                raise StrategyPackageError(
                    "strategy_package_instrument_evidence_hash_invalid"
                )
        target_asset["instrument_evidence"] = dict(instrument_evidence)
    expected_performance = _expected_performance_range(
        primary=primary,
        confirmation=confirmation,
        selected_id=selected_id,
    )
    return {
        "hypothesis": hypothesis,
        "hypothesis_contract_hash": parsed_hypothesis.contract_hash(),
        "hypothesis_lineage_hash": parsed_hypothesis.lineage_hash(),
        "research_question_ref": parsed_hypothesis.research_question_ref.as_dict()
        if parsed_hypothesis.research_question_ref is not None
        else None,
        "observation_refs": [
            item.as_dict() for item in parsed_hypothesis.observation_refs
        ],
        "target_asset": target_asset,
        "strategy_spec": strategy_spec,
        "feature_definitions": features,
        "compiled_strategy_contract": compiled_payload,
        "effective_strategy_parameters": dict(
            compiled_payload["materialized_parameters"]
        ),
        "effective_strategy_parameters_hash": compiled_payload[
            "materialized_parameters_hash"
        ],
        "signal_calculation_timing": merged["execution_timing_policy"],
        "entry_conditions": {
            "entry": rule_spec.get("entry"),
            "entry_prohibitions": list(rule_spec.get("entry_prohibitions") or []),
        },
        "fill_assumptions": {
            "execution_timing_policy": merged["execution_timing_policy"],
            "execution_model": merged["execution_model"],
            "partial_fill_assumptions": merged["partial_fill_assumptions"],
            "order_failure_assumptions": merged["order_failure_assumptions"],
        },
        "take_profit": rule_spec.get("take_profit"),
        "edge_invalidation": rule_spec.get("edge_invalidation"),
        "time_exit": rule_spec.get("time_exit"),
        "stop_loss": rule_spec.get("stop_loss"),
        "position_sizing": {
            "rule": rule_spec.get("position_sizing"),
            "portfolio_policy": merged["portfolio_policy"],
        },
        "cost_assumptions": merged["cost_assumption"],
        "allowed_market_regimes": {
            "allowed": list(report.get("allowed_live_regimes") or []),
            "blocked": list(report.get("blocked_live_regimes") or []),
            "empty_allowed_semantics": "no_regime_restriction_declared",
        },
        "strategy_suspension_conditions": merged["suspension_or_invalidation_criteria"],
        "expected_performance_range": expected_performance,
        "known_limitations": {
            "data": report.get("data_limitations") or {},
            "execution": list(merged.get("execution_limitations") or []),
            "statistical": list(report.get("statistical_evidence_limitations") or []),
            "stress": _stress_limitations(merged),
        },
    }


def _expected_performance_range(
    *,
    primary: dict[str, Any],
    confirmation: dict[str, Any],
    selected_id: str,
) -> dict[str, Any]:
    validation = primary.get("validation_metrics")
    holdout_row = next(
        (
            item
            for item in confirmation.get("candidate_results") or []
            if isinstance(item, dict)
            and str(item.get("candidate_id") or "") == selected_id
        ),
        None,
    )
    holdout = holdout_row.get("metrics") if isinstance(holdout_row, dict) else None
    if not isinstance(validation, dict) or not isinstance(holdout, dict):
        raise StrategyPackageError(
            "strategy_package_expected_performance_evidence_missing"
        )
    ranges: dict[str, dict[str, float | int]] = {}
    for name in sorted(set(validation) | set(holdout)):
        observations = [
            value
            for value in (validation.get(name), holdout.get(name))
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if observations:
            ranges[name] = {
                "minimum": min(observations),
                "maximum": max(observations),
                "observation_count": len(observations),
            }
    if not ranges:
        raise StrategyPackageError("strategy_package_expected_performance_range_empty")
    return {
        "basis": "validation_and_final_holdout_observed_range",
        "validation_metrics": validation,
        "final_holdout_metrics": holdout,
        "metric_ranges": ranges,
    }


def _stress_limitations(merged: dict[str, Any]) -> list[str]:
    limitations: set[str] = set()
    for key in (
        "validation_stress_suite",
        "final_holdout_stress_suite",
        "best_validation_stress_suite",
    ):
        value = merged.get(key)
        if isinstance(value, dict):
            limitations.update(str(item) for item in value.get("limitations") or [])
    return sorted(limitations)


def _canonical_package_contract(
    *,
    report: dict[str, Any],
    selected: dict[str, Any],
    primary: dict[str, Any],
    compiled_payload: dict[str, Any],
    authoritative: bool,
) -> dict[str, Any]:
    """Project package semantics from their canonical production sources."""

    strategy_spec = selected.get("strategy_spec") or report.get("strategy_spec")
    reality = primary.get("execution_reality_contract")
    hypothesis = report.get("hypothesis_spec")
    risk_policy = report.get("risk_policy")
    rule_spec = (
        strategy_spec.get("rule_spec") if isinstance(strategy_spec, dict) else None
    )
    canonical = {
        "decision_contract_version": (
            strategy_spec.get("decision_contract_version")
            if isinstance(strategy_spec, dict)
            else None
        ),
        "data_requirements": compiled_payload.get("data_requirements"),
        "execution_timing_policy": (
            primary.get("execution_timing_policy")
            or report.get("execution_timing_policy")
        ),
        "execution_model": report.get("execution_model")
        or primary.get("execution_model"),
        "cost_assumption": primary.get("cost_assumption")
        or report.get("base_cost_assumption"),
        "partial_fill_assumptions": (
            reality.get("partial_fill_model") if isinstance(reality, dict) else None
        ),
        "order_failure_assumptions": (
            reality.get("order_failure_model") if isinstance(reality, dict) else None
        ),
        "portfolio_policy": report.get("portfolio_policy")
        or primary.get("portfolio_policy"),
        "risk_policy": risk_policy,
        "execution_limitations": report.get("execution_limitations"),
        "suspension_or_invalidation_criteria": (
            {
                "schema_version": 1,
                "source": "hypothesis_strategy_risk_contracts",
                "hypothesis_falsification_criteria": list(
                    hypothesis.get("falsification_criteria") or []
                ),
                "edge_invalidation_rule": rule_spec.get("edge_invalidation"),
                "risk_policy_hash": sha256_prefixed(risk_policy),
                "blocked_market_regimes": list(
                    report.get("blocked_live_regimes") or []
                ),
                "operational_permission": False,
            }
            if isinstance(hypothesis, dict)
            and isinstance(rule_spec, dict)
            and isinstance(risk_policy, dict)
            else None
        ),
    }
    if authoritative:
        report_timing = report.get("execution_timing_policy")
        primary_timing = primary.get("execution_timing_policy")
        if (
            isinstance(report_timing, dict)
            and isinstance(primary_timing, dict)
            and report_timing != primary_timing
        ):
            raise StrategyPackageError(
                "strategy_package_execution_timing_contract_mismatch"
            )
        report_portfolio = report.get("portfolio_policy")
        primary_portfolio = primary.get("portfolio_policy")
        if (
            isinstance(report_portfolio, dict)
            and isinstance(primary_portfolio, dict)
            and report_portfolio != primary_portfolio
        ):
            raise StrategyPackageError(
                "strategy_package_portfolio_policy_contract_mismatch"
            )
        risk_hash = report.get("risk_policy_hash")
        if risk_hash != sha256_prefixed(risk_policy):
            raise StrategyPackageError("strategy_package_risk_policy_hash_mismatch")
        return canonical
    legacy = dict(report) | dict(selected)
    return {
        field: value if value is not None else legacy.get(field)
        for field, value in canonical.items()
    }


def build_strategy_research_package(
    report: dict[str, Any],
    *,
    approval: dict[str, Any] | None = None,
    manager: ResearchPathManager | None = None,
) -> dict[str, Any]:
    """Build only from an internally valid authoritative final-selection report."""
    if manager is not None:
        result_reasons = validate_validated_research_result(report, manager=manager)
        if result_reasons:
            raise StrategyPackageError(
                "strategy_package_validated_result_invalid:" + ",".join(result_reasons)
            )
    reasons = validate_final_selection_report(report)
    if reasons:
        raise StrategyPackageError(
            "strategy_package_final_selection_invalid:" + ",".join(reasons)
        )
    if report.get("final_selection_gate_result") != "PASS":
        raise StrategyPackageError("strategy_package_requires_final_selection_pass")
    selection_artifact = report.get("selection_artifact")
    confirmation = report.get("final_holdout_confirmation")
    if not isinstance(selection_artifact, dict) or not isinstance(confirmation, dict):
        raise StrategyPackageError(
            "strategy_package_requires_selection_and_confirmation_evidence"
        )
    selection_binding_reasons = validate_selection_artifact_binding(
        report=report,
        selection_artifact=selection_artifact,
    )
    if selection_binding_reasons:
        raise StrategyPackageError(
            "strategy_package_selection_binding_invalid:"
            + ",".join(selection_binding_reasons)
        )
    confirmation_reasons = validate_confirmation_artifact(
        confirmation,
        selection_artifact=selection_artifact,
    )
    if confirmation_reasons:
        raise StrategyPackageError(
            "strategy_package_confirmation_invalid:" + ",".join(confirmation_reasons)
        )
    if confirmation.get("confirmation_gate_result") != "PASS":
        raise StrategyPackageError(
            "strategy_package_requires_final_holdout_confirmation_pass"
        )
    selected_id = str(report.get("selected_candidate_id") or "")
    if not selected_id:
        raise StrategyPackageError("strategy_package_selected_candidate_missing")
    selected: dict[str, Any] | None
    if (
        manager is not None
        and report.get("selected_candidate_binding_schema_version") == 1
    ):
        selected = resolve_bound_selected_candidate(report, manager=manager)
    else:
        candidates = list(report.get("candidates") or ())
        selected = next(
            (
                item
                for item in candidates
                if str(
                    item.get("parameter_candidate_id") or item.get("candidate_id") or ""
                )
                == selected_id
            ),
            None,
        )
    if selected is None:
        raise StrategyPackageError("strategy_package_selected_candidate_mismatch")
    if (
        selected.get("legacy_execution_authority")
        or selected.get("legacy_vertical_kernel")
        or report.get("legacy_execution_authority")
    ):
        raise StrategyPackageError("strategy_package_rejects_legacy_execution")
    primary_id = str(selected.get("primary_scenario_id") or "")
    scenarios = list(
        selected.get("scenario_results") or selected.get("scenarios") or ()
    )
    primary = (
        next(
            (
                item
                for item in scenarios
                if str(item.get("scenario_id") or "") == primary_id
            ),
            None,
        )
        if primary_id
        else None
    )
    evidence_sources: list[object] = []
    if isinstance(primary, dict):
        evidence_sources.extend(
            (
                primary.get("execution_evidence"),
                primary.get("validation_execution_event_summary"),
            )
        )
        for split_name in ("validation_resource_usage", "final_holdout_resource_usage"):
            usage = primary.get(split_name)
            if isinstance(usage, dict):
                evidence_sources.append(usage.get("execution_evidence"))
    evidence_sources.extend(
        (
            selected.get("execution_evidence"),
            selected.get("validation_execution_event_summary"),
            report.get("execution_evidence"),
        )
    )
    evidence = dict(
        next((item for item in evidence_sources if isinstance(item, dict) and item), {})
    )
    evidence_schema_version = int(
        evidence.get("execution_evidence_schema_version") or 1
    )
    if evidence_schema_version not in {1, 2, 3}:
        raise StrategyPackageError(
            "strategy_package_execution_evidence_schema_unsupported:"
            + str(evidence_schema_version)
        )
    if evidence_schema_version != 3:
        raise StrategyPackageError(
            "strategy_package_execution_evidence_requires_schema_version:3"
        )
    required = {
        1: REQUIRED_FIELDS,
        2: REQUIRED_FIELDS_V2,
        3: REQUIRED_FIELDS_V3,
    }[evidence_schema_version]
    missing = sorted(required - set(evidence))
    if missing:
        raise StrategyPackageError(
            "strategy_package_missing_execution_evidence:" + ",".join(missing)
        )
    if evidence.get("timing_invariant_status") != "PASS":
        raise StrategyPackageError("strategy_package_timing_invariant_failure")
    if evidence.get("decision_timeline_invariant_status") != "PASS":
        raise StrategyPackageError(
            "strategy_package_decision_timeline_invariant_failure"
        )
    if evidence.get("causal_timeline_validator") != CAUSAL_TIMELINE_VALIDATOR:
        raise StrategyPackageError("strategy_package_causal_validator_mismatch")
    if evidence.get("market_knowledge_time_policy") != MARKET_KNOWLEDGE_TIME_POLICY:
        raise StrategyPackageError("strategy_package_knowledge_time_policy_mismatch")
    if int(evidence.get("market_knowledge_time_assumption_count") or 0) != 0:
        raise StrategyPackageError(
            "strategy_package_knowledge_time_assumption_not_allowed"
        )
    if evidence.get("declared_execution_model_hash") != evidence.get(
        "executed_execution_model_hash"
    ):
        raise StrategyPackageError("strategy_package_execution_model_mismatch")
    declared_timing = evidence.get(
        "declared_execution_timing_policy_hash",
        evidence.get("declared_execution_timing_hash"),
    )
    executed_timing = evidence.get(
        "executed_execution_timing_policy_hash",
        evidence.get("executed_execution_timing_hash"),
    )
    if (
        int(evidence.get("execution_evidence_schema_version") or 1) >= 2
        and declared_timing != executed_timing
    ):
        raise StrategyPackageError("strategy_package_execution_timing_mismatch")
    if not isinstance(primary, dict):
        raise StrategyPackageError("strategy_package_primary_scenario_missing")
    merged = dict(report) | dict(selected)
    primary_contract = primary.get("compiled_strategy_contract")
    primary_contract_hash = primary.get("compiled_strategy_contract_hash")
    if not isinstance(primary_contract, dict) or not isinstance(
        primary_contract_hash, str
    ):
        raise StrategyPackageError(
            "strategy_package_primary_scenario_compiled_contract_missing"
        )
    merged["compiled_strategy_contract"] = primary_contract
    merged["compiled_strategy_contract_hash"] = primary_contract_hash
    merged.update(
        _canonical_package_contract(
            report=report,
            selected=selected,
            primary=primary,
            compiled_payload=primary_contract,
            authoritative=manager is not None,
        )
    )
    absent = [field for field in _CONTRACT_FIELDS if merged.get(field) is None]
    if absent:
        raise StrategyPackageError(
            "strategy_package_missing_required_contract_field:" + ",".join(absent)
        )
    stable_projection = {
        "candidate_id": selected_id,
        "primary_scenario_id": primary_id,
        "strategy_spec_hash": merged["strategy_spec_hash"],
        "decision_contract_version": merged["decision_contract_version"],
        "execution_evidence": evidence,
    }
    candidate_evidence_hash = sha256_prefixed(stable_projection)
    recorded_report_hash = report.get("content_hash")
    if not isinstance(recorded_report_hash, str):
        raise StrategyPackageError(
            "strategy_package_source_report_content_hash_missing"
        )
    actual_report_hash = sha256_prefixed(report_content_hash_payload(report))
    if recorded_report_hash != actual_report_hash:
        raise StrategyPackageError(
            "strategy_package_source_report_content_hash_mismatch"
        )
    missing_bindings = [
        field
        for field in _EVIDENCE_BINDING_FIELDS
        if not str(merged.get(field) or "").startswith("sha256:")
    ]
    if missing_bindings:
        raise StrategyPackageError(
            "strategy_package_missing_evidence_binding:" + ",".join(missing_bindings)
        )
    compiled_payload = merged.get("compiled_strategy_contract")
    if not isinstance(compiled_payload, dict):
        raise StrategyPackageError("strategy_package_compiled_contract_payload_missing")
    try:
        hydrated = validate_compiled_strategy_contract(
            dict(compiled_payload),
            expected_compiled_hash=merged["compiled_strategy_contract_hash"],
            expected_registry_hash=merged["strategy_registry_hash"],
            expected_plugin_hash=merged["strategy_plugin_contract_hash"],
        )
    except StrategyCompilationError as exc:
        raise StrategyPackageError(
            f"strategy_package_compiled_contract_invalid:{exc}"
        ) from exc
    capability = compiled_payload.get("capability_contract")
    if (
        not isinstance(capability, dict)
        or hydrated.capability_contract_hash != merged["capability_contract_hash"]
        or (
            merged.get("capability_contract") is not None
            and merged["capability_contract"] != capability
        )
    ):
        raise StrategyPackageError("strategy_package_capability_contract_hash_mismatch")
    effective_hash = merged.get("effective_strategy_parameters_hash")
    if effective_hash != hydrated.materialized_parameters_hash:
        raise StrategyPackageError("strategy_package_effective_parameter_hash_mismatch")
    effective_payload = merged.get("effective_strategy_parameters")
    if (
        effective_payload is not None
        and sha256_prefixed(effective_payload) != effective_hash
    ):
        raise StrategyPackageError(
            "strategy_package_effective_parameter_payload_hash_mismatch"
        )
    for scenario in scenarios:
        scenario_payload = (
            scenario.get("compiled_strategy_contract")
            if isinstance(scenario, dict)
            else None
        )
        scenario_hash = (
            scenario.get("compiled_strategy_contract_hash")
            if isinstance(scenario, dict)
            else None
        )
        if not isinstance(scenario_payload, dict) or not isinstance(scenario_hash, str):
            raise StrategyPackageError(
                "strategy_package_scenario_compiled_contract_missing"
            )
        try:
            scenario_contract = validate_compiled_strategy_contract(
                scenario_payload,
                expected_compiled_hash=scenario_hash,
                expected_strategy_name=hydrated.strategy_name,
                expected_strategy_version=hydrated.strategy_version,
                expected_registry_hash=hydrated.strategy_registry_hash,
                expected_plugin_hash=hydrated.strategy_plugin_contract_hash,
            )
        except StrategyCompilationError as exc:
            raise StrategyPackageError(
                f"strategy_package_scenario_identity_mismatch:{exc}"
            ) from exc
        if (
            scenario_contract.capability_contract_hash
            != hydrated.capability_contract_hash
        ):
            raise StrategyPackageError("strategy_package_scenario_capability_mismatch")
    decision_sources = {
        value
        for value in (
            evidence.get("decision_stream_hash"),
            selected.get("decision_stream_hash"),
        )
        if value is not None
    }
    metrics_sources = {
        value
        for value in (selected.get("metrics_hash"), evidence.get("metrics_hash"))
        if value is not None
    }
    if len(decision_sources) > 1:
        raise StrategyPackageError("strategy_package_decision_hash_mismatch")
    if len(metrics_sources) > 1:
        raise StrategyPackageError("strategy_package_metrics_hash_mismatch")
    decision_hash = next(iter(decision_sources), None)
    metrics_hash = next(iter(metrics_sources), None)
    if not str(decision_hash or "").startswith("sha256:") or not str(
        metrics_hash or ""
    ).startswith("sha256:"):
        raise StrategyPackageError("strategy_package_missing_decision_or_metrics_hash")
    for payload_key, hash_key in (
        ("decision_stream", "decision_stream_hash"),
        ("execution_request_stream", "execution_request_stream_hash"),
        ("execution_fill_stream", "execution_fill_stream_hash"),
        ("ledger_stream", "ledger_stream_hash"),
    ):
        stream = primary.get(payload_key, selected.get(payload_key))
        expected = evidence.get(
            hash_key,
            evidence.get("portfolio_ledger_hash")
            if hash_key == "ledger_stream_hash"
            else None,
        )
        if stream is not None and sha256_prefixed(stream) != expected:
            raise StrategyPackageError(f"strategy_package_{payload_key}_tampered")
    semantic_contract = _complete_semantic_contract(
        report=report,
        merged=merged,
        primary=primary,
        confirmation=confirmation,
        compiled_payload=compiled_payload,
        selected_id=selected_id,
        strategy_name=hydrated.strategy_name,
        strategy_version=hydrated.strategy_version,
    )
    package = {
        "schema_version": 5,
        "selected_candidate_id": selected_id,
        "authoritative": manager is not None,
        "package_authority_status": (
            "CANONICAL_REGISTRIES_VERIFIED"
            if manager is not None
            else "DECLARED_PATH_ONLY"
        ),
        "package_authority_result": ("PASS" if manager is not None else "UNVERIFIED"),
        **{field: merged[field] for field in _CONTRACT_FIELDS},
        **semantic_contract,
        "execution_timing_hash": executed_timing,
        "execution_timing_stream_hash": evidence.get(
            "execution_timing_stream_hash",
            evidence.get("executed_execution_timing_hash"),
        ),
        "execution_model_hash": evidence["executed_execution_model_hash"],
        "request_stream_hash": evidence["execution_request_stream_hash"],
        "fill_stream_hash": evidence["execution_fill_stream_hash"],
        "ledger_stream_hash": evidence.get(
            "ledger_stream_hash", evidence.get("portfolio_ledger_hash")
        ),
        "decision_stream_hash": decision_hash,
        "metrics_hash": metrics_hash,
        **{field: merged[field] for field in _EVIDENCE_BINDING_FIELDS},
        "capability_contract": merged.get("capability_contract")
        or dict(merged.get("compiled_strategy_contract") or {}).get(
            "capability_contract"
        ),
        "validation_result": "PASS",
        "selection_artifact_hash": selection_artifact["content_hash"],
        "final_holdout_confirmation_hash": confirmation["content_hash"],
        "source_report_content_hash": recorded_report_hash,
        "selected_candidate_evidence_hash": candidate_evidence_hash,
    }
    point_in_time_fields = (
        "point_in_time_decision_stream_hash",
        "point_in_time_authority_binding_hash",
        "point_in_time_evidence_content_hash",
    )
    point_in_time_bindings = {
        field: evidence.get(field)
        for field in point_in_time_fields
        if evidence.get(field) is not None
    }
    if point_in_time_bindings and set(point_in_time_bindings) != set(
        point_in_time_fields
    ):
        raise StrategyPackageError("strategy_package_point_in_time_binding_incomplete")
    if any(not is_sha256_hash(value) for value in point_in_time_bindings.values()):
        raise StrategyPackageError("strategy_package_point_in_time_hash_invalid")
    if point_in_time_bindings:
        lineage = report.get("lineage")
        split_evidence = (
            lineage.get("dataset_split_evidence") if isinstance(lineage, dict) else None
        )
        validation_split = (
            split_evidence.get("validation")
            if isinstance(split_evidence, dict)
            else None
        )
        if not isinstance(validation_split, dict):
            raise StrategyPackageError(
                "strategy_package_point_in_time_validation_lineage_missing"
            )
        mismatches = [
            field
            for field, value in point_in_time_bindings.items()
            if validation_split.get(field) != value
        ]
        if mismatches:
            raise StrategyPackageError(
                "strategy_package_point_in_time_lineage_mismatch:"
                + ",".join(sorted(mismatches))
            )
    package.update(point_in_time_bindings)
    if report.get("validation_admission_binding_schema_version") == 1:
        package.update(
            {
                "validation_admission_binding_schema_version": 1,
                "knowledge_registry_path": report.get("knowledge_registry_path"),
                "validation_admission_record_hash": report.get(
                    "validation_admission_record_hash"
                ),
                "validation_admission_row_hash": report.get(
                    "validation_admission_row_hash"
                ),
            }
        )
    forbidden = {
        "account",
        "credential",
        "private_api",
        "submit_order",
        "api_key",
        "api_secret",
        "order_submission",
    }

    def contains_forbidden(value: object) -> bool:
        if isinstance(value, dict):
            return any(
                str(key).lower() in forbidden or contains_forbidden(item)
                for key, item in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(contains_forbidden(item) for item in value)
        return False

    if contains_forbidden(package):
        raise StrategyPackageError("strategy_package_operational_field_forbidden")
    approval_reasons = validate_strategy_approval(
        approval,
        source_report_hash=recorded_report_hash,
        selected_candidate_id=selected_id,
        final_holdout_confirmation_hash=str(confirmation["content_hash"]),
        hypothesis_id=str(report.get("hypothesis_id") or ""),
        hypothesis_version=str(report.get("hypothesis_version") or ""),
        hypothesis_contract_hash=str(report.get("hypothesis_contract_hash") or ""),
        strategy_name=str(hydrated.strategy_name),
        strategy_version=str(hydrated.strategy_version),
        strategy_plugin_contract_hash=str(merged["strategy_plugin_contract_hash"]),
        effective_strategy_parameters_hash=str(
            merged["effective_strategy_parameters_hash"]
        ),
        expected_registry_path=(
            governance_registry_path(manager) if manager is not None else None
        ),
        manager=manager,
    )
    if approval_reasons:
        raise StrategyPackageError(
            "strategy_package_research_approval_invalid:" + ",".join(approval_reasons)
        )
    if not isinstance(approval, dict):
        raise StrategyPackageError(
            "strategy_package_research_approval_invalid:strategy_approval_missing"
        )
    registry_reasons = validate_experiment_registry_binding(
        report=confirmation,
        require_complete=True,
        expected_registry_path=(
            experiment_registry_path(manager=manager) if manager is not None else None
        ),
    )
    if registry_reasons:
        raise StrategyPackageError(
            "strategy_package_confirmation_registry_invalid:"
            + ",".join(registry_reasons)
        )
    approved_at = str(approval.get("approved_at") or "").strip()
    if not approved_at:
        raise StrategyPackageError(
            "strategy_package_research_approval_timestamp_missing"
        )
    package["approval_record"] = {
        "reviewer_id": approval["reviewer_id"],
        "rationale": approval["rationale"],
        "approved_at": approved_at,
        "approval_hash": approval["content_hash"],
        "review_row_hash": approval["review_row_hash"],
        "transition_row_hash": approval["transition_row_hash"],
    }
    package["research_approval_hash"] = approval["content_hash"]
    package["research_approval_review_row_hash"] = approval["review_row_hash"]
    package["research_approval_transition_row_hash"] = approval["transition_row_hash"]
    package["approved_hypothesis_id"] = approval["hypothesis_id"]
    package["approved_hypothesis_version"] = approval["hypothesis_version"]
    package["approved_hypothesis_contract_hash"] = approval["hypothesis_contract_hash"]
    package["content_hash"] = sha256_prefixed(package)
    return package
