from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, SupportsIndex, SupportsInt, cast

from market_research.paths import ResearchPathManager
from market_research.storage_io import (
    append_authority_jsonl,
    ensure_authority_directory,
    open_lock_file,
    read_authority_text,
    write_json_atomic_create_or_verify,
)

from .research_classification import requires_candidate_validation
from .hashing import content_hash_payload, sha256_prefixed


EXPERIMENT_REGISTRY_SCHEMA_VERSION = 3
FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION = 4
PRE_EXPOSURE_RESERVATION_KEY_SCHEMA_VERSION = 1
FINAL_HOLDOUT_AUTHORITY_CONTRACT_SCHEMA_VERSION = 2
FINAL_HOLDOUT_RESERVATION_SCHEMA_VERSION = 2
PRE_HOLDOUT_GATE_ARTIFACT_SCHEMA_VERSION = 1
HOLDOUT_READ_CAPABILITY_SCHEMA_VERSION = 1
INDEPENDENT_REPRODUCTION_LIMIT_PER_PRIMARY = 1
EMPTY_EXPERIMENT_REGISTRY_HASH = sha256_prefixed([])
VALIDATION_PERMITTED_STATUSES = {"COMPLETED"}
EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE = "pre_completion_evidence_hash"
EXPERIMENT_REGISTRY_BUDGET_POLICY = "registry_append_only_budget_exempt"
PRE_CONTENT_COMPLETION_BOUND_FIELDS = {
    "dataset_content_hash",
    "dataset_quality_hash",
    "dataset_artifact_evidence_hash",
    "final_holdout_split_hash",
    "final_holdout_content_hash",
    "final_holdout_reuse_key_hash_v1",
    "final_holdout_reuse_key_hash",
    "final_holdout_reuse_key_schema_version",
    "final_holdout_reuse_key_hash_v2",
    "final_holdout_query_hash",
    "final_holdout_data_hash",
    "final_holdout_fingerprint_hash",
    "final_holdout_quality_hash",
    "final_holdout_result_hash",
    "selection_artifact_hash",
    "selected_candidate_id",
    "selection_attempt_index",
    "selection_holdout_reuse_count",
}


class FinalHoldoutAccessPurpose(StrEnum):
    """Strictly separate research selection from trusted verification replay."""

    PRIMARY_CONFIRMATION = "PRIMARY_CONFIRMATION"
    INDEPENDENT_REPRODUCTION = "INDEPENDENT_REPRODUCTION"


_INDEPENDENT_REPRODUCTION_PRIMARY_EQUAL_FIELDS = (
    "dataset_artifact_evidence_hash",
    "final_holdout_query_hash",
    "final_holdout_data_hash",
    "final_holdout_fingerprint_hash",
    "final_holdout_quality_hash",
    "final_holdout_reuse_key_hash",
    "final_holdout_reuse_key_schema_version",
    "final_holdout_result_hash",
    "final_holdout_result_hash_schema_version",
    "selection_artifact_hash",
    "selected_candidate_id",
    "selection_attempt_index",
    "selection_holdout_reuse_count",
    "candidate_count",
    "confirmation_gate_result",
)


def experiment_registry_path(*, manager: ResearchPathManager) -> Path:
    """Return the managed append-only experiment registry path.

    The experiment registry is a cross-run final-holdout attempt ledger. It is
    not an experiment-scoped artifact budget target, but it is managed reports
    evidence with append-only rows, prior-registry hashes, row hashes, and
    repo-local artifact checks.
    """
    path = manager.final_holdout_registry_path()
    project_root = manager.project_root.resolve()
    if ResearchPathManager.is_within(path.resolve(), project_root):
        raise ValueError(
            f"experiment registry path must be outside repository: {path.resolve()}"
        )
    return path


def final_holdout_dataset_identity_hash(manifest: Any) -> str:
    """Bind the immutable dataset declaration without binding mount paths."""

    dataset = getattr(manifest, "dataset", None)
    artifact_ref = getattr(dataset, "artifact_ref", None)
    top = getattr(dataset, "top_of_book", None)
    depth = getattr(dataset, "depth", None)
    return sha256_prefixed(
        {
            "schema_version": 1,
            "source": getattr(dataset, "source", None),
            "snapshot_id": getattr(dataset, "snapshot_id", None),
            "source_content_hash": getattr(dataset, "source_content_hash", None),
            "source_schema_hash": getattr(dataset, "source_schema_hash", None),
            "artifact_manifest_hash": getattr(
                artifact_ref, "artifact_manifest_hash", None
            ),
            "top_of_book_source_content_hash": getattr(
                top, "source_content_hash", None
            ),
            "top_of_book_source_schema_hash": getattr(
                top, "source_schema_hash", None
            ),
            "depth_source_content_hash": getattr(
                depth, "source_content_hash", None
            ),
            "depth_source_schema_hash": getattr(
                depth, "source_schema_hash", None
            ),
        }
    )


def final_holdout_authority_scope_hash(manifest: Any) -> str:
    """Return the dataset/market/range identity governed by one exposure fence.

    A manifest hash is deliberately *not* part of this scope.  Changing a
    hypothesis, strategy parameter, or experiment id must not turn the same
    immutable terminal rows into a fresh holdout.  The reservation row still
    binds the exact manifest independently.
    """

    split = getattr(getattr(manifest, "dataset", None), "split", None)
    final_holdout = getattr(split, "final_holdout", None)
    holdout_payload = (
        final_holdout.as_dict() if final_holdout is not None else None
    )
    return sha256_prefixed(
        {
            "schema_version": FINAL_HOLDOUT_AUTHORITY_CONTRACT_SCHEMA_VERSION,
            "dataset_identity_hash": final_holdout_dataset_identity_hash(manifest),
            "final_holdout_identity_hash": final_holdout_identity_hash_from_parts(
                dataset_source=getattr(getattr(manifest, "dataset", None), "source", None),
                market=getattr(manifest, "market", None),
                interval=getattr(manifest, "interval", None),
                final_holdout=holdout_payload,
            ),
        }
    )


def final_holdout_reservation_request_hash(
    *, request_id: str, request_hash: str
) -> str:
    return sha256_prefixed(
        {
            "schema_version": 1,
            "request_id": str(request_id),
            "request_hash": str(request_hash),
        }
    )


def registry_content_hash(path: Path) -> str:
    rows = load_experiment_registry_rows(path)
    return sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH


def row_hash_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "row_hash"}


def compute_row_hash(row: dict[str, Any]) -> str:
    return sha256_prefixed(content_hash_payload(row_hash_payload(row)))


def research_freedom_hash(payload: dict[str, Any]) -> str:
    material = {
        "experiment_family_id": payload.get("experiment_family_id"),
        "hypothesis_id": payload.get("hypothesis_id"),
        "hypothesis_version": payload.get("hypothesis_version"),
        "hypothesis_contract_hash": payload.get("hypothesis_contract_hash"),
        "hypothesis_semantic_fingerprint": payload.get(
            "hypothesis_semantic_fingerprint"
        ),
        "hypothesis_status": payload.get("hypothesis_status"),
        "pre_registered_at": payload.get("pre_registered_at"),
        "registration_evidence_hash": payload.get("registration_evidence_hash"),
        "dataset_snapshot_id": payload.get("dataset_snapshot_id"),
        "dataset_artifact_evidence_hash": payload.get("dataset_artifact_evidence_hash"),
        "train_split_hash": payload.get("train_split_hash"),
        "validation_split_hash": payload.get("validation_split_hash"),
        "final_holdout_split_hash": payload.get("final_holdout_split_hash"),
        "final_holdout_fingerprint": payload.get("final_holdout_fingerprint"),
        "final_holdout_identity_hash": payload.get("final_holdout_identity_hash"),
        "final_holdout_content_hash": payload.get("final_holdout_content_hash"),
        "final_holdout_query_hash": payload.get("final_holdout_query_hash"),
        "final_holdout_data_hash": payload.get("final_holdout_data_hash"),
        "final_holdout_fingerprint_hash": payload.get("final_holdout_fingerprint_hash"),
        "final_holdout_quality_hash": payload.get("final_holdout_quality_hash"),
        "final_holdout_reuse_key_hash": payload.get("final_holdout_reuse_key_hash"),
        "final_holdout_reuse_key_hash_v1": payload.get(
            "final_holdout_reuse_key_hash_v1"
        ),
        "final_holdout_reuse_key_schema_version": payload.get(
            "final_holdout_reuse_key_schema_version"
        ),
        "pre_exposure_reservation_key_hash": payload.get(
            "pre_exposure_reservation_key_hash"
        ),
        "pre_exposure_reservation_key_schema_version": payload.get(
            "pre_exposure_reservation_key_schema_version"
        ),
        "objective_metric": payload.get("objective_metric"),
        "parameter_space_hash": payload.get("parameter_space_hash"),
        "computed_attempt_index": payload.get("computed_attempt_index"),
        "computed_holdout_reuse_count": payload.get("computed_holdout_reuse_count"),
        "experiment_registry_prior_hash": payload.get("experiment_registry_prior_hash")
        or payload.get("prior_registry_hash"),
        "experiment_registry_row_hash": payload.get("experiment_registry_row_hash")
        or payload.get("row_hash"),
    }
    if payload.get("hypothesis_lineage_hash") is not None:
        material.update(
            {
                "hypothesis_lineage_hash": payload.get("hypothesis_lineage_hash"),
                "research_question_id": payload.get("research_question_id"),
                "research_question_version": payload.get("research_question_version"),
                "research_question_hash": payload.get("research_question_hash"),
                "observation_hashes": payload.get("observation_hashes"),
            }
        )
    return sha256_prefixed(material)


def research_identity_from_manifest(manifest: Any) -> dict[str, Any]:
    raw = (
        getattr(manifest, "raw", {})
        if isinstance(getattr(manifest, "raw", {}), dict)
        else {}
    )
    experiment_id = str(
        getattr(manifest, "experiment_id", "") or raw.get("experiment_id") or ""
    )
    spec = getattr(manifest, "hypothesis_spec", None)
    manifest_hypothesis = getattr(manifest, "hypothesis", None)
    if spec is not None:
        family_id = str(spec.experiment_family_id)
        hypothesis_id = str(spec.hypothesis_id)
        status = str(spec.registration_status)
        identity_source = "manifest.hypothesis_spec"
        family_source = "manifest.hypothesis_spec.experiment_family_id"
        version = str(spec.version)
        contract_hash = str(spec.contract_hash())
        semantic_fingerprint = str(spec.semantic_fingerprint())
        lineage_hash = spec.lineage_hash()
        question_ref = spec.research_question_ref
        research_question_id = (
            question_ref.question_id if question_ref is not None else None
        )
        research_question_version = (
            question_ref.version if question_ref is not None else None
        )
        research_question_hash = (
            question_ref.question_hash if question_ref is not None else None
        )
        observation_hashes = [item.observation_hash for item in spec.observation_refs]
        pre_registered_at = spec.pre_registered_at
        registration_evidence_hash = spec.registration_evidence_hash
        pre_registration_verified = bool(spec.pre_registration_verified)
    else:
        family_id = experiment_id
        hypothesis_id = sha256_prefixed(
            {"legacy_hypothesis": manifest_hypothesis or experiment_id}
        )
        status = "unregistered"
        identity_source = "legacy_manifest.hypothesis"
        family_source = "experiment_id"
        version = None
        contract_hash = None
        semantic_fingerprint = None
        lineage_hash = None
        research_question_id = None
        research_question_version = None
        research_question_hash = None
        observation_hashes = []
        pre_registered_at = None
        registration_evidence_hash = None
        pre_registration_verified = False
    return {
        "experiment_family_id": family_id,
        "hypothesis_id": hypothesis_id,
        "hypothesis_version": version,
        "hypothesis_contract_hash": contract_hash,
        "hypothesis_semantic_fingerprint": semantic_fingerprint,
        "hypothesis_lineage_hash": lineage_hash,
        "research_question_id": research_question_id,
        "research_question_version": research_question_version,
        "research_question_hash": research_question_hash,
        "observation_hashes": observation_hashes,
        "hypothesis_status": status,
        "hypothesis_identity_source": identity_source,
        "experiment_family_identity_source": family_source,
        "pre_registered_at": pre_registered_at,
        "registration_evidence_hash": registration_evidence_hash,
        "pre_registration_verified": pre_registration_verified,
        "experiment_id": experiment_id,
    }


def final_holdout_identity_hash_from_parts(
    *,
    dataset_source: str | None,
    market: str | None,
    interval: str | None,
    final_holdout: dict[str, Any] | None,
) -> str:
    return sha256_prefixed(
        {
            "dataset_source": dataset_source,
            "market": market,
            "interval": interval,
            "final_holdout_start": (final_holdout or {}).get("start"),
            "final_holdout_end": (final_holdout or {}).get("end"),
        }
    )


def final_holdout_reuse_key_hash_v2_from_parts(
    *,
    strategy_name: str | None,
    market: str | None,
    interval: str | None,
    final_holdout: dict[str, Any] | None,
    objective_metric: str | None,
    experiment_family_id: str | None = None,
    dataset_artifact_evidence_hash: str | None = None,
    final_holdout_query_hash: str | None = None,
    final_holdout_data_hash: str | None = None,
    final_holdout_fingerprint_hash: str | None = None,
    final_holdout_quality_hash: str | None = None,
) -> str | None:
    metric = str(objective_metric or "").strip()
    required_evidence = (
        dataset_artifact_evidence_hash,
        final_holdout_query_hash,
        final_holdout_data_hash,
        final_holdout_fingerprint_hash,
        final_holdout_quality_hash,
    )
    if not metric or not all(
        isinstance(value, str) and value.startswith("sha256:")
        for value in required_evidence
    ):
        return None
    return sha256_prefixed(
        {
            "schema": "final_holdout_completed_reuse_key_v4",
            "schema_version": FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
            "strategy_name": strategy_name,
            "market": market,
            "interval": interval,
            "final_holdout_start": (final_holdout or {}).get("start"),
            "final_holdout_end": (final_holdout or {}).get("end"),
            "objective_metric": metric,
            "experiment_family_id": experiment_family_id,
            "dataset_artifact_evidence_hash": dataset_artifact_evidence_hash,
            "final_holdout_query_hash": final_holdout_query_hash,
            "final_holdout_data_hash": final_holdout_data_hash,
            "final_holdout_fingerprint_hash": final_holdout_fingerprint_hash,
            "final_holdout_quality_hash": final_holdout_quality_hash,
        }
    )


