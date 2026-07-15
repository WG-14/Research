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
import re

from market_research.storage_io import write_json_atomic

from .experiment_manifest import ExperimentManifest
from .hashing import content_hash_payload, sha256_prefixed
from .strategy_compiler import StrategyCompilationError, validate_compiled_strategy_contract


REPRODUCTION_FINGERPRINT_SCHEMA_VERSION = 8
REPRODUCTION_RECEIPT_SCHEMA_VERSION = 8

_SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ReproductionContractError(ValueError):
    """A receipt or report does not provide sufficient reproducibility evidence."""


@dataclass(frozen=True, slots=True)
class ResearchReproductionFingerprint:
    schema_version: int
    report_kind: str
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
            "report_kind": self.report_kind,
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

    report_kind = _required_string(report, "report_kind", "report")
    if report_kind not in {"backtest", "walk_forward"}:
        raise ReproductionContractError("report.report_kind is unsupported")
    manifest_hash = _required_sha256(report, "manifest_hash", "report")
    if manifest_hash != manifest.manifest_hash():
        raise ReproductionContractError("report.manifest_hash does not match manifest")
    research_classification = _required_string(report, "research_classification", "report")
    if research_classification != manifest.research_classification:
        raise ReproductionContractError("report.research_classification does not match manifest")
    dataset_fingerprint = _required_sha256(report, "dataset_content_hash", "report")
    strategy_name = _required_string(report, "strategy_name", "report")
    if strategy_name != manifest.strategy_name:
        raise ReproductionContractError("report.strategy_name does not match manifest")

    dataset_split_hashes = _dataset_split_hashes(report)
    candidates_value = report.get("candidates")
    if not isinstance(candidates_value, list) or not candidates_value:
        raise ReproductionContractError("report.candidates is required and must be non-empty")
    candidates = tuple(sorted((_candidate_fingerprint(candidate) for candidate in candidates_value), key=_candidate_sort_key))
    strategy_contract_hashes = tuple(sorted({str(item["strategy_plugin_contract_hash"]) for item in candidates}))

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
        "selection_artifact_hash": report.get("selection_artifact_hash"),
        "final_holdout_confirmation_hash": report.get("final_holdout_confirmation_hash"),
    }
    material: dict[str, object] = {
        "schema_version": REPRODUCTION_FINGERPRINT_SCHEMA_VERSION,
        "report_kind": report_kind,
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
    source_report_hash = _required_sha256(report, "content_hash", "report")
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
    expected_hash = _required_sha256(payload, "receipt_content_hash", "receipt")
    actual_hash = sha256_prefixed(
        content_hash_payload({key: value for key, value in payload.items() if key != "receipt_content_hash"}),
        label="reproduction_receipt_content",
    )
    if actual_hash != expected_hash:
        raise ReproductionContractError("reproduction receipt content hash mismatch")
    stable = payload.get("stable_fingerprint")
    if not isinstance(stable, dict):
        raise ReproductionContractError("receipt.stable_fingerprint is required")
    stable_hash = _required_sha256(payload, "stable_fingerprint_hash", "receipt")
    stable_without_hash = {key: value for key, value in stable.items() if key != "stable_fingerprint_hash"}
    actual_stable_hash = sha256_prefixed(stable_without_hash, label="reproduction_stable_fingerprint")
    if stable_hash != actual_stable_hash or stable.get("stable_fingerprint_hash") != stable_hash:
        raise ReproductionContractError("receipt stable fingerprint hash mismatch")
    _required_string(payload, "experiment_id", "receipt")
    _required_sha256(payload, "manifest_hash", "receipt")
    _required_sha256(payload, "source_report_hash", "receipt")
    _validate_fingerprint_payload(stable, context="receipt.stable_fingerprint")
    return payload


def compare_reproduction_fingerprints(
    expected: Mapping[str, Any],
    actual: ResearchReproductionFingerprint | Mapping[str, Any],
) -> ResearchReproductionComparison:
    expected_payload = _fingerprint_payload(expected)
    actual_payload = actual.as_dict() if isinstance(actual, ResearchReproductionFingerprint) else _fingerprint_payload(actual)
    _validate_fingerprint_payload(expected_payload, context="expected fingerprint")
    _validate_fingerprint_payload(actual_payload, context="actual fingerprint")
    expected_hash = _required_sha256(expected_payload, "stable_fingerprint_hash", "expected fingerprint")
    actual_hash = _required_sha256(actual_payload, "stable_fingerprint_hash", "actual fingerprint")
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
            "content_hash": _required_sha256(split, "content_hash", f"dataset_splits.{split_name}"),
            "quality_hash": _required_sha256(split, "quality_hash", f"dataset_splits.{split_name}"),
            "snapshot_data_hash": _required_sha256(split, "snapshot_data_hash", f"dataset_splits.{split_name}"),
            "snapshot_query_hash": _required_sha256(split, "snapshot_query_hash", f"dataset_splits.{split_name}"),
            "snapshot_fingerprint_hash": _required_sha256(split, "snapshot_fingerprint_hash", f"dataset_splits.{split_name}"),
            "artifact_id": _required_string(split, "artifact_id", f"dataset_splits.{split_name}"),
            "artifact_manifest_hash": _required_sha256(split, "artifact_manifest_hash", f"dataset_splits.{split_name}"),
            "artifact_content_hash": _required_sha256(split, "artifact_content_hash", f"dataset_splits.{split_name}"),
            "artifact_schema_hash": _required_sha256(split, "artifact_schema_hash", f"dataset_splits.{split_name}"),
            "verification_status": _required_string(split, "verification_status", f"dataset_splits.{split_name}"),
            "verification": _required_mapping(split, "verification", f"dataset_splits.{split_name}"),
            "requested_range": _required_mapping(split, "requested_range", f"dataset_splits.{split_name}"),
        })
    return tuple(sorted(rows, key=lambda item: str(item["split_name"])))


