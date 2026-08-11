"""Research-only validation summary writer.

Validation aggregates research evidence. It intentionally has no declaration,
profile, replay, or account-execution stage.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, NoReturn

from market_research.paths import ResearchPathManager
from market_research.storage_io import (
    open_lock_file,
    write_json_atomic_create_or_verify,
)

from .data_governance import (
    DATA_GOVERNANCE_BINDING_SCHEMA_VERSION,
    DATA_GOVERNANCE_POLICY_GOVERNED,
    DataGovernanceError,
    data_governance_report_binding_reasons,
    publish_data_usage_binding_for_artifact,
    require_data_usage_binding_for_artifact,
)
from .experiment_manifest import ExperimentManifest
from .experiment_registry import (
    experiment_registry_path,
    research_identity_from_manifest,
    validate_experiment_registry_binding,
)
from .final_selection import (
    selection_candidate_binding_summary,
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
from .knowledge_contract import AuthorityRef, KnowledgeContractError
from .validation_protocol import (
    resolve_candidate_result_artifact,
    run_final_holdout_confirmation,
    run_research_backtest,
    run_research_walk_forward,
)
from .strategy_registry import StrategyRegistry
from .research_decision_report import build_research_decision_report
from .report_writer import candidate_evidence_hash_inputs, summarize_report_candidate
from .research_classification import requires_candidate_validation
from .reproduction import (
    ReproductionContractError,
    build_reproduction_receipt_from_fingerprint,
    load_reproduction_receipt,
)
from .research_standard import (
    ResearchStandardError,
    has_research_standard_binding_evidence,
    parse_research_standard_binding,
    research_standard_version_matches,
)
from .study_lifecycle import StudyLifecycleError, admit_study_validation
from .validation_experiment_bundle import (
    ValidationExperimentBundle,
    ValidationExperimentBundleError,
    ValidationExperimentCapability,
    ValidationExperimentCapabilityMode,
    ValidationExperimentGateStatus,
    ValidationExperimentOutputs,
    ValidationExperimentPolicy,
    build_validation_experiment_bundle,
    derive_validation_experiment_capability,
    parse_validation_experiment_capability,
    parse_validation_experiment_policy,
    validate_validation_experiment_bundle,
)


class ValidationRunError(ValueError):
    pass


_MAX_TERMINAL_JSON_BYTES = 16 * 1024 * 1024
_MAX_VALIDATION_EXPERIMENT_BUNDLE_BYTES = 16 * 1024 * 1024


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_object_key")
        result[key] = value
    return result


def _reject_non_finite_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non_finite_json_constant:{value}")


def _lexical_absolute(path: str | Path) -> Path:
    """Return an absolute normalized path without following symlinks."""

    return Path(os.path.abspath(os.fspath(path)))


def _load_precomputed_validation_experiment_bundle(
    *, manager: ResearchPathManager, path: str | Path
) -> tuple[dict[str, Any], ValidationExperimentPolicy]:
    """Load one external bundle while leaving terminal binding checks to the gate."""

    try:
        resolved = manager.external_input_path(
            path, label="validation experiment bundle input"
        )
        raw = resolved.read_bytes()
    except (OSError, ValueError) as exc:
        raise ValidationRunError(
            f"validation_experiment_bundle_input_invalid:{exc}"
        ) from exc
    if not raw or len(raw) > _MAX_VALIDATION_EXPERIMENT_BUNDLE_BYTES:
        raise ValidationRunError("validation_experiment_bundle_input_size_invalid")
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_non_finite_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationRunError(
            "validation_experiment_bundle_input_json_invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise ValidationRunError("validation_experiment_bundle_input_must_be_object")
    try:
        policy = parse_validation_experiment_policy(payload.get("policy"))
    except ValidationExperimentBundleError as exc:
        raise ValidationRunError(
            f"validation_experiment_bundle_policy_invalid:{exc}"
        ) from exc
    return payload, policy


def _require_selected_artifact_path(
    *, manager: ResearchPathManager, experiment_id: str, declared_path: str
) -> Path:
    report_root = _lexical_absolute(manager.report_root)
    expected_path = _lexical_absolute(
        manager.report_path("research", experiment_id, "selected_candidate.json")
    )
    declared = Path(declared_path)
    if not declared_path or not declared.is_absolute():
        raise ValidationRunError("selected_candidate_artifact_path_mismatch")
    if _lexical_absolute(declared) != expected_path:
        raise ValidationRunError("selected_candidate_artifact_path_mismatch")
    try:
        expected_path.relative_to(report_root)
    except ValueError as exc:
        raise ValidationRunError("selected_candidate_artifact_path_mismatch") from exc

    cursor = expected_path
    while True:
        if cursor.is_symlink():
            raise ValidationRunError("selected_candidate_artifact_symlink_rejected")
        if cursor == report_root:
            break
        if cursor.parent == cursor:
            raise ValidationRunError("selected_candidate_artifact_path_mismatch")
        cursor = cursor.parent
    return expected_path


def resolve_bound_selected_candidate(
    report: dict[str, Any], *, manager: ResearchPathManager
) -> dict[str, Any]:
    """Load and verify the full selected candidate behind a compact terminal report."""

    if report.get("selected_candidate_binding_schema_version") != 1:
        raise ValidationRunError("selected_candidate_binding_schema_invalid")

    experiment_id = str(report.get("experiment_id") or "").strip()
    if not experiment_id:
        raise ValidationRunError("selected_candidate_experiment_id_missing")
    declared_path = str(report.get("selected_candidate_path") or "").strip()
    expected_path = _require_selected_artifact_path(
        manager=manager,
        experiment_id=experiment_id,
        declared_path=declared_path,
    )
    if not expected_path.is_file():
        raise ValidationRunError("selected_candidate_artifact_missing")
    try:
        payload = json.loads(expected_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationRunError("selected_candidate_artifact_unreadable") from exc
    if not isinstance(payload, dict):
        raise ValidationRunError("selected_candidate_artifact_malformed")
    actual_hash = sha256_prefixed(payload, label="selected_candidate_artifact_hash")
    if actual_hash != report.get("selected_candidate_artifact_hash"):
        raise ValidationRunError("selected_candidate_artifact_hash_mismatch")
    selected_id = str(report.get("selected_candidate_id") or "")
    artifact_id = str(
        payload.get("parameter_candidate_id") or payload.get("candidate_id") or ""
    )
    if selected_id != artifact_id:
        raise ValidationRunError("selected_candidate_artifact_identity_mismatch")
    compact = report.get("selected_candidate")
    if not isinstance(compact, dict):
        raise ValidationRunError("selected_candidate_compact_projection_missing")
    logical_candidate_hash = sha256_prefixed(
        candidate_evidence_hash_inputs(payload),
        label="candidate_evidence_hash",
    )
    if compact.get("candidate_payload_hash") != logical_candidate_hash:
        raise ValidationRunError("selected_candidate_logical_hash_mismatch")
    if compact.get("selection_binding") != selection_candidate_binding_summary(payload):
        raise ValidationRunError("selected_candidate_selection_binding_mismatch")
    return payload


def _candidate_identity(candidate: dict[str, Any]) -> str:
    return str(
        candidate.get("parameter_candidate_id") or candidate.get("candidate_id") or ""
    ).strip()


def _has_compact_candidate_binding(candidate: dict[str, Any]) -> bool:
    return bool(
        str(candidate.get("candidate_payload_hash") or "").startswith("sha256:")
        and isinstance(candidate.get("selection_binding"), dict)
    )


def _terminal_candidate_projections(
    *,
    selection_report: dict[str, Any],
    authoritative_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return compact, artifact-bound candidate rows for every terminal mode."""

    authoritative_by_id: dict[str, dict[str, Any]] = {}
    for candidate in authoritative_candidates:
        candidate_id = _candidate_identity(candidate)
        if not candidate_id:
            raise ValidationRunError("terminal_candidate_identity_missing")
        if candidate_id in authoritative_by_id:
            raise ValidationRunError("terminal_candidate_identity_duplicate")
        authoritative_by_id[candidate_id] = candidate

    raw_rows = [
        item
        for item in selection_report.get("candidates") or []
        if isinstance(item, dict)
    ]
    if not raw_rows:
        raw_rows = list(authoritative_candidates)
    contract = selection_report.get("final_selection_contract")
    final_selection_contract = contract if isinstance(contract, dict) else None
    projections: list[dict[str, Any]] = []
    observed_ids: set[str] = set()
    for row in raw_rows:
        candidate_id = _candidate_identity(row)
        if not candidate_id:
            raise ValidationRunError("terminal_candidate_identity_missing")
        if candidate_id in observed_ids:
            raise ValidationRunError("terminal_candidate_identity_duplicate")
        observed_ids.add(candidate_id)
        if _has_compact_candidate_binding(row):
            projection = dict(row)
        else:
            source = authoritative_by_id.get(candidate_id, row)
            projection = summarize_report_candidate(
                source,
                final_selection_contract=final_selection_contract,
            )
        if not _has_compact_candidate_binding(projection):
            raise ValidationRunError("terminal_candidate_compact_binding_missing")
        projections.append(projection)
    return projections