def pre_exposure_reservation_key_hash_from_parts(
    *,
    strategy_name: str | None,
    market: str | None,
    interval: str | None,
    final_holdout: dict[str, Any] | None,
    objective_metric: str | None,
    dataset_artifact_evidence_hash: str | None,
) -> str | None:
    """Govern pre-exposure duplicate detection without claiming completed evidence."""
    metric = str(objective_metric or "").strip()
    if (
        not metric
        or not isinstance(dataset_artifact_evidence_hash, str)
        or not dataset_artifact_evidence_hash.startswith("sha256:")
    ):
        return None
    return sha256_prefixed(
        {
            "schema": "pre_exposure_reservation_key_v1",
            "schema_version": PRE_EXPOSURE_RESERVATION_KEY_SCHEMA_VERSION,
            "strategy_name": strategy_name,
            "market": market,
            "interval": interval,
            "final_holdout_start": (final_holdout or {}).get("start"),
            "final_holdout_end": (final_holdout or {}).get("end"),
            "objective_metric": metric,
            "dataset_artifact_evidence_hash": dataset_artifact_evidence_hash,
        }
    )


def objective_metric_from_manifest(manifest: Any) -> str | None:
    statistical_validation = getattr(manifest, "statistical_validation", None)
    primary_metric = str(
        getattr(statistical_validation, "primary_metric", "") or ""
    ).strip()
    if primary_metric:
        return primary_metric
    raw = (
        getattr(manifest, "raw", {})
        if isinstance(getattr(manifest, "raw", {}), dict)
        else {}
    )
    for key in ("objective_metric", "primary_metric"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return None


def final_holdout_content_hash_from_parts(
    *,
    dataset_snapshot_id: str | None,
    final_holdout_split_hash: str | None,
    dataset_quality_hash: str | None,
) -> str:
    return sha256_prefixed(
        {
            "dataset_snapshot_id": dataset_snapshot_id,
            "final_holdout_split_hash": final_holdout_split_hash,
            "dataset_quality_hash": dataset_quality_hash,
        }
    )


def final_holdout_hashes_from_manifest(
    *,
    manifest: Any,
    final_holdout_split_hash: str | None,
    dataset_quality_hash: str | None,
    dataset_artifact: dict[str, Any] | None = None,
    final_holdout_evidence: dict[str, Any] | None = None,
) -> dict[str, str | None]:
    dataset = getattr(manifest, "dataset", None)
    split = getattr(dataset, "split", None)
    final_holdout = getattr(split, "final_holdout", None)
    holdout_payload = final_holdout.as_dict() if final_holdout is not None else None
    objective_metric = objective_metric_from_manifest(manifest)
    identity = research_identity_from_manifest(manifest)
    identity_hash = final_holdout_identity_hash_from_parts(
        dataset_source=getattr(dataset, "source", None),
        market=getattr(manifest, "market", None),
        interval=getattr(manifest, "interval", None),
        final_holdout=holdout_payload,
    )
    artifact_evidence = _artifact_evidence(dataset_artifact)
    split_evidence = _split_evidence(final_holdout_evidence)
    reuse_key_hash = final_holdout_reuse_key_hash_v2_from_parts(
        strategy_name=getattr(manifest, "strategy_name", None),
        market=getattr(manifest, "market", None),
        interval=getattr(manifest, "interval", None),
        final_holdout=holdout_payload,
        objective_metric=objective_metric,
        experiment_family_id=None,
        dataset_artifact_evidence_hash=artifact_evidence[
            "dataset_artifact_evidence_hash"
        ],
        final_holdout_query_hash=split_evidence["final_holdout_query_hash"],
        final_holdout_data_hash=split_evidence["final_holdout_data_hash"],
        final_holdout_fingerprint_hash=split_evidence["final_holdout_fingerprint_hash"],
        final_holdout_quality_hash=split_evidence["final_holdout_quality_hash"],
    )
    content_hash = final_holdout_content_hash_from_parts(
        dataset_snapshot_id=getattr(dataset, "snapshot_id", None),
        final_holdout_split_hash=final_holdout_split_hash,
        dataset_quality_hash=dataset_quality_hash,
    )
    return {
        "final_holdout_identity_hash": identity_hash,
        "final_holdout_content_hash": content_hash,
        "final_holdout_reuse_key_hash_v1": identity_hash,
        "final_holdout_reuse_key_hash": reuse_key_hash,
        "final_holdout_reuse_key_schema_version": FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
        "final_holdout_reuse_key_hash_v2": reuse_key_hash,
        "objective_metric": objective_metric,
        "experiment_family_id": identity["experiment_family_id"],
        "final_holdout_fingerprint": identity_hash,
        **artifact_evidence,
        **split_evidence,
    }


def _artifact_evidence(value: dict[str, Any] | None) -> dict[str, str | None]:
    artifact = value if isinstance(value, dict) else {}
    canonical = {
        "artifact_id": artifact.get("artifact_id"),
        "artifact_manifest_hash": artifact.get("artifact_manifest_hash"),
        "artifact_content_hash": artifact.get("artifact_content_hash"),
        "artifact_schema_hash": artifact.get("artifact_schema_hash"),
        "verification_status": artifact.get("verification_status"),
    }
    return {"dataset_artifact_evidence_hash": sha256_prefixed(canonical)}


def _split_evidence(value: dict[str, Any] | None) -> dict[str, str | None]:
    split = value if isinstance(value, dict) else {}
    requested_range = split.get("requested_range")
    return {
        "final_holdout_query_hash": sha256_prefixed(
            {
                "requested_range": requested_range,
                "snapshot_query_hash": split.get("snapshot_query_hash"),
            }
        ),
        "final_holdout_data_hash": split.get("snapshot_data_hash"),
        "final_holdout_fingerprint_hash": split.get("snapshot_fingerprint_hash"),
        "final_holdout_quality_hash": split.get("quality_hash"),
    }


def load_experiment_registry_rows(path: Path) -> list[dict[str, Any]]:
    text_payload = read_authority_text(path, require_kernel_append_only=True)
    if text_payload is None:
        return []
    rows: list[dict[str, Any]] = []
    for line in text_payload.splitlines():
        text = line.strip()
        if not text:
            continue
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("experiment_registry_row_must_be_object")
        if payload.get("schema_version") != EXPERIMENT_REGISTRY_SCHEMA_VERSION:
            raise ValueError("experiment_registry_schema_version_unsupported")
        rows.append(payload)
    return rows


def experiment_registry_chain_reasons(rows: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for index, row in enumerate(rows):
        if compute_row_hash(row) != row.get("row_hash"):
            reasons.append("experiment_registry_row_hash_mismatch")
        expected_prior = (
            sha256_prefixed(rows[:index]) if index else EMPTY_EXPERIMENT_REGISTRY_HASH
        )
        if row.get("prior_registry_hash") != expected_prior:
            reasons.append("experiment_registry_prior_hash_mismatch")
    return sorted(set(reasons))


def validate_final_holdout_authority_registry(
    *,
    manager: ResearchPathManager,
    require_terminal: bool = False,
) -> dict[str, Any]:
    """Validate the shared authority chain and its reserve/fence lifecycle."""

    path = experiment_registry_path(manager=manager)
    try:
        with _locked_registry(path):
            rows = load_experiment_registry_rows(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "row_count": 0,
            "reservation_count": 0,
            "reasons": [f"final_holdout_authority_unreadable:{type(exc).__name__}"],
        }
    reasons = list(experiment_registry_chain_reasons(rows))
    authority_shaped_reservations = [
        row
        for row in rows
        if row.get("event_type") == "research_attempt_reserved"
        and row.get("final_holdout_authority_contract_schema_version") is not None
    ]
    reservations = [
        row
        for row in authority_shaped_reservations
        if row.get("final_holdout_authority_contract_schema_version")
        == FINAL_HOLDOUT_AUTHORITY_CONTRACT_SCHEMA_VERSION
    ]
    if len(reservations) != len(authority_shaped_reservations):
        reasons.append("final_holdout_authority_contract_schema_unsupported")
    authority_reservation_hashes = {
        str(row.get("row_hash") or "") for row in reservations
    }
    for row in rows:
        if row.get("event_type") in {
            "research_attempt_activated",
            "research_attempt_holdout_read_started",
            "research_attempt_completed",
            "research_attempt_aborted",
        } and str(row.get("reservation_row_hash") or "") not in (
            authority_reservation_hashes | {""}
        ):
            # Legacy registry rows may share the file, but any authority-shaped
            # event must resolve to an authority reservation in this chain.
            if row.get("final_holdout_authority_scope_hash") is not None:
                reasons.append("final_holdout_authority_orphan_event")
    request_bindings: set[tuple[str, str]] = set()
    generations: dict[str, int] = {}
    for reservation_index, reservation in enumerate(reservations):
        reasons.extend(
            _authority_reservation_contract_reasons(
                rows=rows,
                reservation=reservation,
                reservation_index=reservation_index,
            )
        )
        row_hash = str(reservation.get("row_hash") or "")
        scope_hash = str(
            reservation.get("final_holdout_authority_scope_hash") or ""
        )
        request_hash = str(reservation.get("reservation_request_hash") or "")
        generation = _as_int(reservation.get("fence_generation"))
        request_binding = (scope_hash, request_hash)
        if request_binding in request_bindings:
            reasons.append("final_holdout_authority_duplicate_request")
        request_bindings.add(request_binding)
        expected_generation = generations.get(scope_hash, 0) + 1
        if generation != expected_generation:
            reasons.append("final_holdout_authority_fence_sequence_invalid")
        generations[scope_hash] = generation or expected_generation
        activations = _activation_rows_for_reservation(rows, row_hash)
        reads = (
            _read_started_rows_for_activation(rows, str(activations[0].get("row_hash")))
            if len(activations) == 1
            else []
        )
        terminals = _terminal_rows_for_reservation(rows, row_hash)
        if len(activations) > 1:
            reasons.append("final_holdout_authority_multiple_activations")
        if len(terminals) > 1:
            reasons.append("final_holdout_authority_multiple_terminal_events")
        if len(reads) > 1:
            reasons.append("final_holdout_authority_multiple_holdout_reads")
        if require_terminal and len(terminals) != 1:
            reasons.append("final_holdout_authority_incomplete_reservation")
        if terminals:
            terminal = terminals[0]
            exposed = bool(reads)
            if bool(terminal.get("holdout_accessed")) != exposed:
                reasons.append("final_holdout_authority_access_status_mismatch")
            if exposed and terminal.get("activation_row_hash") != activations[0].get(
                "row_hash"
            ):
                reasons.append("final_holdout_authority_activation_binding_mismatch")
            if terminal.get("event_type") == "research_attempt_completed" and not exposed:
                reasons.append("final_holdout_authority_completion_without_activation")
            if (
                terminal.get("event_type") == "research_attempt_completed"
                and reservation.get("final_holdout_access_purpose")
                == FinalHoldoutAccessPurpose.INDEPENDENT_REPRODUCTION.value
            ):
                try:
                    primary = _required_primary_completion(
                        rows=rows,
                        completion_row_hash=str(
                            reservation.get("primary_completion_row_hash") or ""
                        ),
                        authority_scope_hash=str(
                            reservation.get("final_holdout_authority_scope_hash") or ""
                        ),
                        manifest_hash=str(reservation.get("manifest_hash") or ""),
                        final_holdout_result_hash=str(
                            reservation.get("primary_final_holdout_result_hash") or ""
                        ),
                    )
                    _require_independent_reproduction_matches_primary(
                        primary=primary,
                        reproduced=terminal,
                        error_code=(
                            "final_holdout_independent_completion_primary_mismatch"
                        ),
                    )
                    if activations:
                        _require_independent_reproduction_matches_primary(
                            primary=primary,
                            reproduced=activations[0],
                            error_code=(
                                "final_holdout_independent_activation_primary_mismatch"
                            ),
                            fields=(
                                "selection_artifact_hash",
                                "selected_candidate_id",
                                "selection_attempt_index",
                                "selection_holdout_reuse_count",
                            ),
                        )
                except ValueError as exc:
                    reasons.append(str(exc).split(":", 1)[0])
    unique_reasons = sorted(set(reasons))
    return {
        "status": "FAIL" if unique_reasons else "PASS",
        "row_count": len(rows),
        "reservation_count": len(reservations),
        "reasons": unique_reasons,
    }


def _require_valid_registry_chain(rows: list[dict[str, Any]]) -> None:
    reasons = experiment_registry_chain_reasons(rows)
    if reasons:
        raise ValueError("experiment_registry_chain_invalid:" + ",".join(reasons))


def _authority_reservation_contract_reasons(
    *,
    rows: list[dict[str, Any]],
    reservation: dict[str, Any],
    reservation_index: int,
) -> list[str]:
    del reservation_index
    reasons: list[str] = []
    try:
        purpose = FinalHoldoutAccessPurpose(
            str(reservation.get("final_holdout_access_purpose") or "")
        )
    except ValueError:
        return ["final_holdout_authority_access_purpose_invalid"]
    common_material = {
        "schema_version": 1,
        "purpose": purpose.value,
        "authority_scope_hash": reservation.get(
            "final_holdout_authority_scope_hash"
        ),
        "manifest_hash": reservation.get("manifest_hash"),
        "reservation_request_hash": reservation.get("reservation_request_hash"),
    }
    independent_fields = {
        "primary_completion_row_hash": reservation.get(
            "primary_completion_row_hash"
        ),
        "primary_final_holdout_result_hash": reservation.get(
            "primary_final_holdout_result_hash"
        ),
        "primary_final_holdout_confirmation_hash": reservation.get(
            "primary_final_holdout_confirmation_hash"
        ),
        "primary_reproduction_receipt_hash": reservation.get(
            "primary_reproduction_receipt_hash"
        ),
        "independent_principal_assertion_hash": reservation.get(
            "independent_principal_assertion_hash"
        ),
        "independent_principal_assertion_scope_hash": reservation.get(
            "independent_principal_assertion_scope_hash"
        ),
        "independent_principal_assertion_issuer": reservation.get(
            "independent_principal_assertion_issuer"
        ),
        "independent_principal_assertion_key_id": reservation.get(
            "independent_principal_assertion_key_id"
        ),
        "independent_principal_assertion_subject": reservation.get(
            "independent_principal_assertion_subject"
        ),
        "independent_principal_assertion_nonce": reservation.get(
            "independent_principal_assertion_nonce"
        ),
    }
    if purpose is FinalHoldoutAccessPurpose.PRIMARY_CONFIRMATION:
        expected = sha256_prefixed(
            {
                **common_material,
                "reservation_actor_binding_hash": reservation.get(
                    "reservation_actor_binding_hash"
                ),
            },
            label="final_holdout_primary_confirmation_authorization",
        )
        if any(value is not None for value in independent_fields.values()):
            reasons.append("final_holdout_primary_independent_binding_present")
    else:
        expected = sha256_prefixed(
            {**common_material, **independent_fields},
            label="final_holdout_independent_reproduction_authorization",
        )
        hash_fields = (
            "primary_completion_row_hash",
            "primary_final_holdout_result_hash",
            "primary_final_holdout_confirmation_hash",
            "primary_reproduction_receipt_hash",
            "independent_principal_assertion_hash",
            "independent_principal_assertion_scope_hash",
        )
        text_fields = (
            "independent_principal_assertion_issuer",
            "independent_principal_assertion_key_id",
            "independent_principal_assertion_subject",
            "independent_principal_assertion_nonce",
        )
        if any(
            not isinstance(independent_fields[field], str)
            or not str(independent_fields[field]).startswith("sha256:")
            for field in hash_fields
        ) or any(
            not isinstance(independent_fields[field], str)
            or not str(independent_fields[field]).strip()
            for field in text_fields
        ):
            reasons.append("final_holdout_independent_binding_invalid")
        try:
            _required_primary_completion(
                rows=rows,
                completion_row_hash=str(
                    independent_fields["primary_completion_row_hash"] or ""
                ),
                authority_scope_hash=str(
                    reservation.get("final_holdout_authority_scope_hash") or ""
                ),
                manifest_hash=str(reservation.get("manifest_hash") or ""),
                final_holdout_result_hash=str(
                    independent_fields["primary_final_holdout_result_hash"] or ""
                ),
            )
        except ValueError:
            reasons.append("final_holdout_independent_primary_binding_invalid")
        independent_reservations = [
            row
            for row in rows
            if row.get("event_type") == "research_attempt_reserved"
            and row.get("final_holdout_access_purpose") == purpose.value
        ]
        same_primary = sum(
            1
            for row in independent_reservations
            if row.get("primary_completion_row_hash")
            == independent_fields["primary_completion_row_hash"]
        )
        same_nonce = sum(
            1
            for row in independent_reservations
            if row.get("independent_principal_assertion_issuer")
            == independent_fields["independent_principal_assertion_issuer"]
            and row.get("independent_principal_assertion_key_id")
            == independent_fields["independent_principal_assertion_key_id"]
            and row.get("independent_principal_assertion_nonce")
            == independent_fields["independent_principal_assertion_nonce"]
        )
        if same_primary > INDEPENDENT_REPRODUCTION_LIMIT_PER_PRIMARY:
            reasons.append("final_holdout_independent_primary_budget_exceeded")
        if same_nonce > 1:
            reasons.append("final_holdout_independent_assertion_nonce_replayed")
    if reservation.get("final_holdout_purpose_binding_hash") != expected:
        reasons.append("final_holdout_authority_purpose_binding_hash_mismatch")
    return reasons


def _terminal_rows_for_reservation(
    rows: list[dict[str, Any]], reservation_row_hash: str
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("event_type")
        in {"research_attempt_completed", "research_attempt_aborted"}
        and str(row.get("reservation_row_hash") or "") == reservation_row_hash
    ]


def _activation_rows_for_reservation(
    rows: list[dict[str, Any]], reservation_row_hash: str
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("event_type") == "research_attempt_activated"
        and str(row.get("reservation_row_hash") or "") == reservation_row_hash
    ]


def _read_started_rows_for_activation(
    rows: list[dict[str, Any]], activation_row_hash: str
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("event_type") == "research_attempt_holdout_read_started"
        and str(row.get("activation_row_hash") or "") == activation_row_hash
    ]


def _reservation_counts_as_exposure_or_lock(
    rows: list[dict[str, Any]], reservation: dict[str, Any]
) -> bool:
    """Pending and exposed reservations fence peers; clean pre-gate aborts do not."""

    if (
        reservation.get("final_holdout_authority_contract_schema_version")
        != FINAL_HOLDOUT_AUTHORITY_CONTRACT_SCHEMA_VERSION
    ):
        return True
    row_hash = str(reservation.get("row_hash") or "")
    terminal = _terminal_rows_for_reservation(rows, row_hash)
    if not terminal:
        return True
    if _activation_rows_for_reservation(rows, row_hash):
        return True
    return any(bool(row.get("holdout_accessed")) for row in terminal)


def _matching_authority_reservation(
    rows: list[dict[str, Any]], base_payload: dict[str, Any]
) -> dict[str, Any] | None:
    request_hash = str(base_payload.get("reservation_request_hash") or "")
    scope_hash = str(base_payload.get("final_holdout_authority_scope_hash") or "")
    if not request_hash or not scope_hash:
        return None
    matches = [
        row
        for row in rows
        if row.get("event_type") == "research_attempt_reserved"
        and row.get("reservation_request_hash") == request_hash
        and row.get("final_holdout_authority_scope_hash") == scope_hash
    ]
    if len(matches) > 1:
        raise ValueError("final_holdout_reservation_idempotency_conflict")
    return matches[0] if matches else None


def _authority_replay_binding_reasons(
    existing: dict[str, Any], requested: dict[str, Any]
) -> list[str]:
    fields = (
        "reservation_request_id",
        "reservation_actor_binding_hash",
        "manifest_hash",
        "pre_exposure_dataset_identity_hash",
        "pre_exposure_reservation_key_hash",
        "final_holdout_access_purpose",
        "final_holdout_purpose_binding_hash",
    )
    return [
        f"final_holdout_reservation_replay_binding_mismatch:{field}"
        for field in fields
        if existing.get(field) != requested.get(field)
    ]


def _next_authority_fence_generation(
    rows: list[dict[str, Any]], scope_hash: str
) -> int:
    generations = [
        _as_int(row.get("fence_generation")) or 0
        for row in rows
        if row.get("event_type") == "research_attempt_reserved"
        and row.get("final_holdout_authority_scope_hash") == scope_hash
    ]
    return max(generations, default=0) + 1


def compute_research_attempt_counters(
    *,
    manager: ResearchPathManager,
    base_payload: dict[str, Any],
) -> dict[str, int]:
    path = experiment_registry_path(manager=manager)
    rows = load_experiment_registry_rows(path)
    family_id = str(base_payload.get("experiment_family_id") or "")
    hypothesis_id = str(base_payload.get("hypothesis_id") or "")
    pre_exposure_key = str(base_payload.get("pre_exposure_reservation_key_hash") or "")
    duplicate_count = sum(
        1
        for row in rows
        if row.get("event_type") == "research_attempt_reserved"
        and pre_exposure_key
        and str(row.get("pre_exposure_reservation_key_hash") or "") == pre_exposure_key
        and _reservation_counts_as_exposure_or_lock(rows, row)
    )
    return {
        "computed_attempt_index": 1
        + sum(
            1
            for row in rows
            if row.get("event_type") == "research_attempt_reserved"
            and str(row.get("experiment_family_id") or "") == family_id
            and str(row.get("hypothesis_id") or "") == hypothesis_id
        ),
        # Pre-exposure duplicate detection counts reservations by their
        # deliberately incomplete reservation identity.  Authoritative reuse
        # counts only completed v4 rows and are calculated at completion.
        "computed_pre_exposure_duplicate_count": duplicate_count,
        "computed_holdout_reuse_count": duplicate_count,
    }


def append_research_attempt_rejected(
    *,
    manager: ResearchPathManager,
    base_payload: dict[str, Any],
    reasons: list[str],
    computed_attempt_index: int,
    computed_holdout_reuse_count: int,
    created_at: str | None = None,
) -> dict[str, Any]:
    path = experiment_registry_path(manager=manager)
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        _require_valid_registry_chain(rows)
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        row = {
            "schema_version": EXPERIMENT_REGISTRY_SCHEMA_VERSION,
            "budget_policy": EXPERIMENT_REGISTRY_BUDGET_POLICY,
            "event_type": "research_attempt_rejected",
            **base_payload,
            "computed_attempt_index": computed_attempt_index,
            "computed_holdout_reuse_count": computed_holdout_reuse_count,
            "result_status": "REJECTED",
            "rejection_reasons": list(reasons),
            "counted_attempt": False,
            "prior_registry_hash": prior_hash,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        row["row_hash"] = compute_row_hash(row)
        append_authority_jsonl(path, row, require_kernel_append_only=True)
    return {
        "path": str(path.resolve()),
        "prior_hash": prior_hash,
        "row_hash": str(row["row_hash"]),
        "row": dict(row),
    }


def reserve_research_attempt(
    *,
    manager: ResearchPathManager,
    base_payload: dict[str, Any],
    created_at: str | None = None,
) -> dict[str, Any]:
    _reject_generic_independent_reproduction_payload(base_payload)
    path = experiment_registry_path(manager=manager)
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        _require_valid_registry_chain(rows)
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        counters = _compute_research_attempt_counters_from_rows(
            rows=rows, base_payload=base_payload
        )
        computed_attempt_index = counters["computed_attempt_index"]
        computed_holdout_reuse_count = counters["computed_holdout_reuse_count"]
        row = {
            "schema_version": EXPERIMENT_REGISTRY_SCHEMA_VERSION,
            "budget_policy": EXPERIMENT_REGISTRY_BUDGET_POLICY,
            "event_type": "research_attempt_reserved",
            **base_payload,
            "computed_attempt_index": computed_attempt_index,
            "computed_holdout_reuse_count": computed_holdout_reuse_count,
            "result_status": "IN_PROGRESS",
            "prior_registry_hash": prior_hash,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        row["row_hash"] = compute_row_hash(row)
        append_authority_jsonl(path, row, require_kernel_append_only=True)
    result = {
        "path": str(path.resolve()),
        "prior_hash": prior_hash,
        "row_hash": str(row["row_hash"]),
        "row": dict(row),
        "computed_attempt_index": computed_attempt_index,
        "computed_holdout_reuse_count": computed_holdout_reuse_count,
    }
    result["research_freedom_hash"] = research_freedom_hash(
        {
            **row,
            "experiment_registry_path": result["path"],
            "experiment_registry_prior_hash": prior_hash,
            "experiment_registry_row_hash": row["row_hash"],
        }
    )
    return result


def reserve_research_attempt_checked(
    *,
    manager: ResearchPathManager,
    base_payload: dict[str, Any],
    statistical_validation_contract: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    _reject_generic_independent_reproduction_payload(base_payload)
    path = experiment_registry_path(manager=manager)
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        _require_valid_registry_chain(rows)
        existing = _matching_authority_reservation(rows, base_payload)
        if existing is not None:
            replay_reasons = _authority_replay_binding_reasons(existing, base_payload)
            if replay_reasons:
                raise ValueError(
                    "final_holdout_reservation_idempotency_conflict:"
                    + ",".join(replay_reasons)
                )
            terminal = _terminal_rows_for_reservation(
                rows, str(existing.get("row_hash") or "")
            )
            if terminal:
                return {
                    "accepted": False,
                    "path": str(path.resolve()),
                    "prior_hash": str(existing.get("prior_registry_hash") or ""),
                    "row_hash": str(existing.get("row_hash") or ""),
                    "row": dict(existing),
                    "computed_attempt_index": existing.get(
                        "computed_attempt_index"
                    ),
                    "computed_holdout_reuse_count": existing.get(
                        "computed_holdout_reuse_count"
                    ),
                    "reasons": ["final_holdout_reservation_request_already_terminal"],
                }
            result = {
                "accepted": True,
                "path": str(path.resolve()),
                "prior_hash": str(existing.get("prior_registry_hash") or ""),
                "row_hash": str(existing["row_hash"]),
                "row": dict(existing),
                "computed_attempt_index": existing.get("computed_attempt_index"),
                "computed_holdout_reuse_count": existing.get(
                    "computed_holdout_reuse_count"
                ),
                "idempotent_replay": True,
            }
            result["research_freedom_hash"] = research_freedom_hash(
                {
                    **existing,
                    "experiment_registry_path": result["path"],
                    "experiment_registry_prior_hash": result["prior_hash"],
                    "experiment_registry_row_hash": result["row_hash"],
                }
            )
            return result
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        counters = _compute_research_attempt_counters_from_rows(
            rows=rows, base_payload=base_payload
        )
        computed_attempt_index = counters["computed_attempt_index"]
        computed_holdout_reuse_count = counters["computed_holdout_reuse_count"]
        reasons = _checked_reservation_reasons(
            base_payload=base_payload,
            computed_attempt_index=computed_attempt_index,
            computed_holdout_reuse_count=computed_holdout_reuse_count,
            statistical_validation_contract=statistical_validation_contract,
        )
        authority_scope_hash = str(
            base_payload.get("final_holdout_authority_scope_hash") or ""
        )
        if authority_scope_hash and any(
            row.get("event_type") == "research_attempt_reserved"
            and row.get("final_holdout_authority_scope_hash")
            == authority_scope_hash
            and _reservation_counts_as_exposure_or_lock(rows, row)
            for row in rows
        ):
            # The configured statistical reuse budget governs broader research
            # freedom.  The operated authority is intentionally stricter: one
            # manifest/dataset/holdout scope may be exposed only once.  A
            # clean pre-gate abort is the sole case that releases this fence.
            reasons.append("final_holdout_authority_scope_already_exposed")
        if reasons:
            row = {
                "schema_version": EXPERIMENT_REGISTRY_SCHEMA_VERSION,
                "budget_policy": EXPERIMENT_REGISTRY_BUDGET_POLICY,
                "event_type": "research_attempt_rejected",
                **base_payload,
                "computed_attempt_index": computed_attempt_index,
                "computed_holdout_reuse_count": computed_holdout_reuse_count,
                "result_status": "REJECTED",
                "rejection_reasons": sorted(set(reasons)),
                "counted_attempt": False,
                "prior_registry_hash": prior_hash,
                "created_at": created_at or datetime.now(timezone.utc).isoformat(),
            }
            row["row_hash"] = compute_row_hash(row)
            append_authority_jsonl(path, row, require_kernel_append_only=True)
            return {
                "accepted": False,
                "path": str(path.resolve()),
                "prior_hash": prior_hash,
                "row_hash": str(row["row_hash"]),
                "row": dict(row),
                "computed_attempt_index": computed_attempt_index,
                "computed_holdout_reuse_count": computed_holdout_reuse_count,
                "reasons": list(row["rejection_reasons"]),
            }
        authority_fields = (
            {
                "fence_generation": _next_authority_fence_generation(
                    rows, authority_scope_hash
                ),
                "holdout_access_status": "PENDING_PRE_HOLDOUT_GATES",
            }
            if authority_scope_hash
            else {}
        )
        row = {
            "schema_version": EXPERIMENT_REGISTRY_SCHEMA_VERSION,
            "budget_policy": EXPERIMENT_REGISTRY_BUDGET_POLICY,
            "event_type": "research_attempt_reserved",
            **base_payload,
            **authority_fields,
            "computed_attempt_index": computed_attempt_index,
            "computed_holdout_reuse_count": computed_holdout_reuse_count,
            "result_status": "IN_PROGRESS",
            "prior_registry_hash": prior_hash,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        row["row_hash"] = compute_row_hash(row)
        append_authority_jsonl(path, row, require_kernel_append_only=True)
    result = {
        "accepted": True,
        "path": str(path.resolve()),
        "prior_hash": prior_hash,
        "row_hash": str(row["row_hash"]),
        "row": dict(row),
        "computed_attempt_index": computed_attempt_index,
        "computed_holdout_reuse_count": computed_holdout_reuse_count,
    }
    result["research_freedom_hash"] = research_freedom_hash(
        {
            **row,
            "experiment_registry_path": result["path"],
            "experiment_registry_prior_hash": prior_hash,
            "experiment_registry_row_hash": row["row_hash"],
        }
    )
    return result


def _reject_generic_independent_reproduction_payload(
    base_payload: dict[str, Any],
) -> None:
    """Keep trusted replay authorization out of general reservation APIs.

    Independent reproduction is not a role/name supplied by a caller.  Only
    :func:`reserve_independent_reproduction_holdout_authority` may construct
    that row after re-verifying the signed principal assertion, terminal
    receipt, primary completion, and per-primary budget.
    """

    if (
        base_payload.get("final_holdout_access_purpose")
        == FinalHoldoutAccessPurpose.INDEPENDENT_REPRODUCTION.value
    ):
        raise ValueError(
            "independent_reproduction_requires_trusted_reservation_api"
        )


def reserve_final_holdout_authority(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    request_id: str,
    request_hash: str,
    actor_binding_hash: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Reserve one durable, cross-adapter holdout fence before sandbox launch."""

    final_holdout = getattr(
        getattr(getattr(manifest, "dataset", None), "split", None),
        "final_holdout",
        None,
    )
    if final_holdout is None:
        raise ValueError("final_holdout_missing")
    manifest_hash = str(manifest.manifest_hash())
    dataset_identity_hash = final_holdout_dataset_identity_hash(manifest)
    authority_scope_hash = final_holdout_authority_scope_hash(manifest)
    objective_metric = objective_metric_from_manifest(manifest)
    pre_exposure_key = pre_exposure_reservation_key_hash_from_parts(
        strategy_name=getattr(manifest, "strategy_name", None),
        market=getattr(manifest, "market", None),
        interval=getattr(manifest, "interval", None),
        final_holdout=final_holdout.as_dict(),
        objective_metric=objective_metric,
        dataset_artifact_evidence_hash=dataset_identity_hash,
    )
    if pre_exposure_key is None:
        raise ValueError("final_holdout_pre_exposure_identity_incomplete")
    identity = research_identity_from_manifest(manifest)
    raw = getattr(manifest, "raw", {})
    raw = raw if isinstance(raw, dict) else {}
    reservation_request_hash = final_holdout_reservation_request_hash(
        request_id=request_id, request_hash=request_hash
    )
    purpose_binding_hash = sha256_prefixed(
        {
            "schema_version": 1,
            "purpose": FinalHoldoutAccessPurpose.PRIMARY_CONFIRMATION.value,
            "authority_scope_hash": authority_scope_hash,
            "manifest_hash": manifest_hash,
            "reservation_request_hash": reservation_request_hash,
            "reservation_actor_binding_hash": actor_binding_hash,
        },
        label="final_holdout_primary_confirmation_authorization",
    )
    base_payload = {
        "final_holdout_authority_contract_schema_version": (
            FINAL_HOLDOUT_AUTHORITY_CONTRACT_SCHEMA_VERSION
        ),
        "final_holdout_authority_scope_hash": authority_scope_hash,
        "reservation_request_hash": reservation_request_hash,
        "reservation_request_id": str(request_id),
        "reservation_actor_binding_hash": actor_binding_hash,
        "final_holdout_access_purpose": (
            FinalHoldoutAccessPurpose.PRIMARY_CONFIRMATION.value
        ),
        "final_holdout_purpose_binding_hash": purpose_binding_hash,
        "primary_completion_row_hash": None,
        "primary_final_holdout_result_hash": None,
        "primary_final_holdout_confirmation_hash": None,
        "primary_reproduction_receipt_hash": None,
        "independent_principal_assertion_hash": None,
        "independent_principal_assertion_scope_hash": None,
        "independent_principal_assertion_issuer": None,
        "independent_principal_assertion_key_id": None,
        "independent_principal_assertion_subject": None,
        "independent_principal_assertion_nonce": None,
        "run_id": str(request_id),
        "experiment_id": getattr(manifest, "experiment_id", None),
        "experiment_family_id": identity["experiment_family_id"],
        "hypothesis_id": identity["hypothesis_id"],
        "hypothesis_version": identity["hypothesis_version"],
        "hypothesis_contract_hash": identity["hypothesis_contract_hash"],
        "hypothesis_semantic_fingerprint": identity[
            "hypothesis_semantic_fingerprint"
        ],
        "hypothesis_lineage_hash": identity["hypothesis_lineage_hash"],
        "research_question_id": identity["research_question_id"],
        "research_question_version": identity["research_question_version"],
        "research_question_hash": identity["research_question_hash"],
        "observation_hashes": identity["observation_hashes"],
        "hypothesis_status": identity["hypothesis_status"],
        "hypothesis_identity_source": identity["hypothesis_identity_source"],
        "experiment_family_identity_source": identity[
            "experiment_family_identity_source"
        ],
        "pre_registered_at": identity["pre_registered_at"],
        "registration_evidence_hash": identity["registration_evidence_hash"],
        "manifest_hash": manifest_hash,
        "research_classification": getattr(
            manifest, "research_classification", None
        ),
        "dataset_snapshot_id": getattr(manifest.dataset, "snapshot_id", None),
        "pre_exposure_dataset_identity_hash": dataset_identity_hash,
        "final_holdout_identity_hash": final_holdout_identity_hash_from_parts(
            dataset_source=getattr(manifest.dataset, "source", None),
            market=getattr(manifest, "market", None),
            interval=getattr(manifest, "interval", None),
            final_holdout=final_holdout.as_dict(),
        ),
        "pre_exposure_reservation_key_hash": pre_exposure_key,
        "pre_exposure_reservation_key_schema_version": (
            PRE_EXPOSURE_RESERVATION_KEY_SCHEMA_VERSION
        ),
        "final_holdout_content_pending_until_completion": True,
        "objective_metric": objective_metric,
        "selection_artifact_hash": None,
        "selected_candidate_id": None,
        "declared_attempt_index": _as_int(raw.get("attempt_index")),
        "declared_holdout_reuse_count": _as_int(raw.get("holdout_reuse_count")),
        "selection_attempt_index": None,
        "selection_holdout_reuse_count": None,
    }
    statistical_contract = (
        manifest.statistical_validation.as_dict()
        if getattr(manifest, "statistical_validation", None) is not None
        else {"gates": {"max_holdout_reuse_count": 0}}
    )
    reservation = reserve_research_attempt_checked(
        manager=manager,
        base_payload=base_payload,
        statistical_validation_contract=statistical_contract,
        created_at=created_at,
    )
    if not reservation.get("accepted"):
        raise ValueError(
            "final_holdout_pre_exposure_authorization_failed:"
            + ",".join(str(item) for item in reservation.get("reasons") or [])
        )
    reservation["transport"] = final_holdout_reservation_transport(reservation)
    return reservation


_INDEPENDENT_REPRODUCTION_CLONED_FIELDS = (
    "experiment_id",
    "experiment_family_id",
    "hypothesis_id",
    "hypothesis_version",
    "hypothesis_contract_hash",
    "hypothesis_semantic_fingerprint",
    "hypothesis_lineage_hash",
    "research_question_id",
    "research_question_version",
    "research_question_hash",
    "observation_hashes",
    "hypothesis_status",
    "hypothesis_identity_source",
    "experiment_family_identity_source",
    "pre_registered_at",
    "registration_evidence_hash",
    "manifest_hash",
    "research_classification",
    "dataset_snapshot_id",
    "pre_exposure_dataset_identity_hash",
    "final_holdout_identity_hash",
    "pre_exposure_reservation_key_hash",
    "pre_exposure_reservation_key_schema_version",
    "objective_metric",
    "declared_attempt_index",
    "declared_holdout_reuse_count",
)


def reserve_independent_reproduction_holdout_authority(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    request_id: str,
    request_hash: str,
    primary_completion_row_hash: str,
    baseline_receipt_path: str | Path,
    principal_assertion: object,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Reserve the sole bounded, authenticated replay of a completed primary.

    This is intentionally a different API from
    :func:`reserve_final_holdout_authority`: callers cannot turn an actor name,
    role string, or generic reservation payload into an independent replay.
    The assertion is re-verified here against the configured external trust
    store and the exact terminal receipt before any authority row is appended.
    """

    authorization = _verified_independent_reproduction_authorization(
        manager=manager,
        manifest=manifest,
        primary_completion_row_hash=primary_completion_row_hash,
        baseline_receipt_path=baseline_receipt_path,
        principal_assertion=principal_assertion,
    )
    authority_scope_hash = final_holdout_authority_scope_hash(manifest)
    manifest_hash = str(manifest.manifest_hash())
    reservation_request_hash = final_holdout_reservation_request_hash(
        request_id=request_id,
        request_hash=request_hash,
    )
    purpose_material = {
        "schema_version": 1,
        "purpose": FinalHoldoutAccessPurpose.INDEPENDENT_REPRODUCTION.value,
        "authority_scope_hash": authority_scope_hash,
        "manifest_hash": manifest_hash,
        "reservation_request_hash": reservation_request_hash,
        **authorization,
    }
    purpose_binding_hash = sha256_prefixed(
        purpose_material,
        label="final_holdout_independent_reproduction_authorization",
    )
    path = experiment_registry_path(manager=manager)
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        _require_valid_registry_chain(rows)
        primary = _required_primary_completion(
            rows=rows,
            completion_row_hash=primary_completion_row_hash,
            authority_scope_hash=authority_scope_hash,
            manifest_hash=manifest_hash,
            final_holdout_result_hash=str(
                authorization["primary_final_holdout_result_hash"]
            ),
        )
        base_payload = {
            "final_holdout_authority_contract_schema_version": (
                FINAL_HOLDOUT_AUTHORITY_CONTRACT_SCHEMA_VERSION
            ),
            "final_holdout_authority_scope_hash": authority_scope_hash,
            "reservation_request_hash": reservation_request_hash,
            "reservation_request_id": str(request_id),
            "reservation_actor_binding_hash": authorization[
                "independent_principal_assertion_hash"
            ],
            "final_holdout_access_purpose": (
                FinalHoldoutAccessPurpose.INDEPENDENT_REPRODUCTION.value
            ),
            "final_holdout_purpose_binding_hash": purpose_binding_hash,
            **authorization,
            "run_id": str(request_id),
            **{
                field: primary.get(field)
                for field in _INDEPENDENT_REPRODUCTION_CLONED_FIELDS
            },
            "final_holdout_content_pending_until_completion": True,
            "selection_artifact_hash": None,
            "selected_candidate_id": None,
            "selection_attempt_index": None,
            "selection_holdout_reuse_count": None,
        }
        existing = _matching_authority_reservation(rows, base_payload)
        if existing is not None:
            replay_reasons = _authority_replay_binding_reasons(existing, base_payload)
            if replay_reasons:
                raise ValueError(
                    "final_holdout_reservation_idempotency_conflict:"
                    + ",".join(replay_reasons)
                )
            if _terminal_rows_for_reservation(rows, str(existing.get("row_hash") or "")):
                raise ValueError("final_holdout_reservation_request_already_terminal")
            result = _reservation_result(
                path=path,
                row=existing,
                idempotent_replay=True,
            )
            result["transport"] = final_holdout_reservation_transport(result)
            return result
        _require_independent_reproduction_budget(
            rows=rows,
            primary_completion_row_hash=primary_completion_row_hash,
            assertion_issuer=str(
                authorization["independent_principal_assertion_issuer"]
            ),
            assertion_key_id=str(
                authorization["independent_principal_assertion_key_id"]
            ),
            assertion_nonce=str(
                authorization["independent_principal_assertion_nonce"]
            ),
            assertion_hash=str(
                authorization["independent_principal_assertion_hash"]
            ),
        )
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        row = {
            "schema_version": EXPERIMENT_REGISTRY_SCHEMA_VERSION,
            "budget_policy": EXPERIMENT_REGISTRY_BUDGET_POLICY,
            "event_type": "research_attempt_reserved",
            **base_payload,
            "fence_generation": _next_authority_fence_generation(
                rows, authority_scope_hash
            ),
            "holdout_access_status": "PENDING_PRE_HOLDOUT_GATES",
            "computed_attempt_index": primary.get("computed_attempt_index"),
            "computed_holdout_reuse_count": primary.get(
                "selection_holdout_reuse_count",
                primary.get("computed_holdout_reuse_count"),
            ),
            "result_status": "IN_PROGRESS",
            "prior_registry_hash": prior_hash,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        row["row_hash"] = compute_row_hash(row)
        append_authority_jsonl(path, row, require_kernel_append_only=True)
    result = _reservation_result(path=path, row=row)
    result["transport"] = final_holdout_reservation_transport(result)
    return result


def _reservation_result(
    *,
    path: Path,
    row: dict[str, Any],
    idempotent_replay: bool = False,
) -> dict[str, Any]:
    result = {
        "accepted": True,
        "path": str(path.resolve()),
        "prior_hash": str(row.get("prior_registry_hash") or ""),
        "row_hash": str(row["row_hash"]),
        "row": dict(row),
        "computed_attempt_index": row.get("computed_attempt_index"),
        "computed_holdout_reuse_count": row.get("computed_holdout_reuse_count"),
    }
    if idempotent_replay:
        result["idempotent_replay"] = True
    result["research_freedom_hash"] = research_freedom_hash(
        {
            **row,
            "experiment_registry_path": result["path"],
            "experiment_registry_prior_hash": result["prior_hash"],
            "experiment_registry_row_hash": result["row_hash"],
        }
    )
    return result


def _verified_independent_reproduction_authorization(
    *,
    manager: ResearchPathManager,
    manifest: Any,
    primary_completion_row_hash: str,
    baseline_receipt_path: str | Path,
    principal_assertion: object,
) -> dict[str, str]:
    from .principal_assertion import (
        INDEPENDENT_VERIFIER_ROLE,
        IndependentVerificationAssertionScope,
        PrincipalAssertion,
        verify_principal_assertion,
    )
    from .independent_verification import independent_reproduction_evidence
    from .reproduction import load_reproduction_receipt

    if not isinstance(principal_assertion, PrincipalAssertion):
        raise ValueError("independent_reproduction_principal_assertion_required")
    if INDEPENDENT_VERIFIER_ROLE not in principal_assertion.roles:
        raise ValueError("independent_reproduction_principal_role_invalid")
    manifest_hash = str(manifest.manifest_hash())
    experiment_id = str(getattr(manifest, "experiment_id", "") or "")
    baseline_path = Path(baseline_receipt_path).expanduser()
    expected_baseline_path = manager.report_path(
        "research",
        experiment_id,
        "validated_research_reproduction_receipt.json",
    ).resolve()
    if baseline_path.is_symlink() or baseline_path.resolve() != expected_baseline_path:
        raise ValueError("independent_reproduction_baseline_receipt_path_invalid")
    receipt = load_reproduction_receipt(baseline_path)
    binding = receipt.get("source_evidence_binding")
    if (
        receipt.get("evidence_scope") != "validated_research_result"
        or receipt.get("experiment_id") != experiment_id
        or receipt.get("manifest_hash") != manifest_hash
        or not isinstance(binding, dict)
    ):
        raise ValueError("independent_reproduction_baseline_receipt_invalid")
    # Resolve and re-hash the terminal source report now.  A self-consistent
    # receipt plus a signature is not enough if its claimed source artifact is
    # absent or has drifted.
    independent_reproduction_evidence(
        manager=manager,
        baseline_receipt_path=baseline_path,
    )
    expected_scope = IndependentVerificationAssertionScope(
        verification_id=principal_assertion.scope.verification_id,
        verification_version=principal_assertion.scope.verification_version,
        experiment_id=experiment_id,
        research_version=manifest_hash,
        source_report_hash=str(receipt.get("source_report_hash") or ""),
        baseline_receipt_hash=str(receipt.get("receipt_content_hash") or ""),
    )
    verify_principal_assertion(
        assertion=principal_assertion,
        expected_scope=expected_scope,
        trust_store_path=manager.settings.independent_verifier_trust_store_path,
        manager=manager,
    )
    confirmation_path = manager.report_path(
        "research", experiment_id, "final_holdout_confirmation.json"
    )
    if confirmation_path.is_symlink():
        raise ValueError("independent_reproduction_primary_confirmation_invalid")
    try:
        confirmation = json.loads(confirmation_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(
            "independent_reproduction_primary_confirmation_invalid"
        ) from exc
    if not isinstance(confirmation, dict):
        raise ValueError("independent_reproduction_primary_confirmation_invalid")
    confirmation_material = {
        key: value for key, value in confirmation.items() if key != "content_hash"
    }
    confirmation_hash = sha256_prefixed(
        confirmation_material,
        label="final_holdout_confirmation",
    )
    if (
        confirmation.get("content_hash") != confirmation_hash
        or binding.get("final_holdout_confirmation_hash") != confirmation_hash
        or binding.get("final_holdout_result_hash")
        != confirmation.get("final_holdout_result_hash")
        or confirmation.get("manifest_hash") != manifest_hash
        or confirmation.get("experiment_registry_completion_row_hash")
        != primary_completion_row_hash
        or Path(str(confirmation.get("experiment_registry_path") or "")).resolve()
        != experiment_registry_path(manager=manager).resolve()
    ):
        raise ValueError("independent_reproduction_primary_confirmation_invalid")
    return {
        "primary_completion_row_hash": primary_completion_row_hash,
        "primary_final_holdout_result_hash": str(
            confirmation["final_holdout_result_hash"]
        ),
        "primary_final_holdout_confirmation_hash": confirmation_hash,
        "primary_reproduction_receipt_hash": str(receipt["receipt_content_hash"]),
        "independent_principal_assertion_hash": principal_assertion.content_hash,
        "independent_principal_assertion_scope_hash": expected_scope.content_hash(),
        "independent_principal_assertion_issuer": principal_assertion.issuer,
        "independent_principal_assertion_key_id": principal_assertion.key_id,
        "independent_principal_assertion_subject": principal_assertion.subject,
        "independent_principal_assertion_nonce": principal_assertion.nonce,
    }


def _required_primary_completion(
    *,
    rows: list[dict[str, Any]],
    completion_row_hash: str,
    authority_scope_hash: str,
    manifest_hash: str,
    final_holdout_result_hash: str,
) -> dict[str, Any]:
    primary = next(
        (
            row
            for row in rows
            if row.get("event_type") == "research_attempt_completed"
            and row.get("row_hash") == completion_row_hash
        ),
        None,
    )
    if (
        not isinstance(primary, dict)
        or primary.get("result_status") != "COMPLETED"
        or primary.get("final_holdout_access_purpose")
        != FinalHoldoutAccessPurpose.PRIMARY_CONFIRMATION.value
        or primary.get("final_holdout_authority_scope_hash") != authority_scope_hash
        or primary.get("manifest_hash") != manifest_hash
        or primary.get("final_holdout_result_hash") != final_holdout_result_hash
        or primary.get("holdout_access_status") != "EXPOSURE_COMPLETED"
        or primary.get("holdout_accessed") is not True
        or primary.get("confirmation_gate_result") != "PASS"
    ):
        raise ValueError("independent_reproduction_primary_completion_invalid")
    for field in (
        "dataset_artifact_evidence_hash",
        "final_holdout_query_hash",
        "final_holdout_data_hash",
        "final_holdout_fingerprint_hash",
        "final_holdout_quality_hash",
        "final_holdout_reuse_key_hash",
        "final_holdout_result_hash",
        "selection_artifact_hash",
    ):
        value = primary.get(field)
        if not isinstance(value, str) or not value.startswith("sha256:"):
            raise ValueError("independent_reproduction_primary_completion_invalid")
    if (
        not isinstance(primary.get("selected_candidate_id"), str)
        or not str(primary["selected_candidate_id"]).strip()
        or _as_int(primary.get("selection_attempt_index")) is None
        or _as_int(primary.get("selection_holdout_reuse_count")) is None
    ):
        raise ValueError("independent_reproduction_primary_completion_invalid")
    return primary


def _require_independent_reproduction_matches_primary(
    *,
    primary: dict[str, Any],
    reproduced: dict[str, Any],
    error_code: str,
    fields: tuple[str, ...] = _INDEPENDENT_REPRODUCTION_PRIMARY_EQUAL_FIELDS,
) -> None:
    mismatches = [
        field
        for field in fields
        if reproduced.get(field) != primary.get(field)
    ]
    if mismatches:
        raise ValueError(error_code + ":" + ",".join(sorted(mismatches)))


def _require_independent_reproduction_budget(
    *,
    rows: list[dict[str, Any]],
    primary_completion_row_hash: str,
    assertion_issuer: str,
    assertion_key_id: str,
    assertion_nonce: str,
    assertion_hash: str,
) -> None:
    independent = [
        row
        for row in rows
        if row.get("event_type") == "research_attempt_reserved"
        and row.get("final_holdout_access_purpose")
        == FinalHoldoutAccessPurpose.INDEPENDENT_REPRODUCTION.value
    ]
    if any(
        row.get("independent_principal_assertion_hash") == assertion_hash
        or (
            row.get("independent_principal_assertion_issuer") == assertion_issuer
            and row.get("independent_principal_assertion_key_id") == assertion_key_id
            and row.get("independent_principal_assertion_nonce") == assertion_nonce
        )
        for row in independent
    ):
        raise ValueError("independent_reproduction_principal_assertion_replayed")
    count = sum(
        1
        for row in independent
        if row.get("primary_completion_row_hash") == primary_completion_row_hash
    )
    if count >= INDEPENDENT_REPRODUCTION_LIMIT_PER_PRIMARY:
        raise ValueError("independent_reproduction_primary_budget_exhausted")


def final_holdout_reservation_transport(
    reservation: dict[str, Any]
) -> dict[str, Any]:
    row = reservation.get("row")
    if not isinstance(row, dict):
        raise ValueError("final_holdout_reservation_row_missing")
    path = str(reservation.get("path") or "").strip()
    material = {
        "schema_version": FINAL_HOLDOUT_RESERVATION_SCHEMA_VERSION,
        "registry_path": path,
        "registry_path_hash": sha256_prefixed({"registry_path": path}),
        "reservation_row_hash": str(row.get("row_hash") or ""),
        "authority_scope_hash": str(
            row.get("final_holdout_authority_scope_hash") or ""
        ),
        "manifest_hash": str(row.get("manifest_hash") or ""),
        "pre_exposure_reservation_key_hash": str(
            row.get("pre_exposure_reservation_key_hash") or ""
        ),
        "reservation_request_hash": str(row.get("reservation_request_hash") or ""),
        "access_purpose": str(row.get("final_holdout_access_purpose") or ""),
        "purpose_binding_hash": str(
            row.get("final_holdout_purpose_binding_hash") or ""
        ),
        "fence_generation": row.get("fence_generation"),
    }
    _require_reservation_transport_material(material)
    return {
        **material,
        "content_hash": sha256_prefixed(
            material, label="final_holdout_reservation_transport"
        ),
    }


def validate_final_holdout_reservation_transport(
    *, manager: ResearchPathManager, reservation: object
) -> dict[str, Any]:
    required_fields = {
        "schema_version",
        "registry_path",
        "registry_path_hash",
        "reservation_row_hash",
        "authority_scope_hash",
        "manifest_hash",
        "pre_exposure_reservation_key_hash",
        "reservation_request_hash",
        "access_purpose",
        "purpose_binding_hash",
        "fence_generation",
        "content_hash",
    }
    if not isinstance(reservation, dict) or set(reservation) != required_fields:
        raise ValueError("final_holdout_reservation_transport_fields_invalid")
    material = {key: value for key, value in reservation.items() if key != "content_hash"}
    _require_reservation_transport_material(material)
    if reservation.get("content_hash") != sha256_prefixed(
        material, label="final_holdout_reservation_transport"
    ):
        raise ValueError("final_holdout_reservation_transport_hash_mismatch")
    path = Path(str(reservation["registry_path"])).resolve()
    if path != experiment_registry_path(manager=manager).resolve():
        raise ValueError("final_holdout_reservation_registry_path_mismatch")
    if reservation.get("registry_path_hash") != sha256_prefixed(
        {"registry_path": str(path)}
    ):
        raise ValueError("final_holdout_reservation_registry_path_hash_mismatch")
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        _require_valid_registry_chain(rows)
        row = next(
            (
                item
                for item in rows
                if item.get("event_type") == "research_attempt_reserved"
                and item.get("row_hash") == reservation["reservation_row_hash"]
            ),
            None,
        )
        if not isinstance(row, dict):
            raise ValueError("final_holdout_reservation_missing")
        expected = final_holdout_reservation_transport(
            {"path": str(path), "row": row}
        )
        if expected != reservation:
            raise ValueError("final_holdout_reservation_transport_stale_or_tampered")
        if _terminal_rows_for_reservation(rows, str(row["row_hash"])):
            raise ValueError("final_holdout_reservation_already_terminal")
    return dict(row)


def pre_holdout_gate_artifact_path(
    *,
    manager: ResearchPathManager,
    experiment_id: str,
    gate_hash: str,
) -> Path:
    _require_sha256(gate_hash, "pre_holdout_gate_hash")
    return manager.research_artifact_path(
        experiment_id,
        "pre_holdout_gates",
        gate_hash.removeprefix("sha256:") + ".json",
    ).resolve()


def publish_pre_holdout_gate_artifact(
    *,
    manager: ResearchPathManager,
    experiment_id: str,
    material: dict[str, Any],
) -> dict[str, Any]:
    """Immutably publish the gate object whose digest authorizes exposure."""

    _validate_pre_holdout_gate_material(material, require_pass=False)
    gate_hash = sha256_prefixed(material, label="pre_holdout_validation_gate")
    artifact = {**material, "content_hash": gate_hash}
    write_json_atomic_create_or_verify(
        pre_holdout_gate_artifact_path(
            manager=manager,
            experiment_id=experiment_id,
            gate_hash=gate_hash,
        ),
        artifact,
    )
    return artifact


def _load_pre_holdout_gate_artifact(
    *,
    manager: ResearchPathManager,
    experiment_id: str,
    gate_hash: str,
) -> dict[str, Any]:
    path = pre_holdout_gate_artifact_path(
        manager=manager,
        experiment_id=experiment_id,
        gate_hash=gate_hash,
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("pre_holdout_gate_artifact_missing") from exc
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise ValueError("pre_holdout_gate_artifact_access_invalid")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("pre_holdout_gate_artifact_invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError("pre_holdout_gate_artifact_invalid")
    material = {key: item for key, item in value.items() if key != "content_hash"}
    if value.get("content_hash") != gate_hash or gate_hash != sha256_prefixed(
        material,
        label="pre_holdout_validation_gate",
    ):
        raise ValueError("pre_holdout_gate_artifact_hash_mismatch")
    _validate_pre_holdout_gate_material(material, require_pass=True)
    return value


def _validate_pre_holdout_gate_material(
    material: dict[str, Any], *, require_pass: bool
) -> None:
    expected_fields = {
        "schema_version",
        "artifact_type",
        "final_holdout_authority_scope_hash",
        "manifest_hash",
        "selection_report_hash",
        "selection_artifact_hash",
        "selected_candidate_id",
        "validation_experiment_bundle_hash",
        "native_validation_computation_receipt_hash",
        "gate_result",
        "gate_reasons",
    }
    if (
        set(material) != expected_fields
        or material.get("schema_version") != PRE_HOLDOUT_GATE_ARTIFACT_SCHEMA_VERSION
        or material.get("artifact_type") != "pre_holdout_validation_gate"
    ):
        raise ValueError("pre_holdout_gate_artifact_fields_invalid")
    for field in (
        "final_holdout_authority_scope_hash",
        "manifest_hash",
        "selection_report_hash",
        "selection_artifact_hash",
    ):
        _require_sha256(material.get(field), "pre_holdout_gate_" + field)
    for field in (
        "validation_experiment_bundle_hash",
        "native_validation_computation_receipt_hash",
    ):
        value = material.get(field)
        if value is not None:
            _require_sha256(value, "pre_holdout_gate_" + field)
    if not str(material.get("selected_candidate_id") or "").strip():
        raise ValueError("pre_holdout_gate_selected_candidate_id_invalid")
    if material.get("gate_result") not in {"PASS", "FAIL"} or not isinstance(
        material.get("gate_reasons"), list
    ):
        raise ValueError("pre_holdout_gate_result_invalid")
    if require_pass and (
        material.get("gate_result") != "PASS" or material.get("gate_reasons") != []
    ):
        raise ValueError("pre_holdout_gate_not_passed")


def _require_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith(
        "sha256:"
    ):
        raise ValueError(label + "_invalid")
    try:
        int(value.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise ValueError(label + "_invalid") from exc


def _require_reservation_transport_material(material: dict[str, Any]) -> None:
    if material.get("schema_version") != FINAL_HOLDOUT_RESERVATION_SCHEMA_VERSION:
        raise ValueError("final_holdout_reservation_schema_version_invalid")
    for field in (
        "registry_path_hash",
        "reservation_row_hash",
        "authority_scope_hash",
        "manifest_hash",
        "pre_exposure_reservation_key_hash",
        "reservation_request_hash",
        "purpose_binding_hash",
    ):
        value = material.get(field)
        if not isinstance(value, str) or not value.startswith("sha256:"):
            raise ValueError(f"final_holdout_reservation_{field}_invalid")
    path = str(material.get("registry_path") or "").strip()
    if not path or not Path(path).is_absolute():
        raise ValueError("final_holdout_reservation_registry_path_invalid")
    generation = material.get("fence_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("final_holdout_reservation_fence_generation_invalid")
    try:
        FinalHoldoutAccessPurpose(str(material.get("access_purpose") or ""))
    except ValueError as exc:
        raise ValueError("final_holdout_reservation_access_purpose_invalid") from exc


def activate_final_holdout_reservation(
    *,
    manager: ResearchPathManager,
    reservation: dict[str, Any],
    manifest_hash: str,
    selection_artifact_hash: str,
    selected_candidate_id: str,
    selection_attempt_index: int,
    selection_holdout_reuse_count: int,
    pre_holdout_gate_hash: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Fence one reservation immediately before any final-holdout read."""

    authoritative = validate_final_holdout_reservation_transport(
        manager=manager, reservation=reservation
    )
    for field, value in (
        ("manifest_hash", manifest_hash),
        ("selection_artifact_hash", selection_artifact_hash),
        ("pre_holdout_gate_hash", pre_holdout_gate_hash),
    ):
        if not isinstance(value, str) or not value.startswith("sha256:"):
            raise ValueError(f"final_holdout_activation_{field}_invalid")
    gate_artifact = _load_pre_holdout_gate_artifact(
        manager=manager,
        experiment_id=str(authoritative.get("experiment_id") or ""),
        gate_hash=pre_holdout_gate_hash,
    )
    expected_gate_bindings = {
        "final_holdout_authority_scope_hash": authoritative.get(
            "final_holdout_authority_scope_hash"
        ),
        "manifest_hash": manifest_hash,
        "selection_artifact_hash": selection_artifact_hash,
        "selected_candidate_id": selected_candidate_id,
    }
    if any(
        gate_artifact.get(field) != expected
        for field, expected in expected_gate_bindings.items()
    ):
        raise ValueError("pre_holdout_gate_artifact_binding_mismatch")
    if authoritative.get("manifest_hash") != manifest_hash:
        raise ValueError("final_holdout_activation_manifest_hash_mismatch")
    if not isinstance(selected_candidate_id, str) or not selected_candidate_id.strip():
        raise ValueError("final_holdout_activation_selected_candidate_invalid")
    for count_field, count_value in (
        ("selection_attempt_index", selection_attempt_index),
        ("selection_holdout_reuse_count", selection_holdout_reuse_count),
    ):
        if (
            isinstance(count_value, bool)
            or not isinstance(count_value, int)
            or count_value < 0
        ):
            raise ValueError(f"final_holdout_activation_{count_field}_invalid")
    path = experiment_registry_path(manager=manager)
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        _require_valid_registry_chain(rows)
        row_hash = str(reservation["reservation_row_hash"])
        row = next(
            (
                item
                for item in rows
                if item.get("event_type") == "research_attempt_reserved"
                and item.get("row_hash") == row_hash
            ),
            None,
        )
        if not isinstance(row, dict) or row != authoritative:
            raise ValueError("final_holdout_activation_reservation_stale")
        if _terminal_rows_for_reservation(rows, row_hash):
            raise ValueError("final_holdout_activation_reservation_terminal")
        if _activation_rows_for_reservation(rows, row_hash):
            raise ValueError("final_holdout_activation_fence_already_used")
        scope_hash = str(row.get("final_holdout_authority_scope_hash") or "")
        latest_generation = max(
            (
                _as_int(item.get("fence_generation")) or 0
                for item in rows
                if item.get("event_type") == "research_attempt_reserved"
                and item.get("final_holdout_authority_scope_hash") == scope_hash
            ),
            default=0,
        )
        if latest_generation != reservation["fence_generation"]:
            raise ValueError("final_holdout_activation_stale_fence")
        if (
            row.get("final_holdout_access_purpose")
            == FinalHoldoutAccessPurpose.INDEPENDENT_REPRODUCTION.value
        ):
            primary = _required_primary_completion(
                rows=rows,
                completion_row_hash=str(row.get("primary_completion_row_hash") or ""),
                authority_scope_hash=scope_hash,
                manifest_hash=manifest_hash,
                final_holdout_result_hash=str(
                    row.get("primary_final_holdout_result_hash") or ""
                ),
            )
            _require_independent_reproduction_matches_primary(
                primary=primary,
                reproduced={
                    "selection_artifact_hash": selection_artifact_hash,
                    "selected_candidate_id": selected_candidate_id,
                    "selection_attempt_index": selection_attempt_index,
                    "selection_holdout_reuse_count": selection_holdout_reuse_count,
                },
                error_code=(
                    "final_holdout_independent_activation_primary_mismatch"
                ),
                fields=(
                    "selection_artifact_hash",
                    "selected_candidate_id",
                    "selection_attempt_index",
                    "selection_holdout_reuse_count",
                ),
            )
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        activation = {
            "schema_version": EXPERIMENT_REGISTRY_SCHEMA_VERSION,
            "budget_policy": EXPERIMENT_REGISTRY_BUDGET_POLICY,
            "event_type": "research_attempt_activated",
            "reservation_row_hash": row_hash,
            "final_holdout_authority_scope_hash": scope_hash,
            "reservation_request_hash": row.get("reservation_request_hash"),
            "fence_generation": reservation["fence_generation"],
            "manifest_hash": manifest_hash,
            "selection_artifact_hash": selection_artifact_hash,
            "selected_candidate_id": selected_candidate_id,
            "selection_attempt_index": selection_attempt_index,
            "selection_holdout_reuse_count": selection_holdout_reuse_count,
            "pre_holdout_gate_hash": pre_holdout_gate_hash,
            "pre_holdout_gate_artifact_path": str(
                pre_holdout_gate_artifact_path(
                    manager=manager,
                    experiment_id=str(authoritative.get("experiment_id") or ""),
                    gate_hash=pre_holdout_gate_hash,
                )
            ),
            "holdout_access_status": "AUTHORIZED_FOR_SINGLE_EXPOSURE",
            "result_status": "ACTIVE",
            "prior_registry_hash": prior_hash,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        activation["row_hash"] = compute_row_hash(activation)
        append_authority_jsonl(path, activation, require_kernel_append_only=True)
    return {
        "path": str(path.resolve()),
        "prior_hash": prior_hash,
        "row_hash": str(activation["row_hash"]),
        "row": dict(activation),
        "reservation": dict(reservation),
        "reservation_row": dict(authoritative),
        "holdout_read_capability": _holdout_read_capability(
            path=path,
            reservation=authoritative,
            activation=activation,
        ),
    }


def _holdout_read_capability(
    *,
    path: Path,
    reservation: dict[str, Any],
    activation: dict[str, Any],
) -> dict[str, Any]:
    material = {
        "schema_version": HOLDOUT_READ_CAPABILITY_SCHEMA_VERSION,
        "registry_path": str(path.resolve()),
        "registry_path_hash": sha256_prefixed(
            {"registry_path": str(path.resolve())}
        ),
        "reservation_row_hash": reservation.get("row_hash"),
        "activation_row_hash": activation.get("row_hash"),
        "authority_scope_hash": activation.get(
            "final_holdout_authority_scope_hash"
        ),
        "manifest_hash": activation.get("manifest_hash"),
        "selection_artifact_hash": activation.get("selection_artifact_hash"),
        "selected_candidate_id": activation.get("selected_candidate_id"),
        "pre_holdout_gate_hash": activation.get("pre_holdout_gate_hash"),
        "fence_generation": activation.get("fence_generation"),
    }
    return {
        **material,
        "content_hash": sha256_prefixed(material, label="holdout_read_capability"),
    }


def consume_final_holdout_read_capability(
    *,
    manager: ResearchPathManager,
    capability: object,
    manifest_hash: str,
    authority_scope_hash: str,
    split_name: str,
    requested_range: dict[str, Any],
    created_at: str | None = None,
) -> dict[str, Any]:
    """Atomically consume one activated capability before any holdout bytes."""

    required_fields = {
        "schema_version",
        "registry_path",
        "registry_path_hash",
        "reservation_row_hash",
        "activation_row_hash",
        "authority_scope_hash",
        "manifest_hash",
        "selection_artifact_hash",
        "selected_candidate_id",
        "pre_holdout_gate_hash",
        "fence_generation",
        "content_hash",
    }
    if not isinstance(capability, dict) or set(capability) != required_fields:
        raise ValueError("holdout_read_capability_fields_invalid")
    material = {key: value for key, value in capability.items() if key != "content_hash"}
    if (
        capability.get("schema_version") != HOLDOUT_READ_CAPABILITY_SCHEMA_VERSION
        or capability.get("content_hash")
        != sha256_prefixed(material, label="holdout_read_capability")
    ):
        raise ValueError("holdout_read_capability_hash_invalid")
    for field in (
        "registry_path_hash",
        "reservation_row_hash",
        "activation_row_hash",
        "authority_scope_hash",
        "manifest_hash",
        "selection_artifact_hash",
        "pre_holdout_gate_hash",
    ):
        _require_sha256(capability.get(field), "holdout_read_capability_" + field)
    path = experiment_registry_path(manager=manager).resolve()
    if (
        capability.get("registry_path") != str(path)
        or capability.get("registry_path_hash")
        != sha256_prefixed({"registry_path": str(path)})
        or capability.get("manifest_hash") != manifest_hash
        or capability.get("authority_scope_hash") != authority_scope_hash
    ):
        raise ValueError("holdout_read_capability_binding_mismatch")
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        _require_valid_registry_chain(rows)
        reservation_hash = str(capability["reservation_row_hash"])
        activation_hash = str(capability["activation_row_hash"])
        reservation = next(
            (
                row
                for row in rows
                if row.get("event_type") == "research_attempt_reserved"
                and row.get("row_hash") == reservation_hash
            ),
            None,
        )
        activation = next(
            (
                row
                for row in rows
                if row.get("event_type") == "research_attempt_activated"
                and row.get("row_hash") == activation_hash
                and row.get("reservation_row_hash") == reservation_hash
            ),
            None,
        )
        if not isinstance(reservation, dict) or not isinstance(activation, dict):
            raise ValueError("holdout_read_capability_authority_missing")
        if _holdout_read_capability(
            path=path,
            reservation=reservation,
            activation=activation,
        ) != capability:
            raise ValueError("holdout_read_capability_stale_or_tampered")
        if _terminal_rows_for_reservation(rows, reservation_hash):
            raise ValueError("holdout_read_capability_reservation_terminal")
        if _read_started_rows_for_activation(rows, activation_hash):
            raise ValueError("holdout_read_capability_already_consumed")
        latest_generation = max(
            (
                _as_int(row.get("fence_generation")) or 0
                for row in rows
                if row.get("event_type") == "research_attempt_reserved"
                and row.get("final_holdout_authority_scope_hash")
                == authority_scope_hash
            ),
            default=0,
        )
        if latest_generation != capability.get("fence_generation"):
            raise ValueError("holdout_read_capability_stale_fence")
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        read_started = {
            "schema_version": EXPERIMENT_REGISTRY_SCHEMA_VERSION,
            "event_type": "research_attempt_holdout_read_started",
            "reservation_row_hash": reservation_hash,
            "activation_row_hash": activation_hash,
            "final_holdout_authority_scope_hash": authority_scope_hash,
            "fence_generation": capability.get("fence_generation"),
            "manifest_hash": manifest_hash,
            "selection_artifact_hash": capability.get("selection_artifact_hash"),
            "selected_candidate_id": capability.get("selected_candidate_id"),
            "pre_holdout_gate_hash": capability.get("pre_holdout_gate_hash"),
            "holdout_read_capability_hash": capability.get("content_hash"),
            "split_name": split_name,
            "requested_range": dict(requested_range),
            "requested_range_hash": sha256_prefixed(
                requested_range,
                label="final_holdout_requested_range",
            ),
            "holdout_access_status": "READ_STARTED",
            "result_status": "ACTIVE",
            "prior_registry_hash": prior_hash,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        read_started["row_hash"] = compute_row_hash(read_started)
        append_authority_jsonl(path, read_started, require_kernel_append_only=True)
    return dict(read_started)


def abort_final_holdout_reservation(
    *,
    manager: ResearchPathManager,
    reservation: dict[str, Any],
    reason: str,
    created_at: str | None = None,
) -> dict[str, Any] | None:
    validate_final_holdout_reservation_transport(
        manager=manager, reservation=reservation
    )
    return append_attempt_aborted(
        manager=manager,
        reservation_row_hash=str(reservation["reservation_row_hash"]),
        reason=reason,
        created_at=created_at,
    )


def append_attempt_aborted(
    *,
    manager: ResearchPathManager,
    reservation_row_hash: str,
    reason: str,
    created_at: str | None = None,
) -> dict[str, Any] | None:
    path = experiment_registry_path(manager=manager)
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        _require_valid_registry_chain(rows)
        reservation = next(
            (
                row
                for row in rows
                if row.get("row_hash") == reservation_row_hash
                and row.get("event_type") == "research_attempt_reserved"
                and row.get("result_status") == "IN_PROGRESS"
            ),
            None,
        )
        if not isinstance(reservation, dict) or _terminal_rows_for_reservation(
            rows, reservation_row_hash
        ):
            return None
        activation_rows = _activation_rows_for_reservation(
            rows, reservation_row_hash
        )
        if len(activation_rows) > 1:
            raise ValueError("experiment_registry_multiple_activation_events")
        activation = activation_rows[0] if activation_rows else None
        read_rows = (
            _read_started_rows_for_activation(
                rows, str(activation.get("row_hash") or "")
            )
            if isinstance(activation, dict)
            else []
        )
        if len(read_rows) > 1:
            raise ValueError("experiment_registry_multiple_holdout_read_events")
        holdout_accessed = len(read_rows) == 1
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        row = {
            "schema_version": EXPERIMENT_REGISTRY_SCHEMA_VERSION,
            "budget_policy": EXPERIMENT_REGISTRY_BUDGET_POLICY,
            "event_type": "research_attempt_aborted",
            **{
                key: value
                for key, value in reservation.items()
                if key
                not in {
                    "event_type",
                    "result_status",
                    "prior_registry_hash",
                    "row_hash",
                    "created_at",
                }
            },
            "reservation_row_hash": reservation_row_hash,
            "activation_row_hash": (
                activation.get("row_hash") if isinstance(activation, dict) else None
            ),
            "holdout_read_started_row_hash": (
                read_rows[0].get("row_hash") if holdout_accessed else None
            ),
            "fence_generation": reservation.get("fence_generation"),
            "holdout_accessed": holdout_accessed,
            "holdout_access_status": (
                "EXPOSURE_ABORTED"
                if holdout_accessed
                else "PRE_EXPOSURE_ABORTED"
            ),
            "result_status": "ABORTED",
            "abort_reason": reason,
            "prior_registry_hash": prior_hash,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        row["row_hash"] = compute_row_hash(row)
        append_authority_jsonl(path, row, require_kernel_append_only=True)
    return {
        "path": str(path.resolve()),
        "prior_hash": prior_hash,
        "row_hash": str(row["row_hash"]),
        "row": dict(row),
    }


def append_attempt_completion(
    *,
    manager: ResearchPathManager,
    reservation: dict[str, Any],
    updates: dict[str, Any],
    result_status: str = "COMPLETED",
    created_at: str | None = None,
) -> dict[str, Any]:
    path = experiment_registry_path(manager=manager)
    _require_completed_holdout_evidence(updates)
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        _require_valid_registry_chain(rows)
        reservation_row_hash = str(
            reservation.get("row_hash")
            or (
                reservation.get("row", {}).get("row_hash")
                if isinstance(reservation.get("row"), dict)
                else ""
            )
            or ""
        )
        reservation_row = next(
            (
                row
                for row in rows
                if row.get("row_hash") == reservation_row_hash
                and row.get("event_type") == "research_attempt_reserved"
                and row.get("result_status") == "IN_PROGRESS"
            ),
            None,
        )
        if not isinstance(reservation_row, dict):
            raise ValueError("experiment_registry_reservation_missing")
        reservation_path = str(reservation.get("path") or "").strip()
        if reservation_path and Path(reservation_path).resolve() != path.resolve():
            raise ValueError("experiment_registry_path_mismatch")
        if _terminal_rows_for_reservation(rows, reservation_row_hash):
            raise ValueError("experiment_registry_attempt_already_terminal")
        authority_contract = (
            reservation_row.get("final_holdout_authority_contract_schema_version")
            == FINAL_HOLDOUT_AUTHORITY_CONTRACT_SCHEMA_VERSION
        )
        activation: dict[str, Any] | None = None
        if authority_contract:
            activation_rows = _activation_rows_for_reservation(
                rows, reservation_row_hash
            )
            if len(activation_rows) != 1:
                raise ValueError(
                    "experiment_registry_final_holdout_activation_missing_or_ambiguous"
                )
            activation = activation_rows[0]
            read_rows = _read_started_rows_for_activation(
                rows, str(activation.get("row_hash") or "")
            )
            if len(read_rows) != 1:
                raise ValueError(
                    "experiment_registry_final_holdout_read_missing_or_ambiguous"
                )
            if (
                activation.get("fence_generation")
                != reservation_row.get("fence_generation")
                or activation.get("final_holdout_authority_scope_hash")
                != reservation_row.get("final_holdout_authority_scope_hash")
            ):
                raise ValueError("experiment_registry_final_holdout_activation_stale")
            for field in (
                "selection_artifact_hash",
                "selected_candidate_id",
                "selection_attempt_index",
                "selection_holdout_reuse_count",
            ):
                if updates.get(field) != activation.get(field):
                    raise ValueError(
                        "experiment_registry_final_holdout_activation_binding_mismatch"
                    )
            immutable_fields = (
                "final_holdout_authority_contract_schema_version",
                "final_holdout_authority_scope_hash",
                "final_holdout_access_purpose",
                "final_holdout_purpose_binding_hash",
                "reservation_request_hash",
                "reservation_request_id",
                "manifest_hash",
                "fence_generation",
                "pre_exposure_reservation_key_hash",
                "pre_exposure_dataset_identity_hash",
                "primary_completion_row_hash",
                "primary_final_holdout_result_hash",
                "primary_final_holdout_confirmation_hash",
                "primary_reproduction_receipt_hash",
                "independent_principal_assertion_hash",
                "independent_principal_assertion_scope_hash",
                "independent_principal_assertion_issuer",
                "independent_principal_assertion_key_id",
                "independent_principal_assertion_subject",
                "independent_principal_assertion_nonce",
            )
            if any(
                field in updates
                and updates[field] != reservation_row.get(field)
                for field in immutable_fields
            ):
                raise ValueError(
                    "experiment_registry_final_holdout_reservation_immutable_field_mismatch"
                )
            if (
                reservation_row.get("final_holdout_access_purpose")
                == FinalHoldoutAccessPurpose.INDEPENDENT_REPRODUCTION.value
            ):
                primary = _required_primary_completion(
                    rows=rows,
                    completion_row_hash=str(
                        reservation_row.get("primary_completion_row_hash") or ""
                    ),
                    authority_scope_hash=str(
                        reservation_row.get("final_holdout_authority_scope_hash") or ""
                    ),
                    manifest_hash=str(reservation_row.get("manifest_hash") or ""),
                    final_holdout_result_hash=str(
                        reservation_row.get("primary_final_holdout_result_hash") or ""
                    ),
                )
                _require_independent_reproduction_matches_primary(
                    primary=primary,
                    reproduced=updates,
                    error_code=(
                        "experiment_registry_independent_completion_primary_mismatch"
                    ),
                )
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        completed_reuse_key = str(updates["final_holdout_reuse_key_hash"])
        computed_holdout_reuse_count = sum(
            1
            for existing in rows
            if existing.get("event_type") == "research_attempt_completed"
            and _reuse_key_schema_version(existing)
            == FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
            and str(existing.get("final_holdout_reuse_key_hash") or "")
            == completed_reuse_key
        )
        row = {
            "schema_version": EXPERIMENT_REGISTRY_SCHEMA_VERSION,
            "budget_policy": EXPERIMENT_REGISTRY_BUDGET_POLICY,
            "event_type": "research_attempt_completed",
            **{
                key: value
                for key, value in reservation_row.items()
                if key
                not in {
                    "event_type",
                    "result_status",
                    "prior_registry_hash",
                    "row_hash",
                    "created_at",
                }
            },
            **updates,
            "computed_holdout_reuse_count": computed_holdout_reuse_count,
            "reservation_row_hash": reservation_row_hash,
            "activation_row_hash": (
                activation.get("row_hash") if isinstance(activation, dict) else None
            ),
            "holdout_read_started_row_hash": (
                read_rows[0].get("row_hash") if authority_contract else None
            ),
            "fence_generation": reservation_row.get("fence_generation"),
            "final_holdout_authority_scope_hash": reservation_row.get(
                "final_holdout_authority_scope_hash"
            ),
            "holdout_accessed": True if authority_contract else None,
            "holdout_access_status": (
                "EXPOSURE_COMPLETED" if authority_contract else None
            ),
            "result_status": result_status,
            "prior_registry_hash": prior_hash,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        row["row_hash"] = compute_row_hash(row)
        append_authority_jsonl(path, row, require_kernel_append_only=True)
    return {
        "path": str(path.resolve()),
        "prior_hash": prior_hash,
        "row_hash": str(row["row_hash"]),
        "row": dict(row),
    }


def _require_completed_holdout_evidence(updates: dict[str, Any]) -> None:
    required = [
        "dataset_artifact_evidence_hash",
        "final_holdout_query_hash",
        "final_holdout_data_hash",
        "final_holdout_fingerprint_hash",
        "final_holdout_quality_hash",
        "final_holdout_reuse_key_hash",
    ]
    if updates.get("selection_artifact_hash") is not None:
        required.append("final_holdout_result_hash")
    missing = [
        field
        for field in required
        if not isinstance(updates.get(field), str)
        or not str(updates[field]).startswith("sha256:")
    ]
    if (
        missing
        or updates.get("final_holdout_reuse_key_schema_version")
        != FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
    ):
        raise ValueError(
            "experiment_registry_completed_holdout_evidence_missing:"
            + ",".join(missing)
        )
    if (
        updates.get("selection_artifact_hash") is not None
        and updates.get("final_holdout_result_hash_schema_version") != 1
    ):
        raise ValueError(
            "experiment_registry_completed_holdout_evidence_missing:"
            "final_holdout_result_hash_schema_version"
        )


def validate_experiment_registry_binding(
    *,
    report: dict[str, Any],
    evidence: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    require_complete: bool = False,
    expected_registry_path: Path | None = None,
) -> list[str]:
    source = evidence if isinstance(evidence, dict) else report
    validation = validation if isinstance(validation, dict) else {}
    reasons: list[str] = []
    path_value = str(
        source.get("experiment_registry_path")
        or report.get("experiment_registry_path")
        or validation.get("experiment_registry_path")
        or ""
    ).strip()
    row_hash = str(
        source.get("experiment_registry_row_hash")
        or report.get("experiment_registry_row_hash")
        or validation.get("experiment_registry_row_hash")
        or ""
    ).strip()
    prior_hash = str(
        source.get("experiment_registry_prior_hash")
        or report.get("experiment_registry_prior_hash")
        or validation.get("experiment_registry_prior_hash")
        or ""
    ).strip()
    if not path_value:
        return ["experiment_registry_path_missing"]
    if not row_hash.startswith("sha256:"):
        return ["experiment_registry_row_hash_missing"]
    path = Path(path_value).expanduser()
    if (
        expected_registry_path is not None
        and path.resolve() != expected_registry_path.resolve()
    ):
        reasons.append("experiment_registry_path_mismatch")
    if not path.exists():
        return ["experiment_registry_missing"]
    try:
        rows = load_experiment_registry_rows(path)
    except (OSError, json.JSONDecodeError):
        return ["experiment_registry_missing"]
    reasons.extend(experiment_registry_chain_reasons(rows))
    row_index = next(
        (index for index, row in enumerate(rows) if row.get("row_hash") == row_hash),
        None,
    )
    if row_index is None:
        return ["experiment_registry_row_hash_mismatch"]
    row = rows[row_index]
    if compute_row_hash(row) != row_hash:
        reasons.append("experiment_registry_row_hash_mismatch")
    expected_prior = (
        sha256_prefixed(rows[:row_index])
        if row_index
        else EMPTY_EXPERIMENT_REGISTRY_HASH
    )
    if str(row.get("prior_registry_hash") or "") != expected_prior or (
        prior_hash and prior_hash != expected_prior
    ):
        reasons.append("experiment_registry_prior_hash_mismatch")
    completion_hash = str(
        source.get("experiment_registry_completion_row_hash")
        or report.get("experiment_registry_completion_row_hash")
        or validation.get("experiment_registry_completion_row_hash")
        or ""
    ).strip()
    terminal_rows = _terminal_rows_for_reservation(rows, row_hash)
    if len(terminal_rows) > 1:
        reasons.append("experiment_registry_multiple_terminal_events")
    activation_rows = _activation_rows_for_reservation(rows, row_hash)
    authority_contract = (
        row.get("final_holdout_authority_contract_schema_version")
        == FINAL_HOLDOUT_AUTHORITY_CONTRACT_SCHEMA_VERSION
    )
    if authority_contract:
        if len(activation_rows) != 1:
            reasons.append("experiment_registry_activation_missing_or_ambiguous")
        else:
            activation = activation_rows[0]
            declared_activation_hash = str(
                source.get("final_holdout_activation_row_hash")
                or source.get("activation_row_hash")
                or report.get("final_holdout_activation_row_hash")
                or report.get("activation_row_hash")
                or ""
            )
            if declared_activation_hash != activation.get("row_hash"):
                reasons.append("experiment_registry_activation_hash_mismatch")
            if (
                activation.get("fence_generation") != row.get("fence_generation")
                or activation.get("final_holdout_authority_scope_hash")
                != row.get("final_holdout_authority_scope_hash")
            ):
                reasons.append("experiment_registry_activation_stale_fence")
            expected_gate_hash = source.get("pre_holdout_gate_hash") or report.get(
                "pre_holdout_gate_hash"
            )
            if expected_gate_hash != activation.get("pre_holdout_gate_hash"):
                reasons.append("experiment_registry_pre_holdout_gate_hash_mismatch")
    completion = _completion_for_reservation(rows, row_hash, completion_hash)
    _extend_registry_field_mismatch_reasons(
        reasons,
        row=row,
        completion=completion,
        report=report,
        evidence=evidence,
        validation=validation,
    )
    if require_complete:
        if not isinstance(completion, dict):
            reasons.append("experiment_registry_incomplete_attempt")
        elif compute_row_hash(completion) != completion.get("row_hash"):
            reasons.append("experiment_registry_row_hash_mismatch")
        elif (
            str(completion.get("result_status") or "")
            not in VALIDATION_PERMITTED_STATUSES
        ):
            reasons.append("experiment_registry_incomplete_attempt")
        elif str(completion.get("reservation_row_hash") or "") != row_hash:
            reasons.append("experiment_registry_stale")
    if completion_hash and not isinstance(completion, dict):
        reasons.append("experiment_registry_row_hash_mismatch")
    if isinstance(completion, dict):
        if authority_contract and activation_rows:
            if completion.get("activation_row_hash") != activation_rows[0].get(
                "row_hash"
            ):
                reasons.append("experiment_registry_completion_activation_mismatch")
        _extend_completion_mismatch_reasons(
            reasons,
            completion=completion,
            report=report,
            evidence=evidence,
            validation=validation,
        )
    _extend_declared_counter_reasons(reasons, report=report, evidence=evidence)
    _extend_budget_reasons(reasons, report=report, evidence=evidence)
    return sorted(set(reasons))


def _extend_registry_field_mismatch_reasons(
    reasons: list[str],
    *,
    row: dict[str, Any],
    completion: dict[str, Any] | None,
    report: dict[str, Any],
    evidence: dict[str, Any] | None,
    validation: dict[str, Any],
) -> None:
    evidence = evidence if isinstance(evidence, dict) else {}
    completion = completion if isinstance(completion, dict) else {}
    content_pending = bool(row.get("final_holdout_content_pending_until_completion"))
    registry_fields: tuple[str, ...] = (
        "experiment_id",
        "experiment_family_id",
        "hypothesis_id",
        "hypothesis_version",
        "hypothesis_contract_hash",
        "hypothesis_semantic_fingerprint",
        "hypothesis_lineage_hash",
        "research_question_id",
        "research_question_version",
        "research_question_hash",
        "observation_hashes",
        "hypothesis_status",
        "pre_registered_at",
        "registration_evidence_hash",
        "hypothesis_identity_source",
        "experiment_family_identity_source",
        "manifest_hash",
        "dataset_snapshot_id",
        "dataset_content_hash",
        "dataset_quality_hash",
        "train_split_hash",
        "validation_split_hash",
        "final_holdout_split_hash",
        "final_holdout_identity_hash",
        "final_holdout_content_hash",
        "final_holdout_reuse_key_hash_v1",
        "final_holdout_reuse_key_hash",
        "final_holdout_reuse_key_schema_version",
        "final_holdout_reuse_key_hash_v2",
        "objective_metric",
        "parameter_space_hash",
        "dataset_artifact",
        "dataset_split_evidence",
        "dataset_artifact_evidence_hash",
        "final_holdout_query_hash",
        "final_holdout_data_hash",
        "final_holdout_fingerprint_hash",
        "final_holdout_quality_hash",
        "final_holdout_result_hash",
    )
    if report.get("artifact_type") == "final_holdout_confirmation":
        registry_fields += (
            "selection_artifact_hash",
            "selected_candidate_id",
            "selection_attempt_index",
            "selection_holdout_reuse_count",
        )
    for field in registry_fields:
        expected = evidence.get(field)
        if expected is None:
            expected = report.get(field)
        if expected is None:
            expected = validation.get(field)
        actual = row.get(field)
        if (
            content_pending
            and field in PRE_CONTENT_COMPLETION_BOUND_FIELDS
            and actual is None
        ):
            actual = completion.get(field)
        if expected is not None and str(row.get(field) or "") != str(expected or ""):
            if not (
                content_pending
                and field in PRE_CONTENT_COMPLETION_BOUND_FIELDS
                and str(actual or "") == str(expected or "")
            ):
                reasons.append(
                    "experiment_registry_artifact_evidence_mismatch"
                    if field == "dataset_artifact_evidence_hash"
                    else "experiment_registry_split_evidence_mismatch"
                    if field.startswith("final_holdout_") and field.endswith("_hash")
                    else "experiment_registry_stale"
                )
                break
        if (
            expected is None
            and row.get(field) is not None
            and field.endswith("_identity_source")
        ):
            reasons.append("experiment_registry_identity_source_missing")
            break
    fingerprint = (
        evidence.get("final_holdout_fingerprint")
        or report.get("final_holdout_fingerprint")
        or validation.get("final_holdout_fingerprint")
    )
    actual_fingerprint = row.get("final_holdout_fingerprint")
    if content_pending and actual_fingerprint is None:
        actual_fingerprint = completion.get("final_holdout_fingerprint")
    if fingerprint is not None and str(actual_fingerprint or "") != str(
        fingerprint or ""
    ):
        reasons.append("experiment_registry_final_holdout_fingerprint_mismatch")
    identity = (
        evidence.get("final_holdout_identity_hash")
        or report.get("final_holdout_identity_hash")
        or validation.get("final_holdout_identity_hash")
    )
    if identity is not None and str(
        row.get("final_holdout_identity_hash") or ""
    ) != str(identity or ""):
        reasons.append("experiment_registry_final_holdout_identity_mismatch")
    content = (
        evidence.get("final_holdout_content_hash")
        or report.get("final_holdout_content_hash")
        or validation.get("final_holdout_content_hash")
    )
    actual_content = row.get("final_holdout_content_hash")
    if content_pending and actual_content is None:
        actual_content = completion.get("final_holdout_content_hash")
    if content is not None and str(actual_content or "") != str(content or ""):
        reasons.append("experiment_registry_final_holdout_content_mismatch")
    reuse_key = (
        evidence.get("final_holdout_reuse_key_hash")
        or report.get("final_holdout_reuse_key_hash")
        or validation.get("final_holdout_reuse_key_hash")
    )
    actual_reuse_key = row.get("final_holdout_reuse_key_hash")
    if content_pending and actual_reuse_key is None:
        actual_reuse_key = completion.get("final_holdout_reuse_key_hash")
    if reuse_key is not None and str(actual_reuse_key or "") != str(reuse_key or ""):
        reasons.append("experiment_registry_final_holdout_reuse_key_mismatch")
    _extend_validation_reuse_identity_reasons(
        reasons,
        row=row,
        report=report,
        evidence=evidence,
        validation=validation,
    )
    for field, code in (
        ("computed_attempt_index", "experiment_registry_attempt_index_mismatch"),
        (
            "computed_holdout_reuse_count",
            "experiment_registry_holdout_reuse_count_mismatch",
        ),
    ):
        expected = evidence.get(field)
        if expected is None:
            expected = report.get(field)
        if expected is None:
            expected = validation.get(field)
        if expected is not None and str(row.get(field) or "") != str(expected or ""):
            reasons.append(code)
    if validation:
        for field in (
            "return_panel_hash",
            "statistical_evidence_hash",
            "candidate_count",
        ):
            expected = validation.get(field)
            if (
                expected is not None
                and row.get(field) is not None
                and str(row.get(field) or "") != str(expected or "")
            ):
                reasons.append("experiment_registry_stale")


def _extend_declared_counter_reasons(
    reasons: list[str],
    *,
    report: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> None:
    evidence = evidence if isinstance(evidence, dict) else {}
    for declared_field, computed_field, code in (
        (
            "declared_attempt_index",
            "computed_attempt_index",
            "declared_attempt_index_mismatch",
        ),
        (
            "declared_holdout_reuse_count",
            "computed_holdout_reuse_count",
            "declared_holdout_reuse_count_mismatch",
        ),
        (
            "selection_attempt_index",
            "computed_attempt_index",
            "selection_attempt_index_mismatch",
        ),
        (
            "selection_holdout_reuse_count",
            "computed_holdout_reuse_count",
            "selection_holdout_reuse_count_mismatch",
        ),
    ):
        declared = evidence.get(declared_field)
        if declared is None:
            declared = report.get(declared_field)
        computed = evidence.get(computed_field)
        if computed is None:
            computed = report.get(computed_field)
        if (
            declared is not None
            and computed is not None
            and str(declared) != str(computed)
        ):
            reasons.append(code)


def _extend_completion_mismatch_reasons(
    reasons: list[str],
    *,
    completion: dict[str, Any],
    report: dict[str, Any],
    evidence: dict[str, Any] | None,
    validation: dict[str, Any],
) -> None:
    evidence = evidence if isinstance(evidence, dict) else {}
    completion_fields = ["return_panel_hash", "candidate_count"]
    confirmation_fields = {
        "selection_artifact_hash",
        "selected_candidate_id",
        "confirmation_gate_result",
        "final_holdout_result_hash",
        "final_holdout_result_hash_schema_version",
    }
    if report.get("artifact_type") == "final_holdout_confirmation":
        completion_fields.extend(sorted(confirmation_fields))
    for field in completion_fields:
        expected = evidence.get(field)
        if expected is None:
            expected = report.get(field)
        if expected is None:
            expected = validation.get(field)
        actual = completion.get(field)
        if expected is not None and (
            (field in confirmation_fields and actual is None)
            or (actual is not None and str(actual or "") != str(expected or ""))
        ):
            reasons.append("experiment_registry_stale")
    statistical_binding_declared = (
        bool(evidence)
        or bool(completion.get("statistical_evidence_hash"))
        or bool(validation.get("experiment_registry_bound_evidence_hash"))
    )
    if statistical_binding_declared:
        phase = str(completion.get("statistical_evidence_hash_phase") or "").strip()
        if phase != EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE:
            reasons.append("experiment_registry_evidence_hash_phase_mismatch")
    if evidence:
        bound = str(
            evidence.get("experiment_registry_bound_evidence_hash") or ""
        ).strip()
        if not bound.startswith("sha256:"):
            reasons.append("experiment_registry_bound_evidence_hash_missing")
        elif str(completion.get("statistical_evidence_hash") or "") != bound:
            reasons.append("experiment_registry_statistical_evidence_hash_mismatch")
        evidence_phase = str(
            evidence.get("experiment_registry_evidence_hash_phase") or ""
        ).strip()
        if evidence_phase != EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE:
            reasons.append("experiment_registry_evidence_hash_phase_mismatch")
    validation_bound = str(
        validation.get("experiment_registry_bound_evidence_hash") or ""
    ).strip()
    if (
        validation_bound
        and str(completion.get("statistical_evidence_hash") or "") != validation_bound
    ):
        reasons.append("experiment_registry_statistical_evidence_hash_mismatch")


def _extend_budget_reasons(
    reasons: list[str],
    *,
    report: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> None:
    contract = (
        (evidence or {}).get("statistical_validation_contract")
        if isinstance(evidence, dict)
        else None
    )
    if not isinstance(contract, dict):
        contract = report.get("statistical_validation_contract")
    gates = contract.get("gates") if isinstance(contract, dict) else None
    if not isinstance(gates, dict):
        return
    attempt = _as_int(
        (evidence or {}).get("computed_attempt_index")
        if isinstance(evidence, dict)
        else None
    )
    if attempt is None:
        attempt = _as_int(report.get("computed_attempt_index"))
    reuse = _as_int(
        (evidence or {}).get("computed_holdout_reuse_count")
        if isinstance(evidence, dict)
        else None
    )
    if reuse is None:
        reuse = _as_int(report.get("computed_holdout_reuse_count"))
    max_attempt = _as_int(gates.get("max_attempt_index_without_new_hypothesis"))
    max_reuse = _as_int(gates.get("max_holdout_reuse_count"))
    if attempt is not None and max_attempt is not None and attempt > max_attempt:
        reasons.append("experiment_registry_budget_exceeded")
        reasons.append("attempt_budget_exceeded")
    if reuse is not None and max_reuse is not None and reuse > max_reuse:
        reasons.append("experiment_registry_budget_exceeded")
        reasons.append("holdout_reuse_budget_exceeded")


def _completion_for_reservation(
    rows: list[dict[str, Any]],
    reservation_row_hash: str,
    completion_hash: str,
) -> dict[str, Any] | None:
    for row in reversed(rows):
        if row.get("event_type") != "research_attempt_completed":
            continue
        if str(row.get("reservation_row_hash") or "") != reservation_row_hash:
            continue
        if completion_hash and str(row.get("row_hash") or "") != completion_hash:
            continue
        return row
    return None


def _compute_research_attempt_counters_from_rows(
    *,
    rows: list[dict[str, Any]],
    base_payload: dict[str, Any],
) -> dict[str, int]:
    family_id = str(base_payload.get("experiment_family_id") or "")
    hypothesis_id = str(base_payload.get("hypothesis_id") or "")
    pre_exposure_key = str(base_payload.get("pre_exposure_reservation_key_hash") or "")
    duplicate_count = sum(
        1
        for row in rows
        if row.get("event_type") == "research_attempt_reserved"
        and pre_exposure_key
        and str(row.get("pre_exposure_reservation_key_hash") or "") == pre_exposure_key
        and _reservation_counts_as_exposure_or_lock(rows, row)
    )
    return {
        "computed_attempt_index": 1
        + sum(
            1
            for row in rows
            if row.get("event_type") == "research_attempt_reserved"
            and str(row.get("experiment_family_id") or "") == family_id
            and str(row.get("hypothesis_id") or "") == hypothesis_id
        ),
        "computed_pre_exposure_duplicate_count": duplicate_count,
        "computed_holdout_reuse_count": duplicate_count,
    }


def _reuse_key_schema_version(payload: dict[str, Any]) -> int | None:
    try:
        return int(payload.get("final_holdout_reuse_key_schema_version"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _extend_validation_reuse_identity_reasons(
    reasons: list[str],
    *,
    row: dict[str, Any],
    report: dict[str, Any],
    evidence: dict[str, Any] | None,
    validation: dict[str, Any],
) -> None:
    source = {}
    source.update(row)
    source.update(report)
    if isinstance(evidence, dict):
        source.update(evidence)
    source.update(validation)
    if not requires_candidate_validation(source.get("research_classification")):
        return
    schema_version = _reuse_key_schema_version(source)
    if schema_version != FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION:
        reasons.append("final_holdout_reuse_key_schema_version_missing")
    reuse_key = str(source.get("final_holdout_reuse_key_hash") or "").strip()
    if not reuse_key.startswith("sha256:"):
        reasons.append("final_holdout_reuse_key_hash_v2_missing")
    objective_metric = str(
        source.get("objective_metric") or source.get("primary_metric") or ""
    ).strip()
    if not objective_metric or objective_metric.lower() in {"unknown", "none", "null"}:
        reasons.append("objective_metric_missing")


def _checked_reservation_reasons(
    *,
    base_payload: dict[str, Any],
    computed_attempt_index: int,
    computed_holdout_reuse_count: int,
    statistical_validation_contract: dict[str, Any] | None,
) -> list[str]:
    reasons: list[str] = []
    declared_attempt = _as_int(base_payload.get("declared_attempt_index"))
    declared_reuse = _as_int(base_payload.get("declared_holdout_reuse_count"))
    selection_attempt = _as_int(base_payload.get("selection_attempt_index"))
    selection_reuse = _as_int(base_payload.get("selection_holdout_reuse_count"))
    if declared_attempt is not None and declared_attempt != computed_attempt_index:
        reasons.append("declared_attempt_index_mismatch")
    if declared_reuse is not None and declared_reuse != computed_holdout_reuse_count:
        reasons.append("declared_holdout_reuse_count_mismatch")
    if selection_attempt is not None and selection_attempt != computed_attempt_index:
        reasons.append("selection_attempt_index_mismatch")
    if selection_reuse is not None and selection_reuse != computed_holdout_reuse_count:
        reasons.append("selection_holdout_reuse_count_mismatch")
    gates = (
        statistical_validation_contract.get("gates")
        if isinstance(statistical_validation_contract, dict)
        else None
    )
    if isinstance(gates, dict):
        max_attempt = _as_int(gates.get("max_attempt_index_without_new_hypothesis"))
        max_reuse = _as_int(gates.get("max_holdout_reuse_count"))
        if max_attempt is not None and computed_attempt_index > max_attempt:
            reasons.extend(
                ["experiment_registry_budget_exceeded", "attempt_budget_exceeded"]
            )
        if max_reuse is not None and computed_holdout_reuse_count > max_reuse:
            reasons.extend(
                ["experiment_registry_budget_exceeded", "holdout_reuse_budget_exceeded"]
            )
    return sorted(set(reasons))


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    try:
        numeric = cast(str | bytes | bytearray | SupportsInt | SupportsIndex, value)
        return int(numeric)
    except (TypeError, ValueError):
        return None


@contextmanager
def _locked_registry(path: Path) -> Iterator[None]:
    ensure_authority_directory(path.parent)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = open_lock_file(lock_path)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except ImportError:
                pass
        finally:
            os.close(fd)