def _candidate_fingerprint(candidate: Any) -> dict[str, object]:
    if not isinstance(candidate, dict):
        raise ReproductionContractError("report.candidates entries must be objects")
    candidate_id = _required_string(candidate, "parameter_candidate_id", "candidate")
    scenarios_value = candidate.get("scenario_results")
    if not isinstance(scenarios_value, list) or not scenarios_value:
        raise ReproductionContractError(f"candidate {candidate_id} has no scenario_results")
    primary_scenario_id = _required_string(candidate, "primary_scenario_id", f"candidate {candidate_id}")
    recorded_compiled_hash = _required_sha256(candidate, "compiled_strategy_contract_hash", f"candidate {candidate_id}")
    primary = next((item for item in scenarios_value if isinstance(item, dict) and
                    str(item.get("scenario_id") or "") == primary_scenario_id), None)
    if primary is None:
        raise ReproductionContractError(f"candidate {candidate_id}.primary_scenario_id mismatch")
    compiled = primary.get("compiled_strategy_contract")
    scenario_compiled_hash = primary.get("compiled_strategy_contract_hash")
    if not isinstance(compiled, dict) or scenario_compiled_hash != recorded_compiled_hash:
        raise ReproductionContractError(f"candidate {candidate_id}.primary scenario compiled contract mismatch")
    if candidate.get("compiled_strategy_contract") != compiled:
        raise ReproductionContractError(f"candidate {candidate_id}.compiled_strategy_contract primary mismatch")
    try:
        hydrated = validate_compiled_strategy_contract(
            dict(compiled), expected_compiled_hash=recorded_compiled_hash,
            expected_registry_hash=_required_sha256(candidate, "strategy_registry_hash", f"candidate {candidate_id}"),
            expected_plugin_hash=_required_sha256(candidate, "strategy_plugin_contract_hash", f"candidate {candidate_id}"),
        )
    except StrategyCompilationError as exc:
        raise ReproductionContractError(f"candidate {candidate_id}.compiled_strategy_contract invalid:{exc}") from exc
    if candidate.get("capability_contract_hash") not in {None, hydrated.capability_contract_hash}:
        raise ReproductionContractError(f"candidate {candidate_id}.capability_contract_hash mismatch")
    if (candidate.get("capability_contract") is not None
            and candidate.get("capability_contract") != compiled.get("capability_contract")):
        raise ReproductionContractError(f"candidate {candidate_id}.capability_contract mismatch")
    effective_hash = _required_sha256(candidate, "effective_strategy_parameters_hash", f"candidate {candidate_id}")
    if effective_hash != hydrated.materialized_parameters_hash:
        raise ReproductionContractError(f"candidate {candidate_id}.effective_strategy_parameters_hash mismatch")
    effective_payload = candidate.get("effective_strategy_parameters")
    if effective_payload is not None and sha256_prefixed(effective_payload) != effective_hash:
        raise ReproductionContractError(f"candidate {candidate_id}.effective_strategy_parameters payload mismatch")
    scenarios = tuple(sorted((_scenario_fingerprint(
        item, candidate_id, expected_strategy_name=hydrated.strategy_name,
        expected_strategy_version=hydrated.strategy_version,
        expected_registry_hash=hydrated.strategy_registry_hash,
        expected_plugin_hash=hydrated.strategy_plugin_contract_hash,
        expected_capability_hash=hydrated.capability_contract_hash,
    ) for item in scenarios_value), key=_scenario_sort_key))
    return {
        "candidate_id": candidate_id,
        "effective_strategy_parameters_hash": effective_hash,
        "strategy_spec_hash": _required_sha256(candidate, "strategy_spec_hash", f"candidate {candidate_id}"),
        "strategy_plugin_contract_hash": _required_sha256(
            candidate, "strategy_plugin_contract_hash", f"candidate {candidate_id}"
        ),
        "strategy_registry_hash": _required_sha256(candidate, "strategy_registry_hash", f"candidate {candidate_id}"),
        "compiled_strategy_contract_hash": recorded_compiled_hash,
        "acceptance_gate_status": _required_string(candidate, "acceptance_gate_result", f"candidate {candidate_id}"),
        "gate_fail_reasons": sorted(str(item) for item in _string_list(candidate.get("gate_fail_reasons"), "gate_fail_reasons")),
        "primary_scenario_id": primary_scenario_id,
        "scenarios": list(scenarios),
    }