def _terminal_json_bytes(payload: dict[str, Any]) -> bytes:
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(serialized) > _MAX_TERMINAL_JSON_BYTES:
        raise ValueError("atomic_json_target_too_large")
    return serialized


def _preflight_terminal_target(path: Path, expected: bytes) -> None:
    if path.is_symlink():
        raise ValueError("atomic_json_target_conflict")
    if not path.exists():
        return
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise ValueError("atomic_json_target_conflict") from exc
    if actual != expected:
        raise ValueError("atomic_json_target_conflict")


@contextmanager
def _terminal_publication_lock(candidate_target: Path) -> Iterator[None]:
    lock_path = candidate_target.parent / ".terminal-validation-publication.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open_lock_file(lock_path)
    lock_module: Any | None = None
    try:
        try:
            import fcntl
        except ImportError as exc:
            raise RuntimeError("terminal_validation_process_lock_unavailable") from exc
        lock_module = fcntl
        lock_module.flock(fd, lock_module.LOCK_EX)
        yield
    finally:
        try:
            if lock_module is not None:
                lock_module.flock(fd, lock_module.LOCK_UN)
        finally:
            os.close(fd)


def _publish_terminal_validation_artifacts(
    *,
    summary_target: Path,
    summary: dict[str, Any],
    candidate_target: Path,
    decision_report: dict[str, Any],
    selected_target: Path,
    selected_candidate: dict[str, Any],
    reproduction_receipt_target: Path | None = None,
    reproduction_receipt: dict[str, object] | None = None,
) -> None:
    """Publish terminal validation projections without replacing prior evidence."""

    targets: tuple[tuple[Path, dict[str, Any] | dict[str, object]], ...] = (
        (selected_target, selected_candidate),
        (candidate_target, decision_report),
        (summary_target, summary),
    )
    if reproduction_receipt_target is not None:
        if reproduction_receipt is None:
            raise ValidationRunError("terminal_validation_reproduction_receipt_missing")
        targets = (
            (reproduction_receipt_target, reproduction_receipt),
            *targets,
        )
    elif reproduction_receipt is not None:
        raise ValidationRunError(
            "terminal_validation_reproduction_receipt_path_missing"
        )
    with _terminal_publication_lock(candidate_target):
        serialized_by_path: dict[Path, bytes] = {}
        for path, payload in targets:
            key = _lexical_absolute(path)
            try:
                serialized = _terminal_json_bytes(payload)
                if key in serialized_by_path:
                    raise ValueError("atomic_json_target_conflict")
                serialized_by_path[key] = serialized
                _preflight_terminal_target(path, serialized)
            except ValueError as exc:
                raise ValidationRunError(
                    f"terminal_validation_artifact_publication_failed:{path.name}:{exc}"
                ) from exc
        for path, payload in targets:
            try:
                write_json_atomic_create_or_verify(path, payload)
            except ValueError as exc:
                raise ValidationRunError(
                    f"terminal_validation_artifact_publication_failed:{path.name}:{exc}"
                ) from exc


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
    report_content_hash = report.get("content_hash")
    report_hash_valid = (
        isinstance(report_content_hash, str)
        and report_content_hash.startswith("sha256:")
        and report_content_hash == sha256_prefixed(report_content_hash_payload(report))
    )
    if not report_hash_valid:
        reasons.append("validated_research_result_content_hash_invalid")
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
    receipt_scope = report.get("reproduction_receipt_scope")
    if validation_bound and receipt_scope is None:
        reasons.append("validated_research_result_reproduction_receipt_required")
    if receipt_scope is not None:
        if receipt_scope != "validated_research_result":
            reasons.append("validated_research_result_reproduction_scope_invalid")
        receipt_path_value = report.get("reproduction_receipt_path")
        receipt_hash = report.get("reproduction_receipt_hash")
        if (
            report.get("reproduction_receipt_status") != "AVAILABLE"
            or not isinstance(receipt_path_value, str)
            or not Path(receipt_path_value).is_absolute()
            or not isinstance(receipt_hash, str)
            or not receipt_hash.startswith("sha256:")
        ):
            reasons.append("validated_research_result_reproduction_receipt_invalid")
        elif manager is not None:
            expected_receipt_path = manager.report_path(
                "research",
                str(report.get("experiment_id") or ""),
                "validated_research_reproduction_receipt.json",
            ).resolve()
            try:
                receipt = load_reproduction_receipt(receipt_path_value)
            except ReproductionContractError:
                reasons.append("validated_research_result_reproduction_receipt_invalid")
            else:
                if (
                    Path(receipt_path_value).resolve() != expected_receipt_path
                    or receipt.get("receipt_content_hash") != receipt_hash
                    or receipt.get("source_report_hash") != report.get("content_hash")
                    or receipt.get("experiment_id") != report.get("experiment_id")
                    or receipt.get("manifest_hash") != report.get("manifest_hash")
                    or receipt.get("evidence_scope") != "validated_research_result"
                    or not isinstance(receipt.get("source_evidence_binding"), dict)
                ):
                    reasons.append(
                        "validated_research_result_reproduction_receipt_mismatch"
                    )
    reasons.extend(_validated_hypothesis_lineage_reasons(report))
    reasons.extend(_validated_research_standard_binding_reasons(report))
    reasons.extend(_validated_validation_experiment_reasons(report))
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
    if validation_bound:
        reasons.extend(data_governance_report_binding_reasons(report, manager=manager))
    if manager is not None and validation_bound and report_hash_valid:
        try:
            require_data_usage_binding_for_artifact(
                manager=manager,
                source=report,
                affected_authority_refs=(
                    _validated_research_result_authority_ref(report),
                ),
                required_purpose="CONFIRMATORY_RESEARCH",
            )
        except (DataGovernanceError, KnowledgeContractError) as exc:
            reasons.append(
                "validated_research_result_data_usage_binding_invalid:" + str(exc)
            )
    if report.get("selected_candidate_binding_schema_version") != 1:
        reasons.append(
            "validated_research_result_selected_candidate_binding_schema_invalid"
        )
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
    elif not _has_compact_candidate_binding(selected):
        reasons.append("validated_research_result_selected_candidate_binding_invalid")
    if (
        manager is not None
        and validation_bound
        and report.get("selected_candidate_binding_schema_version") == 1
    ):
        try:
            resolve_bound_selected_candidate(report, manager=manager)
        except ValidationRunError as exc:
            reasons.append(
                "validated_research_result_selected_candidate_artifact_invalid:"
                + str(exc)
            )
        compact_candidates = report.get("candidates")
        if not isinstance(compact_candidates, list) or not compact_candidates:
            reasons.append("validated_research_result_candidate_artifacts_missing")
        else:
            for candidate in compact_candidates:
                if not isinstance(candidate, dict):
                    reasons.append(
                        "validated_research_result_candidate_artifact_invalid"
                    )
                    continue
                detail_policy = str(
                    candidate.get("candidate_result_artifact_detail_policy") or ""
                )
                if detail_policy == "standard_bounded":
                    # Standard artifacts intentionally retain only a bounded
                    # diagnostic projection. The independently published full
                    # selected artifact remains the promotion authority.
                    continue
                if detail_policy not in {"external_full", "full"}:
                    reasons.append(
                        "validated_research_result_candidate_artifact_invalid:"
                        "candidate_result_artifact_detail_policy_invalid"
                    )
                    continue
                try:
                    resolve_candidate_result_artifact(
                        manager=manager,
                        compact_candidate=candidate,
                        expected_experiment_id=str(report.get("experiment_id") or ""),
                        expected_manifest_hash=str(report.get("manifest_hash") or ""),
                        expected_dataset_snapshot_id=str(
                            report.get("dataset_snapshot_id") or ""
                        ),
                        expected_dataset_content_hash=str(
                            report.get("dataset_content_hash") or ""
                        ),
                    )
                except ValueError as exc:
                    reasons.append(
                        "validated_research_result_candidate_artifact_invalid:"
                        + str(exc)
                    )
    return sorted(set(reasons))


