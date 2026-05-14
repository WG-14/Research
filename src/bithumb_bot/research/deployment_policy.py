from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEPLOYMENT_TIERS = {
    "research_only",
    "paper_candidate",
    "live_dry_run_candidate",
    "small_live_candidate",
}
PRODUCTION_BOUND_TIERS = {
    "paper_candidate",
    "live_dry_run_candidate",
    "small_live_candidate",
}
PROFILE_MODE_TO_TIER = {
    "paper": "paper_candidate",
    "live_dry_run": "live_dry_run_candidate",
    "small_live": "small_live_candidate",
}


@dataclass(frozen=True)
class DeploymentCalibrationPolicyResult:
    target: str
    production_bound: bool
    required: bool
    status: str
    reasons: tuple[str, ...]
    artifact_hash: str | None
    artifact_hashes: tuple[str, ...]
    policy_source: str = "repo_production_calibration_policy_v1"
    operator_next_step: str = "none"

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "target": self.target,
            "production_bound": self.production_bound,
            "required": self.required,
            "status": self.status,
            "reasons": list(self.reasons),
            "artifact_hash": self.artifact_hash,
            "artifact_hashes": list(self.artifact_hashes),
            "policy_source": self.policy_source,
            "operator_next_step": self.operator_next_step,
        }
        return payload


def normalize_deployment_tier(value: object | None) -> str:
    target = str(value or "research_only").strip().lower()
    if target not in DEPLOYMENT_TIERS:
        return "research_only"
    return target


def deployment_tier_for_profile_mode(mode: object) -> str:
    return PROFILE_MODE_TO_TIER.get(str(mode or "").strip().lower(), "research_only")


def is_production_bound_target(target: object | None) -> bool:
    return normalize_deployment_tier(target) in PRODUCTION_BOUND_TIERS


def validate_production_calibration_policy(
    payload: dict[str, Any],
    *,
    target: object | None = None,
) -> DeploymentCalibrationPolicyResult:
    profile = payload.get("candidate_profile") if isinstance(payload.get("candidate_profile"), dict) else {}
    source = {**profile, **payload}
    normalized_target = normalize_deployment_tier(target or source.get("deployment_tier") or source.get("promotion_target"))
    production_bound = normalized_target in PRODUCTION_BOUND_TIERS
    if not production_bound:
        return DeploymentCalibrationPolicyResult(
            target=normalized_target,
            production_bound=False,
            required=False,
            status="NOT_REQUIRED",
            reasons=(),
            artifact_hash=None,
            artifact_hashes=(),
        )

    reasons: list[str] = []
    execution_model_source = str(source.get("execution_model_source") or "").strip()
    execution_model = source.get("execution_model")
    if execution_model_source == "legacy_cost_model" or not isinstance(execution_model, dict):
        reasons.append("production_execution_model_required")
    reasons.extend(_production_cost_assumption_reasons(source))
    if source.get("execution_calibration_required") is not True:
        reasons.append("production_execution_calibration_required")
    if str(source.get("execution_calibration_strictness") or "").strip().lower() != "fail":
        reasons.append("production_execution_calibration_strictness_must_be_fail")

    gate = source.get("execution_calibration_gate")
    if not isinstance(gate, dict):
        reasons.append("production_execution_calibration_gate_missing")
        hashes: tuple[str, ...] = ()
    else:
        if gate.get("status") != "PASS":
            reasons.append("production_execution_calibration_gate_not_passed")
            reasons.extend(str(reason) for reason in gate.get("reasons") or ())
        hashes = _calibration_hashes(gate)
        if not hashes:
            reasons.append("production_execution_calibration_hash_missing")
        elif len(hashes) > 1:
            reasons.append("production_execution_calibration_hash_inconsistent")
        _extend_scenario_gate_reasons(gate, reasons)

    if source.get("execution_reality_summary") is not None:
        summary = source.get("execution_reality_summary")
        if not isinstance(summary, dict):
            reasons.append("production_execution_reality_summary_missing")
        elif summary.get("execution_reality_gate_status") == "FAIL":
            reasons.extend(str(reason) for reason in summary.get("execution_reality_gate_reasons") or ())

    unique_reasons = tuple(sorted(set(reasons)))
    return DeploymentCalibrationPolicyResult(
        target=normalized_target,
        production_bound=True,
        required=True,
        status="PASS" if not unique_reasons else "FAIL",
        reasons=unique_reasons,
        artifact_hash=hashes[0] if len(hashes) == 1 else None,
        artifact_hashes=hashes,
        operator_next_step=(
            "none"
            if not unique_reasons
            else "regenerate_execution_quality_calibration_and_rerun_research_backtest_with_execution_calibration"
        ),
    )


