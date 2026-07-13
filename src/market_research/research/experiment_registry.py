from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from market_research.paths import ResearchPathManager
from market_research.storage_io import append_jsonl

from .research_classification import requires_candidate_validation
from .hashing import content_hash_payload, sha256_prefixed


EXPERIMENT_REGISTRY_SCHEMA_VERSION = 3
FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION = 4
PRE_EXPOSURE_RESERVATION_KEY_SCHEMA_VERSION = 1
EMPTY_EXPERIMENT_REGISTRY_HASH = sha256_prefixed([])
VALIDATION_PERMITTED_STATUSES = {"COMPLETED"}
EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE = "pre_completion_evidence_hash"
EXPERIMENT_REGISTRY_BUDGET_POLICY = "registry_append_only_budget_exempt"
PRE_CONTENT_COMPLETION_BOUND_FIELDS = {
    "dataset_content_hash",
    "dataset_quality_hash",
    "final_holdout_split_hash",
    "final_holdout_content_hash",
}


def experiment_registry_path(*, manager: ResearchPathManager) -> Path:
    """Return the managed append-only experiment registry path.

    The experiment registry is a cross-run final-holdout attempt ledger. It is
    not an experiment-scoped artifact budget target, but it is managed reports
    evidence with append-only rows, prior-registry hashes, row hashes, and
    repo-local artifact checks.
    """
    path = manager.data_dir() / "reports" / "research" / "_registry" / "experiment_registry.jsonl"
    project_root = manager.project_root.resolve()
    if ResearchPathManager.is_within(path.resolve(), project_root):
        raise ValueError(f"experiment registry path must be outside repository: {path.resolve()}")
    return path


def registry_content_hash(path: Path) -> str:
    rows = load_experiment_registry_rows(path)
    return sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH


def row_hash_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "row_hash"}


def compute_row_hash(row: dict[str, Any]) -> str:
    return sha256_prefixed(content_hash_payload(row_hash_payload(row)))


def research_freedom_hash(payload: dict[str, Any]) -> str:
    return sha256_prefixed(
        {
            "experiment_family_id": payload.get("experiment_family_id"),
            "hypothesis_id": payload.get("hypothesis_id"),
            "hypothesis_version": payload.get("hypothesis_version"),
            "hypothesis_contract_hash": payload.get("hypothesis_contract_hash"),
            "hypothesis_semantic_fingerprint": payload.get("hypothesis_semantic_fingerprint"),
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
            "final_holdout_reuse_key_hash_v1": payload.get("final_holdout_reuse_key_hash_v1"),
            "final_holdout_reuse_key_schema_version": payload.get("final_holdout_reuse_key_schema_version"),
            "pre_exposure_reservation_key_hash": payload.get("pre_exposure_reservation_key_hash"),
            "pre_exposure_reservation_key_schema_version": payload.get("pre_exposure_reservation_key_schema_version"),
            "objective_metric": payload.get("objective_metric"),
            "parameter_space_hash": payload.get("parameter_space_hash"),
            "computed_attempt_index": payload.get("computed_attempt_index"),
            "computed_holdout_reuse_count": payload.get("computed_holdout_reuse_count"),
            "experiment_registry_prior_hash": payload.get("experiment_registry_prior_hash")
            or payload.get("prior_registry_hash"),
            "experiment_registry_row_hash": payload.get("experiment_registry_row_hash") or payload.get("row_hash"),
        }
    )


