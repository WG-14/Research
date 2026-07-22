"""Canonical reproduction evidence for independent-verification tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from market_research.paths import ResearchPathManager
from market_research.research.code_provenance import (
    CODE_PROVENANCE_SCHEMA_VERSION,
    INSTALLED_DEPENDENCY_CONTRACT_BASIS,
    RESOLVED_DEPENDENCY_CONTENT_IDENTITY_BASIS,
    combined_dependency_contract_hash,
)
from market_research.research.execution_plan import (
    DETERMINISTIC_SINGLE_THREAD_ENVIRONMENT_VARIABLES,
    RESULT_AFFECTING_ENVIRONMENT_VARIABLES,
)
from market_research.research.experiment_registry import (
    FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
    append_attempt_completion,
    reserve_research_attempt,
)
from market_research.research.final_selection import (
    FINAL_HOLDOUT_CONFIRMATION_SCHEMA_VERSION,
    FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION,
    SELECTION_ARTIFACT_SCHEMA_VERSION,
    SELECTION_UNIVERSE_HASH_SEMANTICS,
    compute_final_holdout_result_hash,
)
from market_research.research.hashing import (
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research.independent_verification import (
    IndependentVerificationResult,
    bind_reproduction_result_snapshot,
    independent_code_binding_hash,
    independent_reproduction_evidence,
    publish_independent_verification,
)
from market_research.research.reproduction import (
    REPRODUCTION_FINGERPRINT_SCHEMA_VERSION,
    build_reproduction_receipt_from_fingerprint,
    compare_reproduction_fingerprints,
    load_reproduction_receipt,
)
from market_research.storage_io import write_json_atomic_create_or_verify


_FIXTURE_CONFIRMATION_PUBLISH_LOCK = Lock()


def seed_reproduction_receipts(
    *,
    manager: ResearchPathManager,
    experiment_id: str,
    manifest_hash: str,
    source_report_hash: str,
    source_report: Mapping[str, Any] | None = None,
    terminal_source_report_path: Path | None = None,
    reproduced_terminal_return_pct: float = 1.0,
) -> tuple[dict[str, object], Path, Path]:
    """Create canonical source/run reports and their bound receipts."""

    source_path = (
        terminal_source_report_path.resolve()
        if terminal_source_report_path is not None
        else manager.report_path(
            "research",
            experiment_id,
            "validation_summary.json",
        ).resolve()
    )
    if source_path.exists():
        source_report = _load_object(source_path)
        actual_source_hash = str(source_report.get("content_hash") or "")
    elif source_report is not None:
        source_payload = dict(source_report)
        source_payload.setdefault("generated_at", "2019-01-01T00:00:00+00:00")
        source_payload["content_hash"] = sha256_prefixed(
            report_content_hash_payload(source_payload)
        )
        if isinstance(source_report, dict):
            source_report.clear()
            source_report.update(source_payload)
        write_json_atomic_create_or_verify(source_path, source_payload)
        actual_source_hash = str(source_payload["content_hash"])
    else:
        source_payload = fixture_terminal_source_report(
            experiment_id=experiment_id,
            manifest_hash=manifest_hash,
        )
        write_json_atomic_create_or_verify(source_path, source_payload)
        actual_source_hash = str(source_payload["content_hash"])

    selection_receipt_path = manager.report_path(
        "research",
        experiment_id,
        "reproduction_receipt.json",
    ).resolve()
    selection_artifact = _fixture_selection_artifact(manifest_hash=manifest_hash)
    fingerprint = _fingerprint(
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        selection_artifact=selection_artifact,
    )
    selection_report_path = manager.report_path(
        "research", experiment_id, "backtest_report.json"
    ).resolve()
    selection_report = _fixture_reproduced_report(
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        report_path=selection_report_path,
        fingerprint=fingerprint,
        selection_artifact=selection_artifact,
    )
    write_json_atomic_create_or_verify(selection_report_path, selection_report)
    selection_report_hash = str(selection_report["content_hash"])
    selection_receipt = build_reproduction_receipt_from_fingerprint(
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        source_report_hash=selection_report_hash,
        stable_fingerprint=fingerprint,
    )
    write_json_atomic_create_or_verify(selection_receipt_path, selection_receipt)
    selection_receipt = load_reproduction_receipt(selection_receipt_path)

    final_holdout_query_hash = _digest(f"holdout-query:{experiment_id}")
    final_holdout_data_hash = _digest(f"holdout-data:{experiment_id}")
    final_holdout_fingerprint_hash = _digest(f"holdout-fingerprint:{experiment_id}")
    final_holdout_quality_hash = _digest(f"holdout-quality:{experiment_id}")
    confirmation = _publish_fixture_confirmation(
        manager=manager,
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        selection_artifact=selection_artifact,
        generated_at="2019-01-02T12:00:00+00:00",
        final_holdout_query_hash=final_holdout_query_hash,
        final_holdout_data_hash=final_holdout_data_hash,
        final_holdout_fingerprint_hash=final_holdout_fingerprint_hash,
        final_holdout_quality_hash=final_holdout_quality_hash,
    )
    final_holdout_result_hash = str(confirmation["final_holdout_result_hash"])

    binding_material = {
        "schema_version": 1,
        "artifact_type": "validated_research_reproduction_binding",
        "terminal_source_report_hash": actual_source_hash,
        "terminal_source_report_path": str(source_path),
        "manifest_hash": manifest_hash,
        "selection_report_hash": selection_report_hash,
        "selection_reproduction_receipt_hash": selection_receipt[
            "receipt_content_hash"
        ],
        "selection_artifact_hash": selection_artifact["content_hash"],
        "final_holdout_confirmation_hash": confirmation["content_hash"],
        "final_holdout_result_hash": final_holdout_result_hash,
        "final_holdout_query_hash": final_holdout_query_hash,
        "final_holdout_data_hash": final_holdout_data_hash,
        "final_holdout_fingerprint_hash": final_holdout_fingerprint_hash,
        "final_holdout_quality_hash": final_holdout_quality_hash,
        "reproduction_binding_hash": _digest(f"reproduction-binding:{experiment_id}"),
    }
    binding = {
        **binding_material,
        "content_hash": sha256_prefixed(
            binding_material,
            label="validated_research_reproduction_binding",
        ),
    }
    baseline_path = manager.report_path(
        "research",
        experiment_id,
        "validated_research_reproduction_receipt.json",
    ).resolve()
    baseline = build_reproduction_receipt_from_fingerprint(
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        source_report_hash=actual_source_hash,
        stable_fingerprint=fingerprint,
        evidence_scope="validated_research_result",
        source_evidence_binding=binding,
    )
    write_json_atomic_create_or_verify(baseline_path, baseline)
    baseline = load_reproduction_receipt(baseline_path)

    prefix = str(baseline["receipt_content_hash"]).removeprefix("sha256:")[:12]
    reproduction_manager = _fixture_reproduction_manager(
        manager=manager,
        experiment_id=experiment_id,
        prefix=prefix,
    )
    reproduced_path = reproduction_manager.report_path(
        "research", experiment_id, "reproduction_receipt.json"
    ).resolve()
    reproduced_report_path = reproduced_path.with_name("backtest_report.json")
    reproduced_report = _fixture_reproduced_report(
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        report_path=reproduced_report_path,
        fingerprint=fingerprint,
        selection_artifact=selection_artifact,
    )
    write_json_atomic_create_or_verify(reproduced_report_path, reproduced_report)
    _publish_fixture_confirmation(
        manager=reproduction_manager,
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        selection_artifact=selection_artifact,
        generated_at="2019-01-03T00:00:00+00:00",
        final_holdout_query_hash=final_holdout_query_hash,
        final_holdout_data_hash=final_holdout_data_hash,
        final_holdout_fingerprint_hash=final_holdout_fingerprint_hash,
        final_holdout_quality_hash=final_holdout_quality_hash,
        return_pct=reproduced_terminal_return_pct,
    )
    reproduced_receipt = build_reproduction_receipt_from_fingerprint(
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        source_report_hash=str(reproduced_report["content_hash"]),
        stable_fingerprint=fingerprint,
    )
    write_json_atomic_create_or_verify(reproduced_path, reproduced_receipt)
    return baseline, baseline_path, reproduced_path


def publish_pass_verification(
    *,
    manager: ResearchPathManager,
    verification_id: str,
    experiment_id: str,
    source_report_hash: str,
    manifest_hash: str,
    verifier_id: str = "independent-verifier-a",
    version: str = "1",
    verified_at: str | None = None,
    publish: bool = True,
    source_report: Mapping[str, Any] | None = None,
) -> IndependentVerificationResult:
    baseline, baseline_path, reproduced_path = seed_reproduction_receipts(
        manager=manager,
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        source_report_hash=source_report_hash,
        source_report=source_report,
    )
    stable = _stable(baseline)
    reproduced = load_reproduction_receipt(reproduced_path)
    comparison = compare_reproduction_fingerprints(
        stable,
        _stable(reproduced),
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "status": comparison.status,
        "experiment_id": experiment_id,
        "manifest_hash": manifest_hash,
        "baseline_receipt_path": str(baseline_path),
        "baseline_receipt_hash": baseline["receipt_content_hash"],
        "phase": "fingerprint_comparison",
        "error_code": None,
        "error": None,
        **comparison.as_dict(),
        "reproduced_receipt_path": str(reproduced_path),
        "reproduced_receipt_hash": reproduced["receipt_content_hash"],
    }
    evidence = independent_reproduction_evidence(
        manager=manager,
        baseline_receipt_path=baseline_path,
        reproduced_receipt_path=reproduced_path,
    )
    payload.update(evidence)
    effective_verified_at = verified_at or max(
        str(evidence["source_report_generated_at"]),
        str(evidence["reproduction_completed_at"]),
        key=datetime.fromisoformat,
    )
    snapshot_path, snapshot_hash = bind_reproduction_result_snapshot(
        manager=manager,
        payload=payload,
    )
    result = IndependentVerificationResult(
        verification_id=verification_id,
        version=version,
        verifier_id=verifier_id,
        verifier_role="independent_verifier",
        verified_at=effective_verified_at,
        experiment_id=experiment_id,
        research_version=manifest_hash,
        source_report_hash=str(baseline["source_report_hash"]),
        manifest_hash=manifest_hash,
        baseline_receipt_hash=str(baseline["receipt_content_hash"]),
        baseline_receipt_path=str(baseline_path),
        reproduction_result_hash=snapshot_hash,
        reproduction_result_path=str(snapshot_path),
        reproduced_receipt_hash=str(reproduced["receipt_content_hash"]),
        reproduced_receipt_path=str(reproduced_path),
        code_binding_hash=independent_code_binding_hash(stable),
        data_binding_hash=str(stable["dataset_fingerprint"]),
        environment_binding_hash=str(stable["strict_environment_hash"]),
        expected_fingerprint_hash=comparison.expected_fingerprint_hash,
        actual_fingerprint_hash=comparison.actual_fingerprint_hash,
        status="PASS",
    )
    if publish:
        publish_independent_verification(manager=manager, result=result)
    return result


def publish_failed_verification(
    *,
    manager: ResearchPathManager,
    verification_id: str,
    experiment_id: str,
    source_report_hash: str,
    manifest_hash: str,
    verifier_id: str = "independent-verifier-a",
    version: str = "1",
    failure_code: str = "deterministic_fixture_failure",
    verified_at: str | None = None,
    source_report: Mapping[str, Any] | None = None,
) -> IndependentVerificationResult:
    baseline, baseline_path, _ = seed_reproduction_receipts(
        manager=manager,
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        source_report_hash=source_report_hash,
        source_report=source_report,
    )
    stable = _stable(baseline)
    error = "deterministic fixture failure"
    payload = {
        "schema_version": 1,
        "status": "REPRODUCTION_FAILED",
        "experiment_id": experiment_id,
        "manifest_hash": manifest_hash,
        "baseline_receipt_path": str(baseline_path),
        "baseline_receipt_hash": baseline["receipt_content_hash"],
        "phase": "reproduction_execution",
        "error_code": failure_code,
        "error": error,
        "mismatches": [],
    }
    evidence = independent_reproduction_evidence(
        manager=manager,
        baseline_receipt_path=baseline_path,
    )
    payload.update(evidence)
    effective_verified_at = verified_at or str(evidence["source_report_generated_at"])
    snapshot_path, snapshot_hash = bind_reproduction_result_snapshot(
        manager=manager,
        payload=payload,
    )
    failure_hash = sha256_prefixed(
        {
            "phase": payload["phase"],
            "error_code": failure_code,
            "error": error,
        },
        label="independent_verification_failure_evidence",
    )
    result = IndependentVerificationResult(
        verification_id=verification_id,
        version=version,
        verifier_id=verifier_id,
        verifier_role="independent_verifier",
        verified_at=effective_verified_at,
        experiment_id=experiment_id,
        research_version=manifest_hash,
        source_report_hash=str(baseline["source_report_hash"]),
        manifest_hash=manifest_hash,
        baseline_receipt_hash=str(baseline["receipt_content_hash"]),
        baseline_receipt_path=str(baseline_path),
        reproduction_result_hash=snapshot_hash,
        reproduction_result_path=str(snapshot_path),
        reproduced_receipt_hash=None,
        reproduced_receipt_path=None,
        code_binding_hash=independent_code_binding_hash(stable),
        data_binding_hash=str(stable["dataset_fingerprint"]),
        environment_binding_hash=str(stable["strict_environment_hash"]),
        expected_fingerprint_hash=str(stable["stable_fingerprint_hash"]),
        actual_fingerprint_hash=None,
        status="FAILED",
        unresolved_issues=(f"reproduction_failure:{failure_code}",),
        failure_code=failure_code,
        failure_evidence_hash=failure_hash,
    )
    publish_independent_verification(manager=manager, result=result)
    return result


def _fixture_selection_artifact(*, manifest_hash: str) -> dict[str, Any]:
    material = {
        "schema_version": SELECTION_ARTIFACT_SCHEMA_VERSION,
        "artifact_type": "pre_holdout_candidate_selection",
        "manifest_hash": manifest_hash,
        "selected_candidate_id": "candidate-a",
        "parameter_values_hash": _digest("parameter-values"),
        "effective_strategy_parameters_hash": _digest("parameters"),
        "compiled_strategy_contract_hash": _digest("compiled-contract"),
        "selection_universe_hash_semantics": SELECTION_UNIVERSE_HASH_SEMANTICS,
        "selection_universe_hash": _digest("selection-universe"),
        "validation_evidence_hash": _digest("validation-evidence"),
        "final_selection_contract_hash": _digest("final-selection-contract"),
        "candidate_scores_hash": _digest("candidate-scores"),
    }
    return {
        **material,
        "content_hash": sha256_prefixed(material, label="selection_artifact"),
    }


def _fixture_reproduction_manager(
    *,
    manager: ResearchPathManager,
    experiment_id: str,
    prefix: str,
) -> ResearchPathManager:
    settings = replace(
        manager.settings,
        artifact_root=manager.artifact_root / "reproductions" / experiment_id / prefix,
        report_root=manager.report_root / "reproductions" / experiment_id / prefix,
        cache_root=manager.cache_root / "reproductions" / experiment_id / prefix,
    )
    return ResearchPathManager.from_settings(
        settings,
        project_root=manager.project_root,
    )


def _fixture_reproduced_report(
    *,
    experiment_id: str,
    manifest_hash: str,
    report_path: Path,
    fingerprint: Mapping[str, Any],
    selection_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Invert the stable projection into a compact, authoritative report.

    The receipt is deliberately not the source of truth for candidate or run
    semantics.  This report contains every field used by the production
    projection, so changing report semantics while copying the old fingerprint
    is detected by ``validate_reproduction_receipt_report_binding``.
    """

    strict = dict(fingerprint["strict_environment"])
    provenance = {
        "schema_version": strict["code_provenance_schema_version"],
        "source_layout": strict["source_layout"],
        "dependency_contract_basis": strict["dependency_contract_basis"],
        "git_available": strict["git_available"],
        "git_commit": strict["git_commit"],
        "git_dirty": strict["git_dirty"],
        "git_status_hash": strict["git_status_hash"],
        "git_diff_hash": strict["git_diff_hash"],
        "source_tree_hash": strict["source_tree_hash"],
        "source_file_count": strict["source_file_count"],
        "declared_dependency_contract_hash": strict[
            "declared_dependency_contract_hash"
        ],
        "resolved_dependency_contract_hash": strict[
            "resolved_dependency_contract_hash"
        ],
        "resolved_dependency_distribution_identities": strict[
            "resolved_dependency_distribution_identities"
        ],
        "resolved_dependency_content_identity_basis": strict[
            "resolved_dependency_content_identity_basis"
        ],
        "dependency_contract_hash": strict["dependency_contract_hash"],
    }
    provenance["code_provenance_hash"] = sha256_prefixed(
        provenance,
        label="code_provenance",
    )
    run_environment = {
        "repository_version": strict["repository_version"],
        "python_version": strict["python_version"],
        "platform": strict["platform"],
        "system": strict["system"],
        "machine": strict["machine"],
        "runtime_semantics": strict["runtime_semantics"],
        "runtime_semantics_hash": strict["runtime_semantics_hash"],
        "code_provenance": provenance,
        "code_provenance_hash": provenance["code_provenance_hash"],
    }
    assumptions = {
        str(item["name"]): str(item["hash"])
        for item in fingerprint["execution_assumption_hashes"]
    }
    execution_model: dict[str, Any] = {}
    execution_timing_policy: dict[str, Any] = {}
    if sha256_prefixed(execution_model) != assumptions["execution_model"]:
        raise AssertionError("fixture execution-model fingerprint is inconsistent")
    if sha256_prefixed(execution_timing_policy) != assumptions["execution_timing"]:
        raise AssertionError("fixture execution-timing fingerprint is inconsistent")

    split_rows = fingerprint["dataset_split_hashes"]
    candidates = fingerprint["candidate_fingerprints"]
    final_selection = fingerprint["final_selection"]
    assert isinstance(split_rows, list)
    assert isinstance(candidates, list)
    assert isinstance(final_selection, Mapping)
    material: dict[str, Any] = {
        "schema_version": 2,
        "report_kind": fingerprint["report_kind"],
        "experiment_id": experiment_id,
        "strategy_name": fingerprint["strategy_name"],
        "manifest_hash": manifest_hash,
        "research_classification": fingerprint["research_classification"],
        "generated_at": "2019-01-02T18:00:00+00:00",
        "dataset_content_hash": fingerprint["dataset_fingerprint"],
        "dataset_splits": {
            str(row["split_name"]): {
                key: value for key, value in row.items() if key != "split_name"
            }
            for row in split_rows
            if isinstance(row, Mapping)
        },
        "candidates": [
            {
                "parameter_candidate_id": candidate["candidate_id"],
                "reproduction_candidate_fingerprint": dict(candidate),
            }
            for candidate in candidates
            if isinstance(candidate, Mapping)
        ],
        "execution_model": execution_model,
        "execution_timing_policy": execution_timing_policy,
        "portfolio_policy_hash": assumptions["portfolio_policy"],
        "risk_policy_hash": assumptions["risk_policy"],
        "simulation_policy_hash": assumptions["simulation_policy"],
        "run_environment": run_environment,
        "execution_plan": {
            "run_environment": run_environment,
            "run_environment_hash": sha256_prefixed(run_environment),
        },
        "best_candidate_id": final_selection["best_candidate_id"],
        "selected_candidate_id": final_selection["selected_candidate_id"],
        "validation_eligibility_gate_result": final_selection[
            "validation_eligibility_status"
        ],
        "statistical_gate_result": final_selection["statistical_gate_result"],
        "final_selection_gate_result": final_selection["final_selection_gate_result"],
        "selection_artifact_hash": final_selection["selection_artifact_hash"],
        "final_holdout_confirmation_hash": final_selection[
            "final_holdout_confirmation_hash"
        ],
        "selection_artifact": dict(selection_artifact),
        "artifact_paths": {"report_path": str(report_path.resolve())},
    }
    return {
        **material,
        "content_hash": sha256_prefixed(report_content_hash_payload(material)),
    }