def _validated_research_result_authority_ref(
    report: dict[str, Any],
) -> AuthorityRef:
    return AuthorityRef(
        authority="validated_research_result",
        subject_type="research_report",
        subject_id=str(report.get("experiment_id") or ""),
        subject_version=str(report.get("manifest_hash") or ""),
        authority_hash=str(report.get("content_hash") or ""),
    )


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


def _validated_research_standard_binding_reasons(
    report: dict[str, Any],
) -> list[str]:
    marker = report.get("research_standard_binding_schema_version")
    raw_binding = report.get("research_standard_binding")
    if marker is None and raw_binding is None:
        if has_research_standard_binding_evidence(report):
            return ["validated_research_result_research_standard_binding_stripped"]
        return []
    if marker != 2 or not isinstance(raw_binding, dict):
        return ["validated_research_result_research_standard_binding_invalid"]
    try:
        binding = parse_research_standard_binding(raw_binding)
    except (ResearchStandardError, ValueError):
        return ["validated_research_result_research_standard_binding_invalid"]
    reasons: list[str] = []
    if report.get("research_standard_binding_hash") != binding.content_hash:
        reasons.append(
            "validated_research_result_research_standard_binding_hash_mismatch"
        )
    if (
        report.get("hypothesis_id") != binding.hypothesis_version.hypothesis_id
        or not research_standard_version_matches(
            report.get("hypothesis_version"), binding.hypothesis_version.version
        )
        or report.get("hypothesis_contract_hash")
        != binding.legacy_hypothesis_contract_hash
    ):
        reasons.append("validated_research_result_research_standard_identity_mismatch")
    lineage = report.get("research_standard_lineage")
    if not isinstance(lineage, dict) or (
        lineage.get("binding_hash") != binding.content_hash
        or lineage.get("object_hashes") != binding.lineage_hashes()
    ):
        reasons.append("validated_research_result_research_standard_lineage_mismatch")
    admission = report.get("validation_admission")
    admission_payload = (
        admission.get("payload") if isinstance(admission, dict) else None
    )
    component_hashes = (
        admission_payload.get("component_hashes")
        if isinstance(admission_payload, dict)
        else None
    )
    if not isinstance(component_hashes, dict) or component_hashes.get(
        "research_standard_binding"
    ) != sha256_prefixed(binding.as_dict()):
        reasons.append("validated_research_result_research_standard_admission_mismatch")
    expected_ref = {
        "record_type": "research_standard_binding",
        "logical_id": binding.hypothesis_version.hypothesis_id,
        "version": str(binding.hypothesis_version.version),
        "record_hash": sha256_prefixed(binding.as_dict()),
    }
    if not isinstance(admission, dict) or expected_ref not in admission.get(
        "outbound_refs", []
    ):
        reasons.append(
            "validated_research_result_research_standard_admission_ref_mismatch"
        )
    return reasons