def research_identity_from_manifest(manifest: Any) -> dict[str, Any]:
    raw = getattr(manifest, "raw", {}) if isinstance(getattr(manifest, "raw", {}), dict) else {}
    experiment_id = str(getattr(manifest, "experiment_id", "") or raw.get("experiment_id") or "")
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
        pre_registered_at = spec.pre_registered_at
        registration_evidence_hash = spec.registration_evidence_hash
        pre_registration_verified = bool(spec.pre_registration_verified)
    else:
        family_id = experiment_id
        hypothesis_id = sha256_prefixed({"legacy_hypothesis": manifest_hypothesis or experiment_id})
        status = "unregistered"
        identity_source = "legacy_manifest.hypothesis"
        family_source = "experiment_id"
        version = None
        contract_hash = None
        semantic_fingerprint = None
        pre_registered_at = None
        registration_evidence_hash = None
        pre_registration_verified = False
    return {
        "experiment_family_id": family_id,
        "hypothesis_id": hypothesis_id,
        "hypothesis_version": version,
        "hypothesis_contract_hash": contract_hash,
        "hypothesis_semantic_fingerprint": semantic_fingerprint,
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
    if not metric or not all(isinstance(value, str) and value.startswith("sha256:") for value in required_evidence):
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
    if not metric or not isinstance(dataset_artifact_evidence_hash, str) or not dataset_artifact_evidence_hash.startswith("sha256:"):
        return None
    return sha256_prefixed({
        "schema": "pre_exposure_reservation_key_v1",
        "schema_version": PRE_EXPOSURE_RESERVATION_KEY_SCHEMA_VERSION,
        "strategy_name": strategy_name,
        "market": market,
        "interval": interval,
        "final_holdout_start": (final_holdout or {}).get("start"),
        "final_holdout_end": (final_holdout or {}).get("end"),
        "objective_metric": metric,
        "dataset_artifact_evidence_hash": dataset_artifact_evidence_hash,
    })


def objective_metric_from_manifest(manifest: Any) -> str | None:
    statistical_validation = getattr(manifest, "statistical_validation", None)
    primary_metric = str(getattr(statistical_validation, "primary_metric", "") or "").strip()
    if primary_metric:
        return primary_metric
    raw = getattr(manifest, "raw", {}) if isinstance(getattr(manifest, "raw", {}), dict) else {}
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
        dataset_artifact_evidence_hash=artifact_evidence["dataset_artifact_evidence_hash"],
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
        "final_holdout_query_hash": sha256_prefixed({"requested_range": requested_range, "snapshot_query_hash": split.get("snapshot_query_hash")}),
        "final_holdout_data_hash": split.get("snapshot_data_hash"),
        "final_holdout_fingerprint_hash": split.get("snapshot_fingerprint_hash"),
        "final_holdout_quality_hash": split.get("quality_hash"),
    }


def load_experiment_registry_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
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
        append_jsonl(path, row)
    return {"path": str(path.resolve()), "prior_hash": prior_hash, "row_hash": str(row["row_hash"]), "row": dict(row)}


