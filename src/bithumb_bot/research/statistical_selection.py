from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from bithumb_bot.paths import PathManager, PathPolicyError
from bithumb_bot.storage_io import write_json_atomic

from .deployment_policy import is_production_bound_target
from .experiment_manifest import ExperimentManifest, StatisticalSelectionContract
from .hashing import content_hash_payload, sha256_prefixed


STATISTICAL_SELECTION_EVIDENCE_SCHEMA_VERSION = 1


def statistical_validation_required(manifest_or_payload: ExperimentManifest | dict[str, Any]) -> bool:
    if isinstance(manifest_or_payload, ExperimentManifest):
        if manifest_or_payload.statistical_validation is not None:
            return bool(manifest_or_payload.statistical_validation.required_for_promotion)
        return is_production_bound_target(manifest_or_payload.deployment_tier)
    contract = manifest_or_payload.get("statistical_validation_contract")
    if isinstance(contract, dict) and contract.get("required_for_promotion") is not None:
        return bool(contract.get("required_for_promotion"))
    return is_production_bound_target(manifest_or_payload.get("deployment_tier"))


def selection_universe_hash(
    *,
    manifest_hash: str,
    dataset_content_hash: str,
    dataset_quality_hash: str | None,
    experiment_family_id: str | None,
    hypothesis_id: str | None,
    hypothesis_status: str | None,
    candidates: list[dict[str, Any]],
    required_scenario_ids: list[str],
    primary_metric_source: str,
    benchmark: str,
    statistical_validation_contract: dict[str, Any],
) -> str:
    return sha256_prefixed(
        {
            "manifest_hash": manifest_hash,
            "dataset_content_hash": dataset_content_hash,
            "dataset_quality_hash": dataset_quality_hash,
            "experiment_family_id": experiment_family_id,
            "hypothesis_id": hypothesis_id,
            "hypothesis_status": hypothesis_status,
            "candidates": [
                {
                    "candidate_id": str(candidate.get("parameter_candidate_id") or ""),
                    "parameter_values": candidate.get("parameter_values") or {},
                }
                for candidate in sorted(candidates, key=lambda item: str(item.get("parameter_candidate_id") or ""))
            ],
            "required_scenario_ids": sorted(str(item) for item in required_scenario_ids),
            "primary_metric_source": primary_metric_source,
            "benchmark": benchmark,
            "statistical_validation_contract": statistical_validation_contract,
        }
    )


def build_statistical_selection_evidence(
    *,
    manifest: ExperimentManifest,
    candidates: list[dict[str, Any]],
    manifest_hash: str,
    dataset_content_hash: str,
    dataset_quality_hash: str | None,
    experiment_family_id: str | None,
    hypothesis_id: str | None,
    hypothesis_status: str | None,
    selection_hash: str,
    search_budget: int,
    parameter_grid_size: int,
    attempt_index: int,
    holdout_reuse_count: int,
    dataset_reuse_policy: str,
) -> dict[str, Any] | None:
    contract = manifest.statistical_validation
    if contract is None:
        return None
    contract_payload = contract.as_dict()
    primary_metric_source = "validation_metrics"
    metric_values = _candidate_metric_values(candidates, contract)
    p_value, seed = _metric_centered_max_bootstrap_p_value(
        metric_values=metric_values,
        n_bootstrap=contract.bootstrap.n_bootstrap,
        selection_hash=selection_hash,
    )
    gate_reasons = _statistical_gate_fail_reasons(
        contract=contract,
        p_value=p_value,
        attempt_index=attempt_index,
        holdout_reuse_count=holdout_reuse_count,
        metric_values=metric_values,
    )
    payload: dict[str, Any] = {
        "artifact_type": "statistical_selection_evidence",
        "schema_version": STATISTICAL_SELECTION_EVIDENCE_SCHEMA_VERSION,
        "experiment_id": manifest.experiment_id,
        "experiment_family_id": experiment_family_id,
        "hypothesis_id": hypothesis_id,
        "manifest_hash": manifest_hash,
        "dataset_content_hash": dataset_content_hash,
        "dataset_quality_hash": dataset_quality_hash,
        "selection_universe_hash": selection_hash,
        "candidate_count": len(candidates),
        "search_budget": search_budget,
        "parameter_grid_size": parameter_grid_size,
        "attempt_index": attempt_index,
        "holdout_reuse_count": holdout_reuse_count,
        "dataset_reuse_policy": dataset_reuse_policy,
        "benchmark": contract.benchmark,
        "primary_metric": contract.primary_metric,
        "primary_metric_source": primary_metric_source,
        "bootstrap_method": contract.bootstrap.method,
        "n_bootstrap": contract.bootstrap.n_bootstrap,
        "block_length": None,
        "block_length_policy": contract.bootstrap.block_length_policy,
        "seed": seed,
        "effective_trial_count": len(metric_values) * max(1, attempt_index) * max(1, holdout_reuse_count + 1),
        "white_reality_check_p_value": p_value,
        "statistical_gate_result": "FAIL" if gate_reasons else "PASS",
        "gate_fail_reasons": gate_reasons,
        "limitations": _limitations(contract),
        "statistical_validation_contract": contract_payload,
    }
    payload["content_hash"] = sha256_prefixed(content_hash_payload(payload))
    return payload