def _calibration_hashes(gate: dict[str, Any]) -> tuple[str, ...]:
    values: set[str] = set()
    for key in ("artifact_hash", "execution_calibration_artifact_hash"):
        value = gate.get(key)
        if isinstance(value, str) and value.startswith("sha256:"):
            values.add(value)
    raw_hashes = gate.get("artifact_hashes")
    if isinstance(raw_hashes, list):
        values.update(str(value) for value in raw_hashes if str(value).startswith("sha256:"))
    for scenario_gate in gate.get("scenario_gates") or ():
        if isinstance(scenario_gate, dict):
            value = scenario_gate.get("artifact_hash")
            if isinstance(value, str) and value.startswith("sha256:"):
                values.add(value)
    return tuple(sorted(values))


def _extend_scenario_gate_reasons(gate: dict[str, Any], reasons: list[str]) -> None:
    scenario_gates = gate.get("scenario_gates")
    if not isinstance(scenario_gates, list) or not scenario_gates:
        reasons.append("production_execution_calibration_scenario_gate_missing")
        return
    for scenario_gate in scenario_gates:
        if not isinstance(scenario_gate, dict):
            reasons.append("production_execution_calibration_scenario_gate_invalid")
            continue
        if scenario_gate.get("content_hash_present") is not True:
            reasons.append("production_execution_calibration_hash_missing")
        if scenario_gate.get("quality_gate_status") != "PASS":
            reasons.append("execution_calibration_quality_gate_not_passed")
        sample_count = int(scenario_gate.get("sample_count") or 0)
        min_sample = scenario_gate.get("min_sample_count")
        if min_sample is not None and sample_count < int(min_sample):
            reasons.append("execution_calibration_sample_count_below_required")
        if str(scenario_gate.get("market") or "") != str(scenario_gate.get("expected_market") or ""):
            reasons.append("execution_calibration_market_mismatch")
        if str(scenario_gate.get("interval") or "") != str(scenario_gate.get("expected_interval") or ""):
            reasons.append("execution_calibration_interval_mismatch")
        expected_policy = scenario_gate.get("expected_fill_reference_policy")
        artifact_policy = scenario_gate.get("artifact_fill_reference_policy")
        if artifact_policy is not None and str(artifact_policy) != str(expected_policy):
            reasons.append("execution_calibration_fill_reference_policy_mismatch")


def _production_cost_assumption_reasons(source: dict[str, Any]) -> list[str]:
    execution_model_source = str(source.get("execution_model_source") or "").strip()
    execution_model = source.get("execution_model")
    if execution_model_source == "legacy_cost_model" or not isinstance(execution_model, dict):
        return ["production_legacy_cost_model_not_promotable"]
    contract = source.get("cost_assumption_contract")
    if not isinstance(contract, dict):
        contract = execution_model
    scenarios = contract.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        return ["production_base_cost_assumption_required"]
    if all(str(item.get("scenario_role") or "").strip() == "stress" for item in scenarios if isinstance(item, dict)):
        stress_only = True
    else:
        stress_only = False
    reasons: list[str] = ["production_stress_only_cost_model_not_promotable"] if stress_only else []
    base_assumptions: list[dict[str, Any]] = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        if str(scenario.get("scenario_role") or "").strip() != "base":
            continue
        assumption = scenario.get("cost_assumption")
        if isinstance(assumption, dict):
            base_assumptions.append(assumption)
        else:
            base_assumptions.append(scenario)
    if not base_assumptions:
        reasons.append("production_base_cost_assumption_required")
    for assumption in base_assumptions:
        if not str(assumption.get("label") or assumption.get("cost_assumption_label") or "").strip():
            reasons.append("production_cost_assumption_label_required")
        fee_source = str(assumption.get("fee_source") or "").strip()
        slippage_source = str(assumption.get("slippage_source") or "").strip()
        if not fee_source or fee_source in {"legacy_cost_model", "stress_assumption"}:
            reasons.append("production_cost_assumption_source_required")
        if not slippage_source:
            reasons.append("production_cost_assumption_source_required")
        if str(assumption.get("role") or assumption.get("scenario_role") or "").strip() == "stress":
            reasons.append("production_stress_only_cost_model_not_promotable")
        if assumption.get("promotable_as_base") is not True:
            reasons.append("production_stress_only_cost_model_not_promotable")
    return reasons
