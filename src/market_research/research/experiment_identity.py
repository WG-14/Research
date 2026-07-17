"""Cross-adapter identity binding for the ``research-validate`` workflow.

This registry prevents validation calls that share one configured namespace
authority from using an ``experiment_id`` for different canonical manifests.
It is separate from :mod:`market_research.research.experiment_registry`, whose
rows govern final-holdout exposure and reuse.  The binding is manifest
consistency evidence, not principal ownership or exclusive execution rights.

The binding is intentionally scoped to ``research-validate``.  Standalone
backtest and walk-forward workflows are not registered here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from market_research.paths import ResearchPathError, ResearchPathManager

from .hash_chain import (
    HashChainSnapshot,
    mutate_hash_chained_jsonl_atomic,
    read_hash_chained_jsonl_snapshot,
)


EXPERIMENT_IDENTITY_SCHEMA_VERSION = 1
EXPERIMENT_IDENTITY_SCOPE = "research_validate_manifest_identity"
EXPERIMENT_IDENTITY_HASH_LABEL = "research_validate_experiment_identity"
_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "registry_scope",
        "event_id",
        "experiment_id",
        "manifest_hash",
    }
)
_CHAIN_FIELDS = frozenset({"sequence", "prior_hash", "row_hash"})


class ExperimentIdentityError(ValueError):
    """Base error for a fail-closed validation identity registry decision."""


class ExperimentIdentityConflictError(ExperimentIdentityError):
    """One experiment ID is already bound to another canonical manifest."""

    def __init__(
        self,
        *,
        experiment_id: str,
        bound_manifest_hash: str | None,
        requested_manifest_hash: str,
    ) -> None:
        self.experiment_id = experiment_id
        self.bound_manifest_hash = bound_manifest_hash
        self.requested_manifest_hash = requested_manifest_hash
        bound = bound_manifest_hash or "unknown"
        super().__init__(
            "research_validate_experiment_identity_conflict:"
            f"experiment_id={experiment_id}:"
            f"bound_manifest_hash={bound}:"
            f"requested_manifest_hash={requested_manifest_hash}"
        )


class ExperimentIdentityIntegrityError(ExperimentIdentityError):
    """The append-only identity registry is unreadable or invalid."""


def experiment_identity_registry_path(*, manager: ResearchPathManager) -> Path:
    """Return the shared, repository-external validation identity registry."""

    try:
        return manager.experiment_identity_registry_path()
    except ResearchPathError as exc:
        raise ExperimentIdentityIntegrityError(
            f"research_validate_experiment_identity_authority_invalid:{exc}"
        ) from exc


def bind_research_validation_experiment(
    *,
    manager: ResearchPathManager,
    experiment_id: str,
    manifest_hash: str,
) -> dict[str, Any]:
    """Idempotently bind an experiment ID to one canonical manifest hash.

    The underlying hash-chain primitive performs lookup and append while
    holding the registry stream lock.  Concurrent identical calls return the
    same row; a different manifest for an existing ID raises a domain-specific
    conflict before the validation engine is called.
    """

    normalized_id = _validate_experiment_id(experiment_id)
    normalized_hash = _validate_manifest_hash(manifest_hash)
    path = experiment_identity_registry_path(manager=manager)
    payload = {
        "schema_version": EXPERIMENT_IDENTITY_SCHEMA_VERSION,
        "registry_scope": EXPERIMENT_IDENTITY_SCOPE,
        "event_id": normalized_id,
        "experiment_id": normalized_id,
        "manifest_hash": normalized_hash,
    }

    def mutation(snapshot: HashChainSnapshot, stage: Any) -> dict[str, Any]:
        semantic_reasons, _bindings = _identity_semantics(snapshot.rows)
        if semantic_reasons:
            raise ExperimentIdentityIntegrityError(
                "research_validate_experiment_identity_registry_invalid:"
                + ",".join(semantic_reasons)
            )
        existing = next(
            (row for row in snapshot.rows if row.get("experiment_id") == normalized_id),
            None,
        )
        if existing is not None:
            bound_manifest_hash = str(existing.get("manifest_hash") or "")
            if bound_manifest_hash != normalized_hash:
                raise ExperimentIdentityConflictError(
                    experiment_id=normalized_id,
                    bound_manifest_hash=bound_manifest_hash or None,
                    requested_manifest_hash=normalized_hash,
                )
            return dict(existing)
        return stage(payload)

    try:
        return mutate_hash_chained_jsonl_atomic(
            path=path,
            label=EXPERIMENT_IDENTITY_HASH_LABEL,
            mutation=mutation,
        ).value
    except ExperimentIdentityError:
        raise
    except ValueError as exc:
        raise ExperimentIdentityIntegrityError(
            "research_validate_experiment_identity_registry_invalid:" + str(exc)
        ) from exc


def validate_experiment_identity_registry(
    *, manager: ResearchPathManager
) -> dict[str, Any]:
    """Validate the hash chain and every versioned identity binding row."""

    path = experiment_identity_registry_path(manager=manager)
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path, label=EXPERIMENT_IDENTITY_HASH_LABEL
        )
    except (OSError, TypeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "reasons": [
                "experiment_identity_registry_unreadable:" + type(exc).__name__
            ],
            "row_count": 0,
            "stream_hash": None,
            "bindings": {},
        }

    chain = snapshot.as_validation()
    semantic_reasons, bindings = _identity_semantics(snapshot.rows)
    reasons = [*chain["reasons"], *semantic_reasons]
    return {
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "row_count": chain["row_count"],
        "stream_hash": chain["stream_hash"],
        "bindings": bindings,
    }


def _identity_semantics(
    rows: tuple[dict[str, Any], ...],
) -> tuple[list[str], dict[str, str]]:
    reasons: list[str] = []
    bindings: dict[str, str] = {}
    expected_fields = _PAYLOAD_FIELDS | _CHAIN_FIELDS
    for index, row in enumerate(rows):
        if set(row) != expected_fields:
            reasons.append(f"identity_row_fields_invalid:{index}")
        if row.get("schema_version") != EXPERIMENT_IDENTITY_SCHEMA_VERSION:
            reasons.append(f"identity_schema_version_invalid:{index}")
        if row.get("registry_scope") != EXPERIMENT_IDENTITY_SCOPE:
            reasons.append(f"identity_registry_scope_invalid:{index}")
        experiment_id = row.get("experiment_id")
        if (
            not isinstance(experiment_id, str)
            or not experiment_id
            or experiment_id.strip() != experiment_id
        ):
            reasons.append(f"identity_experiment_id_invalid:{index}")
            continue
        if row.get("event_id") != experiment_id:
            reasons.append(f"identity_event_id_mismatch:{index}")
        manifest_hash = row.get("manifest_hash")
        if (
            not isinstance(manifest_hash, str)
            or _HASH_PATTERN.fullmatch(manifest_hash) is None
        ):
            reasons.append(f"identity_manifest_hash_invalid:{index}")
            continue
        if experiment_id in bindings:
            reasons.append(f"identity_experiment_id_duplicate:{index}")
        else:
            bindings[experiment_id] = manifest_hash
    return sorted(set(reasons)), bindings


def _validate_experiment_id(value: object) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ExperimentIdentityError(
            "research_validate_experiment_identity_id_invalid"
        )
    return value


def _validate_manifest_hash(value: object) -> str:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ExperimentIdentityError(
            "research_validate_experiment_identity_manifest_hash_invalid"
        )
    return value