def _scenario_fingerprint(
    scenario: Any, candidate_id: str, *, expected_strategy_name: str | None = None,
    expected_strategy_version: str | None = None, expected_registry_hash: str | None = None,
    expected_plugin_hash: str | None = None, expected_capability_hash: str | None = None,
) -> dict[str, object]:
    if not isinstance(scenario, dict):
        raise ReproductionContractError(f"candidate {candidate_id} scenario must be an object")
    scenario_id = _required_string(scenario, "scenario_id", f"candidate {candidate_id} scenario")
    context = f"candidate {candidate_id} scenario {scenario_id}"
    compiled_payload = scenario.get("compiled_strategy_contract")
    compiled_hash = _required_sha256(scenario, "compiled_strategy_contract_hash", context)
    if not isinstance(compiled_payload, dict):
        raise ReproductionContractError(f"{context}.compiled_strategy_contract is required")
    try:
        hydrated = validate_compiled_strategy_contract(
            dict(compiled_payload), expected_compiled_hash=compiled_hash,
            expected_strategy_name=expected_strategy_name,
            expected_strategy_version=expected_strategy_version,
            expected_registry_hash=expected_registry_hash,
            expected_plugin_hash=expected_plugin_hash,
        )
    except StrategyCompilationError as exc:
        raise ReproductionContractError(f"{context}.compiled_strategy_contract invalid:{exc}") from exc
    if expected_capability_hash is not None and hydrated.capability_contract_hash != expected_capability_hash:
        raise ReproductionContractError(f"{context}.compiled_strategy_contract capability mismatch")
    metrics_hash = _required_sha256(scenario, "metrics_hash", context)
    behavior_hash = _required_sha256(scenario, "behavior_hash", context)
    strategy_behavior_hash = _required_sha256(scenario, "strategy_behavior_hash", context)
    trade_ledger_hash = _required_sha256(scenario, "trade_ledger_hash", context)
    equity_curve_hash = _required_sha256(scenario, "equity_curve_hash", context)
    composite_behavior_hash = _required_sha256(scenario, "composite_behavior_hash", context)
    result = {
        "scenario_index": _required_int(scenario, "scenario_index", context),
        "scenario_id": scenario_id,
        "scenario_role": _required_string(scenario, "scenario_role", context),
        "compiled_strategy_contract_hash": compiled_hash,
        "behavior_hash": behavior_hash,
        "strategy_behavior_hash": strategy_behavior_hash,
        "trade_ledger_hash": trade_ledger_hash,
        "equity_curve_hash": equity_curve_hash,
        "metrics_hash": metrics_hash,
        "composite_behavior_hash": composite_behavior_hash,
        "execution_model_hash": _required_sha256(scenario, "execution_model_hash", context),
        "portfolio_policy_hash": _required_sha256(scenario, "portfolio_policy_hash", context),
    }
    execution = scenario.get("execution_evidence")
    if not isinstance(execution, dict):
        for split_name in ("validation_resource_usage", "final_holdout_resource_usage"):
            usage = scenario.get(split_name)
            if isinstance(usage, dict) and isinstance(usage.get("execution_evidence"), dict):
                execution = usage["execution_evidence"]
                break
    if not isinstance(execution, dict):
        raise ReproductionContractError(f"{context}.execution_evidence is required")
    if isinstance(execution, dict):
        aliases = {
            "decision_stream_hash": "decision_stream_hash",
            "execution_timing_policy_hash": "executed_execution_timing_policy_hash",
            "execution_timing_stream_hash": "execution_timing_stream_hash",
            "execution_model_hash": "executed_execution_model_hash",
            "request_stream_hash": "execution_request_stream_hash",
            "fill_stream_hash": "execution_fill_stream_hash",
            "ledger_stream_hash": "ledger_stream_hash",
        }
        for output_key, source_key in aliases.items():
            legacy = {"executed_execution_timing_policy_hash": "executed_execution_timing_hash", "ledger_stream_hash": "portfolio_ledger_hash"}.get(source_key)
            value = execution.get(source_key, execution.get(legacy) if legacy else None)
            if value is not None:
                if not isinstance(value, str) or not value.startswith("sha256:"):
                    raise ReproductionContractError(f"{context}.{source_key} must be a sha256 hash")
                result[output_key] = value
        for required_key in ("decision_stream_hash", "request_stream_hash", "fill_stream_hash", "ledger_stream_hash"):
            if required_key not in result:
                raise ReproductionContractError(f"{context}.{required_key} is required")
        for payload_key, output_key in (
            ("decision_stream", "decision_stream_hash"),
            ("execution_request_stream", "request_stream_hash"),
            ("execution_fill_stream", "fill_stream_hash"),
            ("ledger_stream", "ledger_stream_hash"),
        ):
            if scenario.get(payload_key) is not None and sha256_prefixed(scenario[payload_key]) != result[output_key]:
                raise ReproductionContractError(f"{context}.{payload_key} tampered")
        seed_rows = scenario.get("execution_fill_stream") or ()
        seed_hashes = sorted({str(fill.get("derived_seed_hash")) for fill in seed_rows if isinstance(fill, dict) and fill.get("derived_seed_hash")})
        if seed_hashes:
            result["stochastic_seed_evidence_hash"] = sha256_prefixed(seed_hashes)
    return result