def _validated_validation_experiment_reasons(
    report: dict[str, Any],
) -> list[str]:
    fields = {
        "validation_experiment_capability",
        "validation_experiment_capability_hash",
        "validation_experiment_policy",
        "validation_experiment_policy_hash",
        "validation_experiment_temporal_plan_hash",
        "validation_experiment_bundle",
        "validation_experiment_bundle_hash",
        "validation_experiment_gate_result",
        "validation_experiment_gate_reasons",
    }
    present = fields.intersection(report)
    if not present:
        return [
            "validated_research_result_validation_experiment_capability_missing"
        ]
    if present != fields:
        return ["validated_research_result_validation_experiment_binding_incomplete"]
    reasons: list[str] = []
    capability: ValidationExperimentCapability | None = None
    try:
        capability = parse_validation_experiment_capability(
            report.get("validation_experiment_capability")
        )
    except ValidationExperimentBundleError:
        reasons.append(
            "validated_research_result_validation_experiment_capability_invalid"
        )
    if capability is not None:
        if report.get(
            "validation_experiment_capability_hash"
        ) != capability.content_hash:
            reasons.append(
                "validated_research_result_validation_experiment_capability_hash_mismatch"
            )
        if (
            capability.mode
            is ValidationExperimentCapabilityMode.LEGACY_SCHEMA_3_READ_ONLY
        ):
            reasons.append(
                "validated_research_result_validation_experiment_legacy_capability_not_promotable"
            )
        try:
            expected_capability = derive_validation_experiment_capability(
                manifest_hash=str(report.get("manifest_hash") or ""),
                research_classification=report.get("research_classification"),
            )
        except (ValueError, ValidationExperimentBundleError):
            reasons.append(
                "validated_research_result_validation_experiment_capability_authority_invalid"
            )
        else:
            if capability != expected_capability:
                reasons.append(
                    "validated_research_result_validation_experiment_capability_mismatch"
                )
    policy: ValidationExperimentPolicy | None = None
    try:
        policy = parse_validation_experiment_policy(
            report.get("validation_experiment_policy")
        )
    except ValidationExperimentBundleError:
        reasons.append(
            "validated_research_result_validation_experiment_policy_invalid"
        )
    if policy is not None and report.get(
        "validation_experiment_policy_hash"
    ) != policy.contract_hash():
        reasons.append(
            "validated_research_result_validation_experiment_policy_hash_mismatch"
        )
    if capability is not None and policy is not None and policy != capability.policy:
        reasons.append(
            "validated_research_result_validation_experiment_policy_not_authoritative"
        )
    bundle = report.get("validation_experiment_bundle")
    if not isinstance(bundle, dict):
        required = policy.required_components if policy is not None else ()
        missing_reasons = sorted(
            "validation_experiment_required_component_missing:" + item.value
            for item in required
        )
        if required:
            reasons.append(
                "validated_research_result_validation_experiment_bundle_missing"
            )
        if report.get("validation_experiment_bundle_hash") is not None:
            reasons.append(
                "validated_research_result_validation_experiment_bundle_hash_mismatch"
            )
        expected_gate_result = "FAIL" if required else "PASS"
        if report.get("validation_experiment_gate_result") != expected_gate_result:
            reasons.append(
                "validated_research_result_validation_experiment_gate_result_mismatch"
            )
        if report.get("validation_experiment_gate_reasons") != missing_reasons:
            reasons.append(
                "validated_research_result_validation_experiment_gate_reasons_mismatch"
            )
        if required:
            reasons.append(
                "validated_research_result_validation_experiment_gate_not_passed"
            )
        return sorted(set(reasons))
    selected = report.get("selected_candidate")
    selected_hash = (
        selected.get("candidate_payload_hash")
        if isinstance(selected, dict)
        else None
    )
    reasons.extend(
        "validated_research_result_" + item
        for item in validate_validation_experiment_bundle(
            bundle,
            expected_policy=policy,
            expected_manifest_hash=str(report.get("manifest_hash") or ""),
            expected_capability_hash=(
                capability.content_hash if capability is not None else ""
            ),
            expected_dataset_snapshot_hash=str(
                report.get("dataset_content_hash") or ""
            ),
            expected_temporal_plan_hash=str(
                report.get("validation_experiment_temporal_plan_hash") or ""
            ),
            expected_selected_candidate_id=str(
                report.get("selected_candidate_id") or ""
            ),
            expected_selected_candidate_hash=str(selected_hash or ""),
        )
    )
    if report.get("validation_experiment_bundle_hash") != bundle.get(
        "content_hash"
    ):
        reasons.append(
            "validated_research_result_validation_experiment_bundle_hash_mismatch"
        )
    if report.get("validation_experiment_gate_result") != bundle.get("gate_result"):
        reasons.append(
            "validated_research_result_validation_experiment_gate_result_mismatch"
        )
    if report.get("validation_experiment_gate_reasons") != bundle.get(
        "gate_reasons"
    ):
        reasons.append(
            "validated_research_result_validation_experiment_gate_reasons_mismatch"
        )
    if (
        policy is not None
        and policy.required_components
        and bundle.get("gate_result") != "PASS"
    ):
        reasons.append(
            "validated_research_result_validation_experiment_gate_not_passed"
        )
    return sorted(set(reasons))