def write_statistical_selection_evidence(
    *,
    manager: PathManager,
    experiment_id: str,
    evidence: dict[str, Any],
) -> Path:
    path = manager.data_dir() / "reports" / "research" / experiment_id / "statistical_selection_evidence.json"
    _ensure_research_output_path_allowed(manager, path)
    write_json_atomic(path, evidence)
    return path


def validate_statistical_evidence_for_candidate(
    *,
    candidate: dict[str, Any],
    report: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> list[str]:
    reasons: list[str] = []
    required = bool(candidate.get("statistical_validation_required")) or is_production_bound_target(
        candidate.get("deployment_tier") or report.get("deployment_tier")
    )
    contract = candidate.get("statistical_validation_contract") or report.get("statistical_validation_contract")
    if required and not isinstance(contract, dict):
        reasons.append("statistical_contract_missing")
    if not required:
        return reasons
    if not isinstance(evidence, dict):
        reasons.append("statistical_evidence_missing")
        return reasons
    expected_hash = str(candidate.get("statistical_evidence_hash") or report.get("statistical_evidence_hash") or "")
    if not expected_hash.startswith("sha256:"):
        reasons.append("statistical_evidence_hash_missing")
    actual_hash = sha256_prefixed(content_hash_payload({k: v for k, v in evidence.items() if k != "content_hash"}))
    embedded_hash = str(evidence.get("content_hash") or "")
    if expected_hash.startswith("sha256:") and actual_hash != expected_hash:
        reasons.append("statistical_evidence_hash_mismatch")
    if embedded_hash != actual_hash:
        reasons.append("statistical_evidence_hash_mismatch")
    expected_universe = str(candidate.get("selection_universe_hash") or report.get("selection_universe_hash") or "")
    if not expected_universe.startswith("sha256:"):
        reasons.append("selection_universe_hash_missing")
    elif str(evidence.get("selection_universe_hash") or "") != expected_universe:
        reasons.append("selection_universe_hash_mismatch")
    for field in ("manifest_hash", "dataset_content_hash", "dataset_quality_hash"):
        expected = candidate.get(field) or report.get(field)
        actual = evidence.get(field)
        if expected or actual:
            if str(expected or "") != str(actual or ""):
                reasons.append("selection_universe_hash_mismatch")
                break
    p_value = evidence.get("white_reality_check_p_value")
    if p_value is None:
        reasons.append("reality_check_p_value_missing")
    elif _as_float(p_value) is None:
        reasons.append("reality_check_p_value_missing")
    if evidence.get("effective_trial_count") is None:
        reasons.append("effective_trial_count_missing")
    if evidence.get("statistical_gate_result") != "PASS":
        gate_reasons = [str(item) for item in evidence.get("gate_fail_reasons") or []]
        reasons.extend(gate_reasons or ["reality_check_p_value_failed"])
    if isinstance(contract, dict):
        gates = contract.get("gates")
        if isinstance(gates, dict):
            if gates.get("max_spa_p_value") is not None and evidence.get("spa_p_value") is None:
                reasons.append("spa_p_value_missing")
            if gates.get("min_deflated_sharpe_probability") is not None and evidence.get("deflated_sharpe_probability") is None:
                reasons.append("deflated_sharpe_missing")
    return sorted(set(reasons))


def _candidate_metric_values(
    candidates: list[dict[str, Any]],
    contract: StatisticalSelectionContract,
) -> list[float]:
    values: list[float] = []
    for candidate in candidates:
        metrics = candidate.get("validation_metrics")
        if not isinstance(metrics, dict):
            continue
        value = _metric_value(metrics, contract.primary_metric, contract.benchmark)
        if value is not None:
            values.append(value)
    return values


def _metric_value(metrics: dict[str, Any], primary_metric: str, benchmark: str) -> float | None:
    if primary_metric in {"net_excess_return", "return_pct"}:
        raw = _as_float(metrics.get("return_pct"))
    elif primary_metric == "sharpe_like":
        raw = _as_float(metrics.get("sharpe_like"))
        if raw is None:
            raw = _as_float(metrics.get("return_pct"))
    else:
        raw = None
    if raw is None:
        return None
    benchmark_value = 0.0
    if benchmark in {"cash", "buy_and_hold", "configured"}:
        return raw - benchmark_value
    return raw


def _metric_centered_max_bootstrap_p_value(
    *,
    metric_values: list[float],
    n_bootstrap: int,
    selection_hash: str,
) -> tuple[float | None, int | None]:
    if not metric_values:
        return None, None
    observed = max(metric_values)
    if observed <= 0.0:
        return 1.0, _seed_from_hash(selection_hash)
    mean_value = sum(metric_values) / len(metric_values)
    centered = [value - mean_value for value in metric_values]
    seed = _seed_from_hash(selection_hash)
    rng = random.Random(seed)
    exceed_count = 0
    sample_size = len(centered)
    for _ in range(n_bootstrap):
        sample_max = max(centered[rng.randrange(sample_size)] for _ in range(sample_size))
        if sample_max >= observed:
            exceed_count += 1
    return round((exceed_count + 1) / (n_bootstrap + 1), 12), seed


def _statistical_gate_fail_reasons(
    *,
    contract: StatisticalSelectionContract,
    p_value: float | None,
    attempt_index: int,
    holdout_reuse_count: int,
    metric_values: list[float],
) -> list[str]:
    reasons: list[str] = []
    if not metric_values:
        reasons.append("effective_trial_count_missing")
    if p_value is None:
        reasons.append("reality_check_p_value_missing")
    elif p_value > contract.gates.max_reality_check_p_value:
        reasons.append("reality_check_p_value_failed")
    if holdout_reuse_count > contract.gates.max_holdout_reuse_count:
        reasons.append("holdout_reuse_budget_exceeded")
    if attempt_index > contract.gates.max_attempt_index_without_new_hypothesis:
        reasons.append("attempt_budget_exceeded")
    if contract.gates.max_spa_p_value is not None:
        reasons.append("spa_p_value_missing")
    if contract.gates.min_deflated_sharpe_probability is not None:
        reasons.append("deflated_sharpe_missing")
    return sorted(set(reasons))


def _limitations(contract: StatisticalSelectionContract) -> list[str]:
    limitations = [
        "metric_summary_bootstrap_not_trade_or_bar_return_bootstrap",
        "white_reality_check_equivalent_uses_centered_candidate_primary_metric_distribution",
    ]
    if contract.gates.max_spa_p_value is None:
        limitations.append("spa_not_implemented")
    if contract.gates.min_deflated_sharpe_probability is None:
        limitations.append("deflated_sharpe_not_implemented")
    return limitations


def _seed_from_hash(value: str) -> int:
    text = value.split("sha256:", 1)[-1]
    return int(text[:16], 16)


def _as_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed


def _ensure_research_output_path_allowed(manager: PathManager, path: Path) -> None:
    project_root = manager.project_root.resolve()
    resolved = path.resolve()
    if PathManager._is_within(resolved, project_root):
        raise PathPolicyError(f"research output path must be outside repository: {resolved}")