def _assert_report_execution_bindings(
    report: Mapping[str, Any], assumptions: tuple[dict[str, str], ...]
) -> None:
    expected = {item["name"]: item["hash"] for item in assumptions}
    for item in assumptions:
        _required_sha256(item, "hash", f"execution assumption {item['name']}")
    if _required_sha256(report, "portfolio_policy_hash", "report") != expected["portfolio_policy"]:
        raise ReproductionContractError("report.portfolio_policy_hash does not match manifest")
    if _required_sha256(report, "simulation_policy_hash", "report") != expected["simulation_policy"]:
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


def _validate_fingerprint_payload(payload: Mapping[str, Any], *, context: str) -> None:
    if payload.get("schema_version") != REPRODUCTION_FINGERPRINT_SCHEMA_VERSION:
        raise ReproductionContractError(f"{context}.schema_version is unsupported")
    report_kind = _required_string(payload, "report_kind", context)
    if report_kind not in {"backtest", "walk_forward"}:
        raise ReproductionContractError(f"{context}.report_kind is unsupported")
    _required_sha256(payload, "manifest_hash", context)
    _required_string(payload, "research_classification", context)
    _required_sha256(payload, "dataset_fingerprint", context)
    _required_sha256(payload, "stable_fingerprint_hash", context)
    split_hashes = payload.get("dataset_split_hashes")
    if not isinstance(split_hashes, list) or not split_hashes:
        raise ReproductionContractError(f"{context}.dataset_split_hashes is required")
    for index, split in enumerate(split_hashes):
        if not isinstance(split, dict):
            raise ReproductionContractError(f"{context}.dataset_split_hashes[{index}] must be an object")
        _required_string(split, "split_name", f"{context}.dataset_split_hashes[{index}]")
        _required_sha256(split, "content_hash", f"{context}.dataset_split_hashes[{index}]")
        _required_sha256(split, "quality_hash", f"{context}.dataset_split_hashes[{index}]")
        for key in ("snapshot_data_hash", "snapshot_query_hash", "snapshot_fingerprint_hash"):
            _required_sha256(split, key, f"{context}.dataset_split_hashes[{index}]")
        _required_string(split, "artifact_id", f"{context}.dataset_split_hashes[{index}]")
        for key in ("artifact_manifest_hash", "artifact_content_hash", "artifact_schema_hash"):
            _required_sha256(split, key, f"{context}.dataset_split_hashes[{index}]")
        _required_string(split, "verification_status", f"{context}.dataset_split_hashes[{index}]")
        _required_mapping(split, "verification", f"{context}.dataset_split_hashes[{index}]")
        _required_mapping(split, "requested_range", f"{context}.dataset_split_hashes[{index}]")
    strategy_hashes = payload.get("strategy_contract_hashes")
    if not isinstance(strategy_hashes, list) or not strategy_hashes:
        raise ReproductionContractError(f"{context}.strategy_contract_hashes is required")
    for index, value in enumerate(strategy_hashes):
        _required_sha256({"hash": value}, "hash", f"{context}.strategy_contract_hashes[{index}]")
    assumptions = payload.get("execution_assumption_hashes")
    if not isinstance(assumptions, list) or not assumptions:
        raise ReproductionContractError(f"{context}.execution_assumption_hashes is required")
    for index, item in enumerate(assumptions):
        if not isinstance(item, dict):
            raise ReproductionContractError(f"{context}.execution_assumption_hashes[{index}] must be an object")
        _required_string(item, "name", f"{context}.execution_assumption_hashes[{index}]")
        _required_sha256(item, "hash", f"{context}.execution_assumption_hashes[{index}]")
    candidates = payload.get("candidate_fingerprints")
    if not isinstance(candidates, list) or not candidates:
        raise ReproductionContractError(f"{context}.candidate_fingerprints is required")
    for candidate in candidates:
        _validate_candidate_fingerprint(candidate, context=context)