def validation_next_action_payload(reasons: Any) -> dict[str, str]:
    del reasons
    return {
        "next_required_action": "inspect_research_validation_summary",
        "recommended_command": "research-validate",
    }


def _manifest_hash_for_validation(
    manifest: Any,
    selection_report: dict[str, Any],
) -> str:
    manifest_hash = getattr(manifest, "manifest_hash", None)
    if callable(manifest_hash):
        return str(manifest_hash())
    return str(selection_report.get("manifest_hash") or "")


def _validation_experiment_temporal_plan_hash(
    *,
    manifest: Any,
    selection_report: dict[str, Any],
) -> str:
    nested = selection_report.get("nested_temporal_validation")
    if isinstance(nested, dict):
        plan_hash = nested.get("plan_hash")
        if isinstance(plan_hash, str) and plan_hash.startswith("sha256:"):
            return plan_hash
    split = getattr(getattr(manifest, "dataset", None), "split", None)
    split_as_dict = getattr(split, "as_dict", None)
    split_payload = (
        split_as_dict()
        if callable(split_as_dict)
        else {
            "train": repr(getattr(split, "train", None)),
            "validation": repr(getattr(split, "validation", None)),
            "final_holdout": repr(getattr(split, "final_holdout", None)),
        }
    )
    return sha256_prefixed(
        {
            "manifest_hash": _manifest_hash_for_validation(
                manifest, selection_report
            ),
            "dataset_split": split_payload,
        },
        label="validation_experiment_temporal_scope",
    )