def reserve_research_attempt(
    *,
    manager: ResearchPathManager,
    base_payload: dict[str, Any],
    created_at: str | None = None,
) -> dict[str, Any]:
    path = experiment_registry_path(manager=manager)
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        counters = _compute_research_attempt_counters_from_rows(rows=rows, base_payload=base_payload)
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
        append_jsonl(path, row)
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
    path = experiment_registry_path(manager=manager)
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        counters = _compute_research_attempt_counters_from_rows(rows=rows, base_payload=base_payload)
        computed_attempt_index = counters["computed_attempt_index"]
        computed_holdout_reuse_count = counters["computed_holdout_reuse_count"]
        reasons = _checked_reservation_reasons(
            base_payload=base_payload,
            computed_attempt_index=computed_attempt_index,
            computed_holdout_reuse_count=computed_holdout_reuse_count,
            statistical_validation_contract=statistical_validation_contract,
        )
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
            append_jsonl(path, row)
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
        append_jsonl(path, row)
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
        reservation = next((row for row in rows if row.get("row_hash") == reservation_row_hash), None)
        if not isinstance(reservation, dict):
            return None
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        row = {
            "schema_version": EXPERIMENT_REGISTRY_SCHEMA_VERSION,
            "budget_policy": EXPERIMENT_REGISTRY_BUDGET_POLICY,
            "event_type": "research_attempt_aborted",
            **{
                key: value
                for key, value in reservation.items()
                if key not in {"event_type", "result_status", "prior_registry_hash", "row_hash", "created_at"}
            },
            "reservation_row_hash": reservation_row_hash,
            "result_status": "ABORTED",
            "abort_reason": reason,
            "prior_registry_hash": prior_hash,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        row["row_hash"] = compute_row_hash(row)
        append_jsonl(path, row)
    return {"path": str(path.resolve()), "prior_hash": prior_hash, "row_hash": str(row["row_hash"]), "row": dict(row)}


def append_attempt_completion(
    *,
    manager: ResearchPathManager,
    reservation: dict[str, Any],
    updates: dict[str, Any],
    result_status: str = "COMPLETED",
    created_at: str | None = None,
) -> dict[str, Any]:
    path = experiment_registry_path(manager=manager)
    reservation_row = reservation.get("row") if isinstance(reservation.get("row"), dict) else {}
    _require_completed_holdout_evidence(updates)
    with _locked_registry(path):
        rows = load_experiment_registry_rows(path)
        prior_hash = sha256_prefixed(rows) if rows else EMPTY_EXPERIMENT_REGISTRY_HASH
        completed_reuse_key = str(updates["final_holdout_reuse_key_hash"])
        computed_holdout_reuse_count = sum(
            1
            for existing in rows
            if existing.get("event_type") == "research_attempt_completed"
            and _reuse_key_schema_version(existing) == FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
            and str(existing.get("final_holdout_reuse_key_hash") or "") == completed_reuse_key
        )
        row = {
            "schema_version": EXPERIMENT_REGISTRY_SCHEMA_VERSION,
            "budget_policy": EXPERIMENT_REGISTRY_BUDGET_POLICY,
            "event_type": "research_attempt_completed",
            **{key: value for key, value in reservation_row.items() if key not in {"event_type", "result_status", "prior_registry_hash", "row_hash", "created_at"}},
            **updates,
            "computed_holdout_reuse_count": computed_holdout_reuse_count,
            "reservation_row_hash": reservation.get("row_hash") or reservation_row.get("row_hash"),
            "result_status": result_status,
            "prior_registry_hash": prior_hash,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        row["row_hash"] = compute_row_hash(row)
        append_jsonl(path, row)
    return {"path": str(path.resolve()), "prior_hash": prior_hash, "row_hash": str(row["row_hash"]), "row": dict(row)}


def _require_completed_holdout_evidence(updates: dict[str, Any]) -> None:
    required = (
        "dataset_artifact_evidence_hash",
        "final_holdout_query_hash",
        "final_holdout_data_hash",
        "final_holdout_fingerprint_hash",
        "final_holdout_quality_hash",
        "final_holdout_reuse_key_hash",
    )
    missing = [field for field in required if not isinstance(updates.get(field), str) or not str(updates[field]).startswith("sha256:")]
    if missing or updates.get("final_holdout_reuse_key_schema_version") != FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION:
        raise ValueError("experiment_registry_completed_holdout_evidence_missing:" + ",".join(missing))


def validate_experiment_registry_binding(
    *,
    report: dict[str, Any],
    evidence: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    require_complete: bool = False,
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
    if not path.exists():
        return ["experiment_registry_missing"]
    try:
        rows = load_experiment_registry_rows(path)
    except (OSError, json.JSONDecodeError):
        return ["experiment_registry_missing"]
    row_index = next((index for index, row in enumerate(rows) if row.get("row_hash") == row_hash), None)
    if row_index is None:
        return ["experiment_registry_row_hash_mismatch"]
    row = rows[row_index]
    if compute_row_hash(row) != row_hash:
        reasons.append("experiment_registry_row_hash_mismatch")
    expected_prior = sha256_prefixed(rows[:row_index]) if row_index else EMPTY_EXPERIMENT_REGISTRY_HASH
    if str(row.get("prior_registry_hash") or "") != expected_prior or (prior_hash and prior_hash != expected_prior):
        reasons.append("experiment_registry_prior_hash_mismatch")
    completion_hash = str(
        source.get("experiment_registry_completion_row_hash")
        or report.get("experiment_registry_completion_row_hash")
        or validation.get("experiment_registry_completion_row_hash")
        or ""
    ).strip()
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
        elif str(completion.get("result_status") or "") not in VALIDATION_PERMITTED_STATUSES:
            reasons.append("experiment_registry_incomplete_attempt")
        elif str(completion.get("reservation_row_hash") or "") != row_hash:
            reasons.append("experiment_registry_stale")
    if completion_hash and not isinstance(completion, dict):
        reasons.append("experiment_registry_row_hash_mismatch")
    if isinstance(completion, dict):
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
    for field in (
        "experiment_id",
        "experiment_family_id",
        "hypothesis_id",
        "hypothesis_version",
        "hypothesis_contract_hash",
        "hypothesis_semantic_fingerprint",
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
    ):
        expected = evidence.get(field)
        if expected is None:
            expected = report.get(field)
        if expected is None:
            expected = validation.get(field)
        actual = row.get(field)
        if content_pending and field in PRE_CONTENT_COMPLETION_BOUND_FIELDS and actual is None:
            actual = completion.get(field)
        if expected is not None and str(row.get(field) or "") != str(expected or ""):
            if not (content_pending and field in PRE_CONTENT_COMPLETION_BOUND_FIELDS and str(actual or "") == str(expected or "")):
                reasons.append(
                    "experiment_registry_artifact_evidence_mismatch"
                    if field == "dataset_artifact_evidence_hash"
                    else "experiment_registry_split_evidence_mismatch"
                    if field.startswith("final_holdout_") and field.endswith("_hash")
                    else "experiment_registry_stale"
                )
                break
        if expected is None and row.get(field) is not None and field.endswith("_identity_source"):
            reasons.append("experiment_registry_identity_source_missing")
            break
    fingerprint = evidence.get("final_holdout_fingerprint") or report.get("final_holdout_fingerprint") or validation.get("final_holdout_fingerprint")
    if fingerprint is not None and str(row.get("final_holdout_fingerprint") or "") != str(fingerprint or ""):
        reasons.append("experiment_registry_final_holdout_fingerprint_mismatch")
    identity = evidence.get("final_holdout_identity_hash") or report.get("final_holdout_identity_hash") or validation.get("final_holdout_identity_hash")
    if identity is not None and str(row.get("final_holdout_identity_hash") or "") != str(identity or ""):
        reasons.append("experiment_registry_final_holdout_identity_mismatch")
    content = evidence.get("final_holdout_content_hash") or report.get("final_holdout_content_hash") or validation.get("final_holdout_content_hash")
    actual_content = row.get("final_holdout_content_hash")
    if content_pending and actual_content is None:
        actual_content = completion.get("final_holdout_content_hash")
    if content is not None and str(actual_content or "") != str(content or ""):
        reasons.append("experiment_registry_final_holdout_content_mismatch")
    reuse_key = evidence.get("final_holdout_reuse_key_hash") or report.get("final_holdout_reuse_key_hash") or validation.get("final_holdout_reuse_key_hash")
    if reuse_key is not None and str(row.get("final_holdout_reuse_key_hash") or "") != str(reuse_key or ""):
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
        ("computed_holdout_reuse_count", "experiment_registry_holdout_reuse_count_mismatch"),
    ):
        expected = evidence.get(field)
        if expected is None:
            expected = report.get(field)
        if expected is None:
            expected = validation.get(field)
        if expected is not None and str(row.get(field) or "") != str(expected or ""):
            reasons.append(code)
    if validation:
        for field in ("return_panel_hash", "statistical_evidence_hash", "candidate_count"):
            expected = validation.get(field)
            if expected is not None and row.get(field) is not None and str(row.get(field) or "") != str(expected or ""):
                reasons.append("experiment_registry_stale")


def _extend_declared_counter_reasons(
    reasons: list[str],
    *,
    report: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> None:
    evidence = evidence if isinstance(evidence, dict) else {}
    for declared_field, computed_field, code in (
        ("declared_attempt_index", "computed_attempt_index", "declared_attempt_index_mismatch"),
        ("declared_holdout_reuse_count", "computed_holdout_reuse_count", "declared_holdout_reuse_count_mismatch"),
    ):
        declared = evidence.get(declared_field)
        if declared is None:
            declared = report.get(declared_field)
        computed = evidence.get(computed_field)
        if computed is None:
            computed = report.get(computed_field)
        if declared is not None and computed is not None and str(declared) != str(computed):
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
    for field in ("return_panel_hash", "candidate_count"):
        expected = evidence.get(field)
        if expected is None:
            expected = report.get(field)
        if expected is None:
            expected = validation.get(field)
        actual = completion.get(field)
        if expected is not None and actual is not None and str(actual or "") != str(expected or ""):
            reasons.append("experiment_registry_stale")
    phase = str(completion.get("statistical_evidence_hash_phase") or "").strip()
    if phase != EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE:
        reasons.append("experiment_registry_evidence_hash_phase_mismatch")
    if evidence:
        bound = str(evidence.get("experiment_registry_bound_evidence_hash") or "").strip()
        if not bound.startswith("sha256:"):
            reasons.append("experiment_registry_bound_evidence_hash_missing")
        elif str(completion.get("statistical_evidence_hash") or "") != bound:
            reasons.append("experiment_registry_statistical_evidence_hash_mismatch")
        evidence_phase = str(evidence.get("experiment_registry_evidence_hash_phase") or "").strip()
        if evidence_phase != EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE:
            reasons.append("experiment_registry_evidence_hash_phase_mismatch")
    validation_bound = str(validation.get("experiment_registry_bound_evidence_hash") or "").strip()
    if validation_bound and str(completion.get("statistical_evidence_hash") or "") != validation_bound:
        reasons.append("experiment_registry_statistical_evidence_hash_mismatch")


def _extend_budget_reasons(
    reasons: list[str],
    *,
    report: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> None:
    contract = (evidence or {}).get("statistical_validation_contract") if isinstance(evidence, dict) else None
    if not isinstance(contract, dict):
        contract = report.get("statistical_validation_contract")
    gates = contract.get("gates") if isinstance(contract, dict) else None
    if not isinstance(gates, dict):
        return
    attempt = _as_int((evidence or {}).get("computed_attempt_index") if isinstance(evidence, dict) else None)
    if attempt is None:
        attempt = _as_int(report.get("computed_attempt_index"))
    reuse = _as_int((evidence or {}).get("computed_holdout_reuse_count") if isinstance(evidence, dict) else None)
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
    objective_metric = str(source.get("objective_metric") or source.get("primary_metric") or "").strip()
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
    if declared_attempt is not None and declared_attempt != computed_attempt_index:
        reasons.append("declared_attempt_index_mismatch")
    if declared_reuse is not None and declared_reuse != computed_holdout_reuse_count:
        reasons.append("declared_holdout_reuse_count_mismatch")
    gates = statistical_validation_contract.get("gates") if isinstance(statistical_validation_contract, dict) else None
    if isinstance(gates, dict):
        max_attempt = _as_int(gates.get("max_attempt_index_without_new_hypothesis"))
        max_reuse = _as_int(gates.get("max_holdout_reuse_count"))
        if max_attempt is not None and computed_attempt_index > max_attempt:
            reasons.extend(["experiment_registry_budget_exceeded", "attempt_budget_exceeded"])
        if max_reuse is not None and computed_holdout_reuse_count > max_reuse:
            reasons.extend(["experiment_registry_budget_exceeded", "holdout_reuse_budget_exceeded"])
    return sorted(set(reasons))


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@contextmanager
def _locked_registry(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
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