def _publish_fixture_confirmation(
    *,
    manager: ResearchPathManager,
    experiment_id: str,
    manifest_hash: str,
    selection_artifact: Mapping[str, Any],
    generated_at: str,
    final_holdout_query_hash: str,
    final_holdout_data_hash: str,
    final_holdout_fingerprint_hash: str,
    final_holdout_quality_hash: str,
    return_pct: float = 1.0,
) -> dict[str, Any]:
    """Publish one immutable fixture confirmation under concurrent setup."""

    with _FIXTURE_CONFIRMATION_PUBLISH_LOCK:
        return _publish_fixture_confirmation_locked(
            manager=manager,
            experiment_id=experiment_id,
            manifest_hash=manifest_hash,
            selection_artifact=selection_artifact,
            generated_at=generated_at,
            final_holdout_query_hash=final_holdout_query_hash,
            final_holdout_data_hash=final_holdout_data_hash,
            final_holdout_fingerprint_hash=final_holdout_fingerprint_hash,
            final_holdout_quality_hash=final_holdout_quality_hash,
            return_pct=return_pct,
        )


def _publish_fixture_confirmation_locked(
    *,
    manager: ResearchPathManager,
    experiment_id: str,
    manifest_hash: str,
    selection_artifact: Mapping[str, Any],
    generated_at: str,
    final_holdout_query_hash: str,
    final_holdout_data_hash: str,
    final_holdout_fingerprint_hash: str,
    final_holdout_quality_hash: str,
    return_pct: float = 1.0,
) -> dict[str, Any]:
    path = manager.report_path(
        "research",
        experiment_id,
        "final_holdout_confirmation.json",
    ).resolve()
    if path.exists():
        return _load_object(path)
    selected_candidate_id = str(selection_artifact["selected_candidate_id"])
    compiled_hash = str(selection_artifact["compiled_strategy_contract_hash"])
    final_holdout_reuse_key_hash = _digest(
        f"holdout-reuse:{experiment_id}:{return_pct}"
    )
    material: dict[str, Any] = {
        "schema_version": FINAL_HOLDOUT_CONFIRMATION_SCHEMA_VERSION,
        "artifact_type": "final_holdout_confirmation",
        "manifest_hash": manifest_hash,
        "selection_artifact_hash": selection_artifact["content_hash"],
        "selected_candidate_id": selected_candidate_id,
        "candidate_results": [
            {
                "candidate_id": selected_candidate_id,
                "compiled_strategy_contract_hash": compiled_hash,
                "metrics": {
                    "return_pct": return_pct,
                    "max_drawdown_pct": 0.5,
                    "trade_count": 1,
                },
            }
        ],
        "confirmation_gate_result": "PASS",
        "confirmation_gate_fail_reasons": [],
        "final_holdout_query_hash": final_holdout_query_hash,
        "final_holdout_data_hash": final_holdout_data_hash,
        "final_holdout_fingerprint_hash": final_holdout_fingerprint_hash,
        "final_holdout_quality_hash": final_holdout_quality_hash,
        "final_holdout_reuse_key_hash": final_holdout_reuse_key_hash,
        "final_holdout_reuse_key_schema_version": (
            FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
        ),
        "dataset_artifact_evidence_hash": _digest(
            f"dataset-artifact-evidence:{experiment_id}"
        ),
        "final_holdout_result_hash_schema_version": (
            FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION
        ),
    }
    material["final_holdout_result_hash"] = compute_final_holdout_result_hash(material)
    reservation = reserve_research_attempt(
        manager=manager,
        base_payload={
            "experiment_id": experiment_id,
            "manifest_hash": manifest_hash,
            "selection_artifact_hash": selection_artifact["content_hash"],
            "selected_candidate_id": selected_candidate_id,
            "dataset_artifact_evidence_hash": material[
                "dataset_artifact_evidence_hash"
            ],
            "final_holdout_content_pending_until_completion": True,
        },
        created_at=generated_at,
    )
    completion = append_attempt_completion(
        manager=manager,
        reservation=reservation,
        updates={
            "dataset_artifact_evidence_hash": material[
                "dataset_artifact_evidence_hash"
            ],
            "final_holdout_query_hash": final_holdout_query_hash,
            "final_holdout_data_hash": final_holdout_data_hash,
            "final_holdout_fingerprint_hash": final_holdout_fingerprint_hash,
            "final_holdout_quality_hash": final_holdout_quality_hash,
            "final_holdout_reuse_key_hash": final_holdout_reuse_key_hash,
            "final_holdout_reuse_key_schema_version": (
                FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
            ),
            "selection_artifact_hash": selection_artifact["content_hash"],
            "selected_candidate_id": selected_candidate_id,
            "candidate_count": 1,
            "confirmation_gate_result": "PASS",
            "final_holdout_result_hash_schema_version": (
                FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION
            ),
            "final_holdout_result_hash": material["final_holdout_result_hash"],
        },
        created_at=generated_at,
    )
    material.update(
        {
            "generated_at": str(completion["row"]["created_at"]),
            "experiment_registry_path": reservation["path"],
            "experiment_registry_prior_hash": reservation["prior_hash"],
            "experiment_registry_row_hash": reservation["row_hash"],
            "experiment_registry_completion_row_hash": completion["row_hash"],
            "authorization_row_hash": reservation["row_hash"],
            "completion_row_hash": completion["row_hash"],
        }
    )
    confirmation = {
        **material,
        "content_hash": sha256_prefixed(
            material,
            label="final_holdout_confirmation",
        ),
    }
    write_json_atomic_create_or_verify(path, confirmation)
    return confirmation