def aggregate_validation_gates(
    *,
    manifest: ExperimentManifest,
    selection_report: dict[str, Any],
    selection_artifact: dict[str, Any] | None,
    selected_candidate: dict[str, Any] | None,
    final_holdout_confirmation: dict[str, Any] | None,
    manager: ResearchPathManager | None = None,
    validation_experiment_policy: ValidationExperimentPolicy | None = None,
    validation_experiment_bundle: ValidationExperimentBundle
    | dict[str, Any]
    | None = None,
) -> tuple[str, dict[str, str], list[str]]:
    """Derive the terminal result from the authoritative stage evidence."""
    validation_experiment_capability = derive_validation_experiment_capability(
        manifest_hash=_manifest_hash_for_validation(manifest, selection_report),
        research_classification=manifest.research_classification,
    )
    authoritative_experiment_policy = validation_experiment_capability.policy
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

    validation_experiment_failed = False
    if (
        validation_experiment_policy is not None
        and validation_experiment_policy != authoritative_experiment_policy
    ):
        reasons.append("validation_experiment_policy_not_authoritative")
        validation_experiment_failed = True
    validation_experiment_policy = authoritative_experiment_policy
    if validation_experiment_policy is not None:
        if validation_experiment_bundle is None:
            if validation_experiment_policy.required_components:
                validation_experiment_failed = True
                reasons.extend(
                    "validation_experiment_required_component_missing:" + item.value
                    for item in validation_experiment_policy.required_components
                )
                reasons.append("validation_experiment_gate_not_passed")
        else:
            bundle_payload = (
                validation_experiment_bundle.as_dict()
                if isinstance(validation_experiment_bundle, ValidationExperimentBundle)
                else validation_experiment_bundle
            )
            expected_candidate_id = (
                _candidate_identity(selected_candidate)
                if isinstance(selected_candidate, dict)
                else None
            )
            expected_candidate_hash = (
                sha256_prefixed(
                    candidate_evidence_hash_inputs(selected_candidate),
                    label="candidate_evidence_hash",
                )
                if isinstance(selected_candidate, dict)
                else None
            )
            experiment_reasons = validate_validation_experiment_bundle(
                bundle_payload,
                expected_policy=validation_experiment_policy,
                expected_manifest_hash=_manifest_hash_for_validation(
                    manifest, selection_report
                ),
                expected_capability_hash=(
                    validation_experiment_capability.content_hash
                ),
                expected_dataset_snapshot_hash=str(
                    selection_report.get("dataset_content_hash") or ""
                ),
                expected_temporal_plan_hash=(
                    _validation_experiment_temporal_plan_hash(
                        manifest=manifest,
                        selection_report=selection_report,
                    )
                ),
                expected_selected_candidate_id=expected_candidate_id,
                expected_selected_candidate_hash=expected_candidate_hash,
            )
            if experiment_reasons:
                validation_experiment_failed = True
                reasons.extend(experiment_reasons)
            if (
                bundle_payload.get("gate_result")
                != ValidationExperimentGateStatus.PASS.value
            ):
                validation_experiment_failed = True
                bundle_gate_reasons = bundle_payload.get("gate_reasons")
                if isinstance(bundle_gate_reasons, list):
                    reasons.extend(str(item) for item in bundle_gate_reasons)
            if validation_experiment_failed:
                reasons.append("validation_experiment_gate_not_passed")

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
        or validation_experiment_failed
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
    validation_experiment_policy: ValidationExperimentPolicy | None = None,
    validation_experiment_outputs: ValidationExperimentOutputs | None = None,
    validation_experiment_bundle_path: str | Path | None = None,
) -> dict[str, Any]:
    if mode != "strict":
        raise ValidationRunError("validation_run_mode_unsupported")
    validation_experiment_capability = derive_validation_experiment_capability(
        manifest_hash=manifest.manifest_hash(),
        research_classification=manifest.research_classification,
    )
    authoritative_validation_experiment_policy = (
        validation_experiment_capability.policy
    )
    if (
        validation_experiment_policy is not None
        and validation_experiment_policy
        != authoritative_validation_experiment_policy
    ):
        raise ValidationRunError(
            "validation_experiment_policy_not_authoritative"
        )
    precomputed_validation_experiment_bundle: dict[str, Any] | None = None
    if validation_experiment_bundle_path is not None:
        if (
            validation_experiment_policy is not None
            or validation_experiment_outputs is not None
        ):
            raise ValidationRunError(
                "validation_experiment_bundle_input_conflicts_with_inline_outputs"
            )
        (
            precomputed_validation_experiment_bundle,
            declared_validation_experiment_policy,
        ) = _load_precomputed_validation_experiment_bundle(
            manager=manager,
            path=validation_experiment_bundle_path,
        )
        if (
            declared_validation_experiment_policy
            != authoritative_validation_experiment_policy
        ):
            raise ValidationRunError(
                "validation_experiment_bundle_policy_not_authoritative"
            )
    validation_experiment_policy = authoritative_validation_experiment_policy
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
        if requires_candidate_validation(manifest.research_classification):
            try:
                admit_study_validation(
                    manager=manager,
                    manifest=manifest,
                    validation_admission=validation_admission,
                    run_id=run_id,
                )
            except StudyLifecycleError as exc:
                raise ValidationRunError(
                    f"validation_study_lifecycle_admission_failed:{exc}"
                ) from exc
    walk_forward_required = bool(
        getattr(manifest.acceptance_gate, "walk_forward_required", False)
    )
    full_candidates: list[dict[str, Any]] = []
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
            full_candidates_sink=full_candidates,
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
            full_candidates_sink=full_candidates,
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
    selection_candidates = [
        item
        for item in selection_report.get("candidates") or []
        if isinstance(item, dict)
    ]
    externally_bound = bool(selection_candidates) and all(
        item.get("candidate_result_artifact_detail_policy") == "external_full"
        for item in selection_candidates
    )
    if externally_bound:
        candidates = [
            resolve_candidate_result_artifact(
                manager=manager,
                compact_candidate=item,
                expected_experiment_id=manifest.experiment_id,
                expected_manifest_hash=manifest.manifest_hash(),
                expected_dataset_snapshot_id=str(
                    selection_report.get("dataset_snapshot_id") or ""
                ),
                expected_dataset_content_hash=str(
                    selection_report.get("dataset_content_hash") or ""
                ),
            )
            for item in selection_candidates
        ]
    else:
        candidates = full_candidates or selection_candidates
    selected = next(
        (
            item
            for item in candidates
            if str(item.get("parameter_candidate_id") or "") == selected_id
        ),
        None,
    )
    terminal_candidates = _terminal_candidate_projections(
        selection_report=selection_report,
        authoritative_candidates=candidates,
    )
    compact_selected = next(
        (
            item
            for item in terminal_candidates
            if str(item.get("parameter_candidate_id") or item.get("candidate_id") or "")
            == selected_id
        ),
        None,
    )
    if selected is not None and compact_selected is None:
        raise ValidationRunError("terminal_selected_candidate_projection_missing")
    if validation_experiment_outputs is not None and validation_experiment_policy is None:
        raise ValidationRunError("validation_experiment_policy_required")
    validation_experiment_bundle: ValidationExperimentBundle | dict[str, Any] | None = (
        precomputed_validation_experiment_bundle
    )
    validation_experiment_temporal_plan_hash: str | None = None
    if validation_experiment_policy is not None:
        validation_experiment_temporal_plan_hash = (
            _validation_experiment_temporal_plan_hash(
                manifest=manifest,
                selection_report=selection_report,
            )
        )
        if (
            precomputed_validation_experiment_bundle is None
            and selected is not None
            and compact_selected is not None
            and (
                bool(validation_experiment_policy.required_components)
                or validation_experiment_outputs is not None
            )
        ):
            try:
                validation_experiment_bundle = build_validation_experiment_bundle(
                    manifest_hash=manifest.manifest_hash(),
                    dataset_snapshot_hash=str(
                        selection_report.get("dataset_content_hash") or ""
                    ),
                    temporal_plan_hash=validation_experiment_temporal_plan_hash,
                    selected_candidate_id=selected_id,
                    selected_candidate_hash=str(
                        compact_selected.get("candidate_payload_hash") or ""
                    ),
                    capability=validation_experiment_capability,
                    policy=validation_experiment_policy,
                    outputs=(
                        validation_experiment_outputs
                        or ValidationExperimentOutputs()
                    ),
                )
            except ValidationExperimentBundleError as exc:
                raise ValidationRunError(
                    f"validation_experiment_bundle_invalid:{exc}"
                ) from exc
    selection_evidence_report = dict(selection_report)
    selection_evidence_report["candidates"] = candidates
    confirmation = (
        run_final_holdout_confirmation(
            manifest=manifest,
            selection_report=selection_evidence_report,
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
        validation_experiment_policy=validation_experiment_policy,
        validation_experiment_bundle=validation_experiment_bundle,
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
        data_governance = validation_admission.get("data_governance")
        if isinstance(data_governance, dict):
            reproduction_binding_material.update(
                {
                    "data_governance_admission_record_hash": data_governance[
                        "admission_record_hash"
                    ],
                    "data_governance_admission_row_hash": data_governance[
                        "admission_row_hash"
                    ],
                    "data_governance_dataset_version_hash": data_governance[
                        "dataset_version_hash"
                    ],
                }
            )
    if manifest.research_standard_binding is not None:
        reproduction_binding_material["research_standard_binding_hash"] = (
            manifest.research_standard_binding.content_hash
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
        "candidates": terminal_candidates,
        "selection_artifact": artifact,
        "reproduction_binding": reproduction_binding,
        "selected_candidate": compact_selected,
        "validation_blocking_reasons": blocking_reasons,
        "end_to_end_validation_result": status,
    }
    if validation_experiment_policy is not None:
        bundle_payload = (
            validation_experiment_bundle.as_dict()
            if isinstance(validation_experiment_bundle, ValidationExperimentBundle)
            else validation_experiment_bundle
            if isinstance(validation_experiment_bundle, dict)
            else None
        )
        experiment_reasons = sorted(
            {
                item
                for item in blocking_reasons
                if str(item).startswith("validation_experiment_")
            }
        )
        summary.update(
            {
                "validation_experiment_capability": (
                    validation_experiment_capability.as_dict()
                ),
                "validation_experiment_capability_hash": (
                    validation_experiment_capability.content_hash
                ),
                "validation_experiment_policy": (
                    validation_experiment_policy.as_dict()
                ),
                "validation_experiment_policy_hash": (
                    validation_experiment_policy.contract_hash()
                ),
                "validation_experiment_temporal_plan_hash": (
                    validation_experiment_temporal_plan_hash
                ),
                "validation_experiment_bundle": bundle_payload,
                "validation_experiment_bundle_hash": (
                    bundle_payload.get("content_hash")
                    if bundle_payload is not None
                    else None
                ),
                "validation_experiment_gate_result": (
                    bundle_payload.get("gate_result")
                    if bundle_payload is not None
                    else (
                        "FAIL"
                        if validation_experiment_policy.required_components
                        else "PASS"
                    )
                ),
                "validation_experiment_gate_reasons": (
                    bundle_payload.get("gate_reasons")
                    if bundle_payload is not None
                    else experiment_reasons
                ),
            }
        )
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
        data_governance = validation_admission.get("data_governance")
        if isinstance(data_governance, dict):
            summary.update(
                {
                    "data_governance_policy": DATA_GOVERNANCE_POLICY_GOVERNED,
                    "data_governance_binding_schema_version": (
                        DATA_GOVERNANCE_BINDING_SCHEMA_VERSION
                    ),
                    "data_governance_registry_path": data_governance["path"],
                    "data_governance_admission_record_hash": data_governance[
                        "admission_record_hash"
                    ],
                    "data_governance_admission_row_hash": data_governance[
                        "admission_row_hash"
                    ],
                    "data_governance_dataset_version_hash": data_governance[
                        "dataset_version_hash"
                    ],
                    "data_governance_dataset_content_hash": (
                        data_governance["dataset_content_hash"]
                    ),
                    "data_governance_admission": data_governance["admission"],
                }
            )
    if manifest.research_standard_binding is not None:
        if validation_admission is None:
            raise ValidationRunError("research_standard_validation_admission_missing")
        standard_lineage = validation_admission.get("research_standard_lineage")
        if not isinstance(standard_lineage, dict):
            raise ValidationRunError("research_standard_registry_lineage_missing")
        summary.update(
            {
                "research_standard_binding_schema_version": 2,
                "research_standard_binding": (
                    manifest.research_standard_binding.as_dict()
                ),
                "research_standard_binding_hash": (
                    manifest.research_standard_binding.content_hash
                ),
                "research_standard_lineage": standard_lineage,
                "research_standard_preregistration_evidence_hash": (
                    manifest.research_standard_binding.preregistration_evidence_hash
                ),
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
    summary["selected_candidate_binding_schema_version"] = 1
    summary["selected_candidate_artifact_hash"] = sha256_prefixed(
        selected or {}, label="selected_candidate_artifact_hash"
    )
    # The selection runner's receipt is bound to the pre-terminal backtest or
    # walk-forward report.  A schema-3 result has a different approval hash,
    # so carrying that receipt forward would make a successful reproduction
    # unusable as approval evidence.  Publish a distinct receipt whose source
    # hash is the terminal result itself while retaining the same executable
    # fingerprint projection and full candidate evidence.
    summary.pop("reproduction_receipt_path", None)
    summary.pop("reproduction_receipt_hash", None)
    if summary.get("reproduction_receipt_status") == "AVAILABLE":
        summary["reproduction_receipt_scope"] = "validated_research_result"
    else:
        summary.pop("reproduction_receipt_scope", None)
    summary["content_hash"] = sha256_prefixed(report_content_hash_payload(summary))
    terminal_receipt_target: Path | None = None
    terminal_receipt: dict[str, object] | None = None
    if summary.get("reproduction_receipt_status") == "AVAILABLE":
        terminal_receipt_target = (
            report_root / "validated_research_reproduction_receipt.json"
        )
        try:
            selection_receipt_path = Path(
                str(selection_report.get("reproduction_receipt_path") or "")
            )
            expected_selection_receipt_path = (
                report_root / "reproduction_receipt.json"
            ).resolve()
            if (
                selection_receipt_path.is_symlink()
                or selection_receipt_path.resolve() != expected_selection_receipt_path
            ):
                raise ReproductionContractError(
                    "terminal validation selection receipt path mismatch"
                )
            selection_receipt = load_reproduction_receipt(selection_receipt_path)
            stable_fingerprint = selection_receipt.get("stable_fingerprint")
            if (
                not isinstance(stable_fingerprint, dict)
                or selection_receipt.get("source_report_hash")
                != selection_report.get("content_hash")
                or selection_receipt.get("manifest_hash") != manifest.manifest_hash()
                or selection_receipt.get("experiment_id") != manifest.experiment_id
            ):
                raise ReproductionContractError(
                    "terminal validation selection receipt binding mismatch"
                )
            confirmation_binding = (
                confirmation if isinstance(confirmation, dict) else {}
            )
            source_binding_material = {
                "schema_version": 1,
                "artifact_type": "validated_research_reproduction_binding",
                "terminal_source_report_hash": summary["content_hash"],
                "terminal_source_report_path": str(target.resolve()),
                "manifest_hash": manifest.manifest_hash(),
                "selection_report_hash": selection_report.get("content_hash"),
                "selection_reproduction_receipt_hash": selection_receipt.get(
                    "receipt_content_hash"
                ),
                "selection_artifact_hash": summary.get("selection_artifact_hash"),
                "final_holdout_confirmation_hash": summary.get(
                    "final_holdout_confirmation_hash"
                ),
                "final_holdout_result_hash": confirmation_binding.get(
                    "final_holdout_result_hash"
                ),
                "final_holdout_query_hash": confirmation_binding.get(
                    "final_holdout_query_hash"
                ),
                "final_holdout_data_hash": confirmation_binding.get(
                    "final_holdout_data_hash"
                ),
                "final_holdout_fingerprint_hash": confirmation_binding.get(
                    "final_holdout_fingerprint_hash"
                ),
                "final_holdout_quality_hash": confirmation_binding.get(
                    "final_holdout_quality_hash"
                ),
                "reproduction_binding_hash": reproduction_binding["content_hash"],
            }
            source_evidence_binding = {
                **source_binding_material,
                "content_hash": sha256_prefixed(
                    source_binding_material,
                    label="validated_research_reproduction_binding",
                ),
            }
            terminal_receipt = build_reproduction_receipt_from_fingerprint(
                experiment_id=manifest.experiment_id,
                manifest_hash=manifest.manifest_hash(),
                source_report_hash=str(summary["content_hash"]),
                stable_fingerprint=stable_fingerprint,
                evidence_scope="validated_research_result",
                source_evidence_binding=source_evidence_binding,
            )
        except ReproductionContractError as exc:
            raise ValidationRunError(
                f"terminal_validation_reproduction_receipt_failed:{exc}"
            ) from exc
        summary["reproduction_receipt_path"] = str(terminal_receipt_target.resolve())
        summary["reproduction_receipt_hash"] = terminal_receipt["receipt_content_hash"]
    _publish_terminal_validation_artifacts(
        summary_target=target,
        summary=summary,
        candidate_target=candidate_target,
        decision_report=decision_report,
        selected_target=selected_target,
        selected_candidate=selected or {},
        reproduction_receipt_target=terminal_receipt_target,
        reproduction_receipt=terminal_receipt,
    )
    if requires_candidate_validation(manifest.research_classification):
        try:
            publish_data_usage_binding_for_artifact(
                manager=manager,
                source=summary,
                affected_authority_refs=(
                    _validated_research_result_authority_ref(summary),
                ),
                recorded_by="validation-pipeline",
                recorded_at=generated_at,
                required_purpose="CONFIRMATORY_RESEARCH",
            )
        except DataGovernanceError as exc:
            raise ValidationRunError(
                f"validation_data_usage_binding_failed:{exc}"
            ) from exc
    return summary
