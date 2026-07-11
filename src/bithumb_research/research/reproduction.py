from __future__ import annotations

"""Stable, fail-closed evidence used to reproduce a research backtest.

Reports deliberately contain operational observations (timestamps, absolute
paths, process ids, and timing/memory measurements).  Those observations are
useful for diagnostics but are not deterministic research evidence, so this
module projects a report into an explicitly ordered, stable evidence view.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import json

from bithumb_research.storage_io import write_json_atomic

from .experiment_manifest import ExperimentManifest
from .hashing import content_hash_payload, sha256_prefixed


REPRODUCTION_FINGERPRINT_SCHEMA_VERSION = 1
REPRODUCTION_RECEIPT_SCHEMA_VERSION = 1


class ReproductionContractError(ValueError):
    """A receipt or report does not provide sufficient reproducibility evidence."""


@dataclass(frozen=True, slots=True)
class ResearchReproductionFingerprint:
    schema_version: int
    manifest_hash: str
    research_classification: str
    dataset_fingerprint: str
    dataset_split_hashes: tuple[dict[str, object], ...]
    strategy_contract_hashes: tuple[str, ...]
    execution_assumption_hashes: tuple[dict[str, str], ...]
    candidate_fingerprints: tuple[dict[str, object], ...]
    final_selection: dict[str, object]
    stable_fingerprint_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_hash": self.manifest_hash,
            "research_classification": self.research_classification,
            "dataset_fingerprint": self.dataset_fingerprint,
            "dataset_split_hashes": [dict(item) for item in self.dataset_split_hashes],
            "strategy_contract_hashes": list(self.strategy_contract_hashes),
            "execution_assumption_hashes": [dict(item) for item in self.execution_assumption_hashes],
            "candidate_fingerprints": [dict(item) for item in self.candidate_fingerprints],
            "final_selection": dict(self.final_selection),
            "stable_fingerprint_hash": self.stable_fingerprint_hash,
        }


@dataclass(frozen=True, slots=True)
class ResearchReproductionComparison:
    status: str
    expected_fingerprint_hash: str
    actual_fingerprint_hash: str
    mismatches: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "expected_fingerprint_hash": self.expected_fingerprint_hash,
            "actual_fingerprint_hash": self.actual_fingerprint_hash,
            "mismatches": [dict(item) for item in self.mismatches],
        }


def build_reproduction_fingerprint(
    report: Mapping[str, Any],
    *,
    manifest: ExperimentManifest,
) -> ResearchReproductionFingerprint:
    """Build stable evidence from a completed report or reject it.

    The required hashes are intentionally broader than a single report hash:
    they bind data, contracts, each candidate gate, and each scenario result.
    All collections are sorted here so neither dict insertion order nor worker
    completion order can influence the result.
    """

    manifest_hash = _required_string(report, "manifest_hash", "report")
    if manifest_hash != manifest.manifest_hash():
        raise ReproductionContractError("report.manifest_hash does not match manifest")
    research_classification = _required_string(report, "research_classification", "report")
    dataset_fingerprint = _required_string(report, "dataset_content_hash", "report")
    strategy_name = _required_string(report, "strategy_name", "report")
    if strategy_name != manifest.strategy_name:
        raise ReproductionContractError("report.strategy_name does not match manifest")

    dataset_split_hashes = _dataset_split_hashes(report)
    candidates_value = report.get("candidates")
    if not isinstance(candidates_value, list) or not candidates_value:
        raise ReproductionContractError("report.candidates is required and must be non-empty")
    candidates = tuple(sorted((_candidate_fingerprint(candidate) for candidate in candidates_value), key=_candidate_sort_key))
    strategy_contract_hashes = tuple(sorted({str(item["strategy_contract_hash"]) for item in candidates}))

    execution_assumptions = (
        {"name": "cost_model", "hash": sha256_prefixed(manifest.cost_model.as_dict())},
        {"name": "execution_model", "hash": sha256_prefixed(manifest.execution_model.as_dict())},
        {"name": "execution_timing", "hash": sha256_prefixed(manifest.execution_timing.as_dict())},
        {"name": "portfolio_policy", "hash": manifest.portfolio_policy_hash()},
        {"name": "risk_policy", "hash": manifest.risk_policy_hash()},
        {"name": "simulation_seed_scope", "hash": manifest.simulation_seed_scope_hash()},
        {"name": "simulation_policy", "hash": manifest.simulation_policy_hash()},
    )
    _assert_report_execution_bindings(report, execution_assumptions)
    final_selection = {
        "best_candidate_id": report.get("best_candidate_id"),
        "selected_candidate_id": report.get("selected_candidate_id"),
        "validation_eligibility_status": _required_string(report, "validation_eligibility_gate_result", "report"),
        "statistical_gate_result": report.get("statistical_gate_result"),
        "final_selection_gate_result": report.get("final_selection_gate_result"),
    }
    material: dict[str, object] = {
        "schema_version": REPRODUCTION_FINGERPRINT_SCHEMA_VERSION,
        "manifest_hash": manifest_hash,
        "research_classification": research_classification,
        "dataset_fingerprint": dataset_fingerprint,
        "dataset_split_hashes": list(dataset_split_hashes),
        "strategy_contract_hashes": list(strategy_contract_hashes),
        "execution_assumption_hashes": list(execution_assumptions),
        "candidate_fingerprints": list(candidates),
        "final_selection": final_selection,
    }
    return ResearchReproductionFingerprint(
        **material,
        stable_fingerprint_hash=sha256_prefixed(material, label="reproduction_stable_fingerprint"),
    )


def create_reproduction_receipt(
    *,
    report: Mapping[str, Any],
    manifest: ExperimentManifest,
    receipt_path: str | Path,
) -> dict[str, object]:
    fingerprint = build_reproduction_fingerprint(report, manifest=manifest)
    source_report_hash = _required_string(report, "content_hash", "report")
    payload: dict[str, object] = {
        "schema_version": REPRODUCTION_RECEIPT_SCHEMA_VERSION,
        "receipt_type": "research_run_reproduction_receipt",
        "experiment_id": manifest.experiment_id,
        "manifest_hash": manifest.manifest_hash(),
        "source_report_hash": source_report_hash,
        "stable_fingerprint": fingerprint.as_dict(),
        "stable_fingerprint_hash": fingerprint.stable_fingerprint_hash,
    }
    payload["receipt_content_hash"] = sha256_prefixed(
        content_hash_payload(payload), label="reproduction_receipt_content"
    )
    write_json_atomic(Path(receipt_path), payload)
    return payload


def load_reproduction_receipt(path: str | Path) -> dict[str, object]:
    receipt_path = Path(path).expanduser()
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReproductionContractError(f"unable to read reproduction receipt: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReproductionContractError("reproduction receipt must be a JSON object")
    if payload.get("schema_version") != REPRODUCTION_RECEIPT_SCHEMA_VERSION:
        raise ReproductionContractError("unsupported reproduction receipt schema_version")
    if payload.get("receipt_type") != "research_run_reproduction_receipt":
        raise ReproductionContractError("unsupported reproduction receipt type")
    expected_hash = _required_string(payload, "receipt_content_hash", "receipt")
    actual_hash = sha256_prefixed(
        content_hash_payload({key: value for key, value in payload.items() if key != "receipt_content_hash"}),
        label="reproduction_receipt_content",
    )
    if actual_hash != expected_hash:
        raise ReproductionContractError("reproduction receipt content hash mismatch")
    stable = payload.get("stable_fingerprint")
    if not isinstance(stable, dict):
        raise ReproductionContractError("receipt.stable_fingerprint is required")
    stable_hash = _required_string(payload, "stable_fingerprint_hash", "receipt")
    stable_without_hash = {key: value for key, value in stable.items() if key != "stable_fingerprint_hash"}
    actual_stable_hash = sha256_prefixed(stable_without_hash, label="reproduction_stable_fingerprint")
    if stable_hash != actual_stable_hash or stable.get("stable_fingerprint_hash") != stable_hash:
        raise ReproductionContractError("receipt stable fingerprint hash mismatch")
    for key in ("experiment_id", "manifest_hash", "source_report_hash"):
        _required_string(payload, key, "receipt")
    return payload


def compare_reproduction_fingerprints(
    expected: Mapping[str, Any],
    actual: ResearchReproductionFingerprint | Mapping[str, Any],
) -> ResearchReproductionComparison:
    expected_payload = _fingerprint_payload(expected)
    actual_payload = actual.as_dict() if isinstance(actual, ResearchReproductionFingerprint) else _fingerprint_payload(actual)
    expected_hash = _required_string(expected_payload, "stable_fingerprint_hash", "expected fingerprint")
    actual_hash = _required_string(actual_payload, "stable_fingerprint_hash", "actual fingerprint")
    mismatches: list[dict[str, object]] = []
    _compare_value(expected_payload, actual_payload, "", mismatches)
    # The hashes summarize the same material and make a compact outcome useful,
    # but field mismatches remain the authoritative drift explanation.
    status = "PASS" if not mismatches and expected_hash == actual_hash else "DRIFT"
    if status == "DRIFT" and not mismatches:
        mismatches.append({
            "path": "stable_fingerprint_hash",
            "expected": expected_hash,
            "actual": actual_hash,
            "kind": "value_mismatch",
        })
    return ResearchReproductionComparison(
        status=status,
        expected_fingerprint_hash=expected_hash,
        actual_fingerprint_hash=actual_hash,
        mismatches=tuple(mismatches),
    )


def _dataset_split_hashes(report: Mapping[str, Any]) -> tuple[dict[str, object], ...]:
    value = report.get("dataset_splits")
    if not isinstance(value, dict) or not value:
        raise ReproductionContractError("report.dataset_splits is required")
    rows: list[dict[str, object]] = []
    for split_name, split in value.items():
        if not isinstance(split, dict):
            raise ReproductionContractError(f"report.dataset_splits.{split_name} must be an object")
        rows.append({
            "split_name": str(split_name),
            "content_hash": _required_string(split, "content_hash", f"dataset_splits.{split_name}"),
            "quality_hash": _required_string(split, "quality_hash", f"dataset_splits.{split_name}"),
        })
    return tuple(sorted(rows, key=lambda item: str(item["split_name"])))


def _candidate_fingerprint(candidate: Any) -> dict[str, object]:
    if not isinstance(candidate, dict):
        raise ReproductionContractError("report.candidates entries must be objects")
    candidate_id = _required_string(candidate, "parameter_candidate_id", "candidate")
    scenarios_value = candidate.get("scenario_results")
    if not isinstance(scenarios_value, list) or not scenarios_value:
        raise ReproductionContractError(f"candidate {candidate_id} has no scenario_results")
    scenarios = tuple(sorted((_scenario_fingerprint(item, candidate_id) for item in scenarios_value), key=_scenario_sort_key))
    primary_scenario_id = _required_string(candidate, "primary_scenario_id", f"candidate {candidate_id}")
    return {
        "candidate_id": candidate_id,
        "effective_strategy_parameters_hash": _required_string(
            candidate, "effective_strategy_parameters_hash", f"candidate {candidate_id}"
        ),
        "acceptance_gate_status": _required_string(candidate, "acceptance_gate_result", f"candidate {candidate_id}"),
        "gate_fail_reasons": sorted(str(item) for item in _string_list(candidate.get("gate_fail_reasons"), "gate_fail_reasons")),
        "primary_scenario_id": primary_scenario_id,
        "strategy_contract_hash": _candidate_strategy_contract_hash(candidate, candidate_id),
        "scenarios": list(scenarios),
    }


def _scenario_fingerprint(scenario: Any, candidate_id: str) -> dict[str, object]:
    if not isinstance(scenario, dict):
        raise ReproductionContractError(f"candidate {candidate_id} scenario must be an object")
    context = f"candidate {candidate_id} scenario"
    metrics_hash = _scenario_metrics_hash(scenario, context)
    behavior_hash = _scenario_result_hash(scenario, "behavior_hash", context)
    strategy_behavior_hash = _scenario_result_hash(scenario, "strategy_behavior_hash", context)
    trade_ledger_hash = _scenario_result_hash(scenario, "trade_ledger_hash", context)
    equity_curve_hash = _scenario_result_hash(scenario, "equity_curve_hash", context)
    composite_behavior_hash = _scenario_result_hash(
        scenario,
        "composite_behavior_hash",
        context,
        components={
            "behavior_hash": behavior_hash,
            "strategy_behavior_hash": strategy_behavior_hash,
            "trade_ledger_hash": trade_ledger_hash,
            "equity_curve_hash": equity_curve_hash,
            "metrics_hash": metrics_hash,
        },
    )
    return {
        "scenario_index": _required_int(scenario, "scenario_index", context),
        "scenario_id": _required_string(scenario, "scenario_id", context),
        "scenario_role": _required_string(scenario, "scenario_role", context),
        "behavior_hash": behavior_hash,
        "strategy_behavior_hash": strategy_behavior_hash,
        "trade_ledger_hash": trade_ledger_hash,
        "equity_curve_hash": equity_curve_hash,
        "metrics_hash": metrics_hash,
        "composite_behavior_hash": composite_behavior_hash,
        "execution_model_hash": _required_string(scenario, "execution_model_hash", context),
        "portfolio_policy_hash": _required_string(scenario, "portfolio_policy_hash", context),
    }


def _scenario_metrics_hash(scenario: Mapping[str, Any], context: str) -> str:
    value = scenario.get("metrics_hash")
    if isinstance(value, str) and value:
        return value
    metric_payload = {
        key: scenario.get(key)
        for key in (
            "train_metrics", "validation_metrics", "final_holdout_metrics",
            "train_metrics_v2", "validation_metrics_v2", "final_holdout_metrics_v2",
        )
        if scenario.get(key) is not None
    }
    if not metric_payload:
        raise ReproductionContractError(f"{context}.metrics_hash is missing and no metrics are available")
    return sha256_prefixed(metric_payload, label="reproduction_scenario_metrics")


def _scenario_result_hash(
    scenario: Mapping[str, Any],
    key: str,
    context: str,
    *,
    components: Mapping[str, str] | None = None,
) -> str:
    """Use recorded evidence first, otherwise hash its retained result material.

    Earlier bounded report variants retain ``research_behavior_hash`` rather
    than every component hash.  The fallbacks below preserve result coverage
    without admitting missing evidence: each fallback is a hash of the
    retained ledger/event, curve/metric, or complete composite material.
    """

    direct = scenario.get(key)
    if isinstance(direct, str) and direct:
        return direct
    usage = scenario.get("validation_resource_usage")
    if isinstance(usage, dict):
        nested = usage.get(key)
        if isinstance(nested, str) and nested:
            return nested
        if key in {"behavior_hash", "strategy_behavior_hash"}:
            nested = usage.get("research_behavior_hash")
            if isinstance(nested, str) and nested:
                return nested
    if key == "trade_ledger_hash":
        material = {
            "validation_execution_event_summary": scenario.get("validation_execution_event_summary"),
            "validation_closed_trades_hash": scenario.get("validation_closed_trades_hash"),
            "validation_execution_metadata": scenario.get("validation_execution_metadata"),
        }
    elif key == "equity_curve_hash":
        material = {
            "validation_metrics": scenario.get("validation_metrics"),
            "validation_metrics_v2": scenario.get("validation_metrics_v2"),
            "validation_equity_curve_hash": scenario.get("validation_equity_curve_hash"),
            "validation_equity_curve_count": scenario.get("validation_equity_curve_count"),
        }
    elif key == "composite_behavior_hash" and components is not None:
        material = dict(components)
    else:
        raise ReproductionContractError(f"{context}.{key} is required")
    if not any(value is not None for value in material.values()):
        raise ReproductionContractError(f"{context}.{key} has no retained result evidence")
    return sha256_prefixed(material, label=f"reproduction_{key}")


def _candidate_strategy_contract_hash(candidate: Mapping[str, Any], candidate_id: str) -> str:
    value = candidate.get("strategy_plugin_contract_hash") or candidate.get("strategy_spec_hash")
    if not isinstance(value, str) or not value:
        raise ReproductionContractError(f"candidate {candidate_id}.strategy contract hash is required")
    return value


def _assert_report_execution_bindings(
    report: Mapping[str, Any], assumptions: tuple[dict[str, str], ...]
) -> None:
    expected = {item["name"]: item["hash"] for item in assumptions}
    if _required_string(report, "portfolio_policy_hash", "report") != expected["portfolio_policy"]:
        raise ReproductionContractError("report.portfolio_policy_hash does not match manifest")
    if _required_string(report, "simulation_policy_hash", "report") != expected["simulation_policy"]:
        raise ReproductionContractError("report.simulation_policy_hash does not match manifest")
    execution_model = report.get("execution_model")
    execution_timing = report.get("execution_timing_policy")
    if sha256_prefixed(execution_model) != expected["execution_model"]:
        raise ReproductionContractError("report.execution_model does not match manifest")
    if sha256_prefixed(execution_timing) != expected["execution_timing"]:
        raise ReproductionContractError("report.execution_timing_policy does not match manifest")


def _fingerprint_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    if not isinstance(payload.get("candidate_fingerprints"), list):
        raise ReproductionContractError("fingerprint.candidate_fingerprints is required")
    return payload


def _compare_value(expected: Any, actual: Any, path: str, mismatches: list[dict[str, object]]) -> None:
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual)):
            child_path = f"{path}.{key}" if path else key
            if key not in expected:
                mismatches.append({"path": child_path, "expected": None, "actual": actual[key], "kind": "missing_expected_field"})
            elif key not in actual:
                mismatches.append({"path": child_path, "expected": expected[key], "actual": None, "kind": "missing_actual_field"})
            else:
                _compare_value(expected[key], actual[key], child_path, mismatches)
        return
    if isinstance(expected, list) and isinstance(actual, list):
        for index in range(max(len(expected), len(actual))):
            child_path = f"{path}[{index}]"
            if index >= len(expected):
                mismatches.append({"path": child_path, "expected": None, "actual": actual[index], "kind": "unexpected_scenario" if ".scenarios" in path else "unexpected_candidate"})
            elif index >= len(actual):
                mismatches.append({"path": child_path, "expected": expected[index], "actual": None, "kind": "missing_scenario" if ".scenarios" in path else "missing_candidate"})
            else:
                _compare_value(expected[index], actual[index], child_path, mismatches)
        return
    if expected != actual:
        mismatches.append({"path": path, "expected": expected, "actual": actual, "kind": "value_mismatch"})


def _candidate_sort_key(value: Mapping[str, object]) -> str:
    return str(value["candidate_id"])


def _scenario_sort_key(value: Mapping[str, object]) -> tuple[int, str]:
    return int(value["scenario_index"]), str(value["scenario_id"])


def _required_string(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ReproductionContractError(f"{context}.{key} is required")
    return value


def _required_int(payload: Mapping[str, Any], key: str, context: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReproductionContractError(f"{context}.{key} is required")
    return value


def _string_list(value: Any, context: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ReproductionContractError(f"{context} must be a list of strings")
    return list(value)