def _fingerprint(
    *,
    experiment_id: str,
    manifest_hash: str,
    selection_artifact: Mapping[str, Any],
) -> dict[str, object]:
    runtime_environment = {
        name: None for name in RESULT_AFFECTING_ENVIRONMENT_VARIABLES
    }
    runtime_environment["PYTHONHASHSEED"] = "0"
    for name in DETERMINISTIC_SINGLE_THREAD_ENVIRONMENT_VARIABLES:
        runtime_environment[name] = "1"
    runtime_semantics = {
        "schema_version": 2,
        "python_implementation": "CPython",
        "byte_order": "little",
        "timezone_names": ["UTC"],
        "locale": "C.UTF-8",
        "result_affecting_environment": runtime_environment,
    }
    dependency_rows = [
        {
            "name": "fixture-runtime",
            "version": "1.0",
            "content_hash": _digest("fixture-runtime-content"),
            "file_count": 1,
        }
    ]
    resolved_dependency_hash = sha256_prefixed(
        dependency_rows,
        label="resolved_dependency_contract",
    )
    strict_environment: dict[str, object] = {
        "schema_version": 1,
        "repository_version": "fixture",
        "python_version": "3.12.0",
        "platform": "fixture-platform",
        "system": "Linux",
        "machine": "x86_64",
        "runtime_semantics": runtime_semantics,
        "runtime_semantics_hash": sha256_prefixed(
            runtime_semantics,
            label="research_runtime_semantics",
        ),
        "code_provenance_schema_version": CODE_PROVENANCE_SCHEMA_VERSION,
        "source_layout": "installed_distribution",
        "dependency_contract_basis": INSTALLED_DEPENDENCY_CONTRACT_BASIS,
        "git_available": False,
        "git_commit": "unknown",
        "git_dirty": None,
        "git_status_hash": None,
        "git_diff_hash": None,
        "source_tree_hash": _digest("source-tree"),
        "source_file_count": 1,
        "declared_dependency_contract_hash": None,
        "resolved_dependency_contract_hash": resolved_dependency_hash,
        "resolved_dependency_distribution_identities": dependency_rows,
        "resolved_dependency_content_identity_basis": (
            RESOLVED_DEPENDENCY_CONTENT_IDENTITY_BASIS
        ),
        "dependency_contract_hash": combined_dependency_contract_hash(
            basis=INSTALLED_DEPENDENCY_CONTRACT_BASIS,
            declared_dependency_contract_hash=None,
            resolved_dependency_contract_hash=resolved_dependency_hash,
        ),
        "code_provenance_hash": "",
        "source_archive_identity": None,
    }
    provenance_material = {
        "schema_version": strict_environment["code_provenance_schema_version"],
        "source_layout": strict_environment["source_layout"],
        "dependency_contract_basis": strict_environment["dependency_contract_basis"],
        "git_available": strict_environment["git_available"],
        "git_commit": strict_environment["git_commit"],
        "git_dirty": strict_environment["git_dirty"],
        "git_status_hash": strict_environment["git_status_hash"],
        "git_diff_hash": strict_environment["git_diff_hash"],
        "source_tree_hash": strict_environment["source_tree_hash"],
        "source_file_count": strict_environment["source_file_count"],
        "declared_dependency_contract_hash": strict_environment[
            "declared_dependency_contract_hash"
        ],
        "resolved_dependency_contract_hash": strict_environment[
            "resolved_dependency_contract_hash"
        ],
        "resolved_dependency_distribution_identities": strict_environment[
            "resolved_dependency_distribution_identities"
        ],
        "resolved_dependency_content_identity_basis": strict_environment[
            "resolved_dependency_content_identity_basis"
        ],
        "dependency_contract_hash": strict_environment["dependency_contract_hash"],
    }
    strict_environment["code_provenance_hash"] = sha256_prefixed(
        provenance_material,
        label="code_provenance",
    )
    material: dict[str, object] = {
        "schema_version": REPRODUCTION_FINGERPRINT_SCHEMA_VERSION,
        "report_kind": "backtest",
        "experiment_id": experiment_id,
        "strategy_name": "fixture_strategy",
        "manifest_hash": manifest_hash,
        "research_classification": "research_only",
        "dataset_fingerprint": _digest("dataset"),
        "dataset_split_hashes": [
            {
                "split_name": "train",
                "content_hash": _digest("split-content"),
                "quality_hash": _digest("split-quality"),
                "snapshot_data_hash": _digest("snapshot-data"),
                "snapshot_query_hash": _digest("snapshot-query"),
                "snapshot_fingerprint_hash": _digest("snapshot-fingerprint"),
                "artifact_id": "fixture-artifact",
                "artifact_manifest_hash": _digest("artifact-manifest"),
                "artifact_content_hash": _digest("artifact-content"),
                "artifact_schema_hash": _digest("artifact-schema"),
                "verification_status": "VERIFIED",
                "verification": {"overall_status": "VERIFIED"},
                "requested_range": {
                    "start": "2026-01-01",
                    "end": "2026-01-01",
                },
            }
        ],
        "strategy_contract_hashes": [_digest("strategy-contract")],
        "execution_assumption_hashes": [
            {"name": "cost_model", "hash": _digest("cost-model")},
            {"name": "execution_model", "hash": sha256_prefixed({})},
            {"name": "execution_timing", "hash": sha256_prefixed({})},
            {"name": "portfolio_policy", "hash": _digest("portfolio-policy")},
            {"name": "risk_policy", "hash": _digest("risk-policy")},
            {
                "name": "simulation_seed_scope",
                "hash": _digest("simulation-seed-scope"),
            },
            {"name": "simulation_policy", "hash": _digest("simulation-policy")},
        ],
        "strict_environment": strict_environment,
        "strict_environment_hash": sha256_prefixed(
            strict_environment,
            label="reproduction_strict_environment",
        ),
        "candidate_fingerprints": [
            {
                "candidate_id": "candidate-a",
                "effective_strategy_parameters_hash": _digest("parameters"),
                "strategy_spec_hash": _digest("strategy-spec"),
                "strategy_plugin_contract_hash": _digest("strategy-contract"),
                "strategy_registry_hash": _digest("strategy-registry"),
                "compiled_strategy_contract_hash": _digest("compiled-contract"),
                "report_candidate_projection_hash": sha256_prefixed(
                    {"parameter_candidate_id": "candidate-a"},
                    label="reproduction_report_candidate_projection",
                ),
                "acceptance_gate_status": "PASS",
                "gate_fail_reasons": [],
                "primary_scenario_id": "base",
                "scenarios": [
                    {
                        "scenario_index": 0,
                        "scenario_id": "base",
                        "scenario_role": "base",
                        "compiled_strategy_contract_hash": _digest("compiled-contract"),
                        "behavior_hash": _digest("behavior"),
                        "strategy_behavior_hash": _digest("strategy-behavior"),
                        "trade_ledger_hash": _digest("trade-ledger"),
                        "equity_curve_hash": _digest("equity-curve"),
                        "metrics_hash": _digest("metrics"),
                        "composite_behavior_hash": _digest("composite-behavior"),
                        "execution_model_hash": _digest("execution-model"),
                        "portfolio_policy_hash": _digest("portfolio-policy"),
                    }
                ],
            }
        ],
        "final_selection": {
            "best_candidate_id": "candidate-a",
            "selected_candidate_id": "candidate-a",
            "validation_eligibility_status": "PASS",
            "statistical_gate_result": "PASS",
            "final_selection_gate_result": "PASS",
            "selection_artifact_hash": selection_artifact["content_hash"],
            "final_holdout_confirmation_hash": None,
        },
    }
    material["stable_fingerprint_hash"] = sha256_prefixed(
        material,
        label="reproduction_stable_fingerprint",
    )
    return material


def fixture_terminal_source_report(
    *,
    experiment_id: str,
    manifest_hash: str,
) -> dict[str, Any]:
    material = {
        "schema_version": 3,
        "artifact_type": "validated_research_result",
        "experiment_id": experiment_id,
        "manifest_hash": manifest_hash,
        "generated_at": "2019-01-01T00:00:00+00:00",
        "end_to_end_validation_result": "PASS",
    }
    return {
        **material,
        "content_hash": sha256_prefixed(report_content_hash_payload(material)),
    }


def _stable(receipt: dict[str, object]) -> dict[str, Any]:
    value = receipt["stable_fingerprint"]
    assert isinstance(value, dict)
    return value


def _digest(value: str) -> str:
    return sha256_prefixed({"fixture": value}, label="verification_fixture")


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value