def _validate_candidate_fingerprint(candidate: Any, *, context: str) -> None:
    if not isinstance(candidate, dict):
        raise ReproductionContractError(f"{context}.candidate_fingerprints entries must be objects")
    candidate_context = f"{context}.candidate {candidate.get('candidate_id') or '<unknown>'}"
    _required_string(candidate, "candidate_id", candidate_context)
    _required_sha256(candidate, "effective_strategy_parameters_hash", candidate_context)
    _required_sha256(candidate, "strategy_spec_hash", candidate_context)
    _required_sha256(candidate, "strategy_plugin_contract_hash", candidate_context)
    _required_string(candidate, "acceptance_gate_status", candidate_context)
    _string_list(candidate.get("gate_fail_reasons"), f"{candidate_context}.gate_fail_reasons")
    _required_string(candidate, "primary_scenario_id", candidate_context)
    scenarios = candidate.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ReproductionContractError(f"{candidate_context}.scenarios is required")
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            raise ReproductionContractError(f"{candidate_context}.scenarios entries must be objects")
        scenario_context = f"{candidate_context} scenario {scenario.get('scenario_id') or '<unknown>'}"
        _required_int(scenario, "scenario_index", scenario_context)
        for key in ("scenario_id", "scenario_role"):
            _required_string(scenario, key, scenario_context)
        for key in (
            "behavior_hash", "strategy_behavior_hash", "trade_ledger_hash", "equity_curve_hash",
            "metrics_hash", "composite_behavior_hash", "execution_model_hash", "portfolio_policy_hash",
        ):
            _required_sha256(scenario, key, scenario_context)


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


def _required_mapping(payload: Mapping[str, Any], key: str, context: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict) or not value:
        raise ReproductionContractError(f"{context}.{key} is required")
    return dict(value)


def _required_sha256(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if value is None:
        raise ReproductionContractError(f"{context}.{key} is required")
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise ReproductionContractError(f"{context}.{key} must be a sha256 hash")
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
