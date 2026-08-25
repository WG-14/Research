"""Fail-closed phase-use policy and append-only exposure evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from market_research.paths import ResearchPathManager

from .hash_chain import (
    HashChainSnapshot,
    mutate_hash_chained_jsonl_atomic,
    read_hash_chained_jsonl_snapshot,
)
from .research_standard import (
    ResearchPhase,
    parse_timestamp,
    require_hash,
    require_stable_id,
)


FINAL_HOLDOUT_DIAGNOSTIC_OVERRIDE_REQUIRED = (
    "final_holdout_diagnostic_override_required"
)
FINAL_HOLDOUT_DIAGNOSTIC_CONTAMINATION_RISK = (
    "final_holdout_diagnostic_contamination_risk"
)
SPLIT_EXPOSURE_SCHEMA_VERSION = 1
SPLIT_EXPOSURE_HASH_LABEL = "research_split_exposure"


class SplitExposurePurpose(StrEnum):
    HYPOTHESIS_GENERATION = "hypothesis_generation"
    EXPLORATION_ANALYSIS = "exploration_analysis"
    FEATURE_MINING = "feature_mining"
    PARAMETER_TUNING = "parameter_tuning"
    MODEL_SELECTION = "model_selection"
    CONFIRMATORY_VALIDATION = "confirmatory_validation"
    FINAL_CONFIRMATION = "final_confirmation"
    INDEPENDENT_REPRODUCTION = "independent_reproduction"


_PURPOSE_PHASE: dict[SplitExposurePurpose, ResearchPhase] = {
    SplitExposurePurpose.HYPOTHESIS_GENERATION: ResearchPhase.EXPLORATION,
    SplitExposurePurpose.EXPLORATION_ANALYSIS: ResearchPhase.EXPLORATION,
    SplitExposurePurpose.FEATURE_MINING: ResearchPhase.EXPLORATION,
    SplitExposurePurpose.PARAMETER_TUNING: ResearchPhase.DEVELOPMENT,
    SplitExposurePurpose.MODEL_SELECTION: ResearchPhase.DEVELOPMENT,
    SplitExposurePurpose.CONFIRMATORY_VALIDATION: ResearchPhase.VALIDATION,
    SplitExposurePurpose.FINAL_CONFIRMATION: ResearchPhase.FINAL_HOLDOUT,
    SplitExposurePurpose.INDEPENDENT_REPRODUCTION: ResearchPhase.FINAL_HOLDOUT,
}


class SplitUsagePolicyError(ValueError):
    def __init__(self, *, reason: str, split_name: str, purpose: str) -> None:
        self.reason = reason
        self.split_name = split_name
        self.purpose = purpose
        super().__init__(reason)


def validate_split_usage(
    *,
    split_name: str,
    purpose: str,
    explicit_override: bool = False,
) -> tuple[dict[str, Any], ...]:
    """Validate one data-use purpose against its preregistered phase."""

    split = str(split_name or "").strip()
    usage_purpose = str(purpose or "").strip()
    if usage_purpose == "feature_mining" and split == "final_holdout":
        if not explicit_override:
            raise SplitUsagePolicyError(
                reason=FINAL_HOLDOUT_DIAGNOSTIC_OVERRIDE_REQUIRED,
                split_name=split,
                purpose=usage_purpose,
            )
        return (
            {
                "reason": FINAL_HOLDOUT_DIAGNOSTIC_CONTAMINATION_RISK,
                "split_name": split,
            },
        )
    try:
        phase = ResearchPhase.EXPLORATION if split == "train" else ResearchPhase(split)
        normalized_purpose = SplitExposurePurpose(usage_purpose)
    except ValueError as exc:
        raise SplitUsagePolicyError(
            reason="split_usage_phase_or_purpose_unknown",
            split_name=split,
            purpose=usage_purpose,
        ) from exc
    if _PURPOSE_PHASE[normalized_purpose] is not phase:
        raise SplitUsagePolicyError(
            reason="split_usage_purpose_phase_mismatch",
            split_name=split,
            purpose=usage_purpose,
        )
    return ()


def split_exposure_registry_path(manager: ResearchPathManager) -> Path:
    path = manager.artifact_path(
        "reports",
        "research",
        "_registry",
        "split_exposures.jsonl",
    )
    if manager.is_within(path, manager.project_root):
        raise SplitUsagePolicyError(
            reason="split_exposure_registry_must_be_repository_external",
            split_name="",
            purpose="",
        )
    return path


def record_split_exposure(
    *,
    manager: ResearchPathManager,
    event_id: str,
    hypothesis_id: str,
    hypothesis_version: str,
    split_name: ResearchPhase | str,
    purpose: SplitExposurePurpose | str,
    actor_id: str,
    recorded_at: str,
    source_artifact_hash: str,
) -> dict[str, Any]:
    """Append one immutable, idempotent phase exposure event."""

    phase = ResearchPhase(split_name)
    normalized_purpose = SplitExposurePurpose(purpose)
    validate_split_usage(split_name=phase.value, purpose=normalized_purpose.value)
    if phase is ResearchPhase.FINAL_HOLDOUT:
        # The experiment registry capability-consumption event is the sole
        # final-holdout exposure authority.  A second writable ledger would
        # create a competing count/fence and could diverge after a crash.
        raise SplitUsagePolicyError(
            reason="final_holdout_exposure_owned_by_experiment_registry",
            split_name=phase.value,
            purpose=normalized_purpose.value,
        )
    require_stable_id(event_id, "split_exposure.event_id")
    require_stable_id(hypothesis_id, "split_exposure.hypothesis_id")
    require_stable_id(hypothesis_version, "split_exposure.hypothesis_version")
    require_stable_id(actor_id, "split_exposure.actor_id")
    parse_timestamp(recorded_at, "split_exposure.recorded_at")
    require_hash(source_artifact_hash, "split_exposure.source_artifact_hash")
    path = split_exposure_registry_path(manager)

    def mutation(
        snapshot: HashChainSnapshot,
        stage: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        existing = [row for row in snapshot.rows if row.get("event_id") == event_id]
        immutable_input = {
            "schema_version": SPLIT_EXPOSURE_SCHEMA_VERSION,
            "event_id": event_id,
            "hypothesis_id": hypothesis_id,
            "hypothesis_version": hypothesis_version,
            "split_name": phase.value,
            "purpose": normalized_purpose.value,
            "actor_id": actor_id,
            "recorded_at": recorded_at,
            "source_artifact_hash": source_artifact_hash,
        }
        if existing:
            if len(existing) != 1 or any(
                existing[0].get(key) != value for key, value in immutable_input.items()
            ):
                raise SplitUsagePolicyError(
                    reason="split_exposure_event_conflict",
                    split_name=phase.value,
                    purpose=normalized_purpose.value,
                )
            return dict(existing[0])
        prior_hypothesis_rows = [
            row
            for row in snapshot.rows
            if row.get("hypothesis_id") == hypothesis_id
            and row.get("hypothesis_version") == hypothesis_version
        ]
        if prior_hypothesis_rows:
            latest = max(
                parse_timestamp(
                    str(row.get("recorded_at") or ""),
                    "split_exposure.recorded_at",
                )
                for row in prior_hypothesis_rows
            )
            if parse_timestamp(recorded_at, "split_exposure.recorded_at") <= latest:
                raise SplitUsagePolicyError(
                    reason="split_exposure_timestamps_not_strictly_increasing",
                    split_name=phase.value,
                    purpose=normalized_purpose.value,
                )
        exposure_count = sum(
            1
            for row in prior_hypothesis_rows
            if row.get("hypothesis_id") == hypothesis_id
            and row.get("hypothesis_version") == hypothesis_version
            and row.get("split_name") == phase.value
        )
        purity_status = (
            "REPEATED_CONFIRMATORY_ACCESS"
            if phase in {ResearchPhase.VALIDATION, ResearchPhase.FINAL_HOLDOUT}
            and exposure_count > 0
            else "PURE"
        )
        payload = {
            **immutable_input,
            "prior_phase_exposure_count": exposure_count,
            "purity_status": purity_status,
        }
        return stage(payload)

    try:
        return dict(
            mutate_hash_chained_jsonl_atomic(
                path=path,
                label=SPLIT_EXPOSURE_HASH_LABEL,
                mutation=mutation,
            ).value
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        if isinstance(exc, SplitUsagePolicyError):
            raise
        raise SplitUsagePolicyError(
            reason=f"split_exposure_registry_write_failed:{exc}",
            split_name=phase.value,
            purpose=normalized_purpose.value,
        ) from exc


def split_exposure_rows(
    manager: ResearchPathManager,
    *,
    hypothesis_id: str,
    hypothesis_version: str,
) -> tuple[dict[str, Any], ...]:
    require_stable_id(hypothesis_id, "split_exposure.hypothesis_id")
    require_stable_id(hypothesis_version, "split_exposure.hypothesis_version")
    path = split_exposure_registry_path(manager)
    snapshot = read_hash_chained_jsonl_snapshot(
        path=path,
        label=SPLIT_EXPOSURE_HASH_LABEL,
    )
    if snapshot.status != "PASS":
        raise SplitUsagePolicyError(
            reason="split_exposure_registry_invalid",
            split_name="",
            purpose="",
        )
    recorded = tuple(
        dict(row)
        for row in snapshot.rows
        if row.get("hypothesis_id") == hypothesis_id
        and row.get("hypothesis_version") == hypothesis_version
    )
    projected = _authoritative_final_holdout_exposure_rows(
        manager=manager,
        hypothesis_id=hypothesis_id,
        hypothesis_version=hypothesis_version,
    )
    return tuple(
        sorted(
            (*recorded, *projected),
            key=lambda row: (
                parse_timestamp(
                    str(row.get("recorded_at") or ""),
                    "split_exposure.recorded_at",
                ),
                str(row.get("event_id") or ""),
            ),
        )
    )


def _authoritative_final_holdout_exposure_rows(
    *,
    manager: ResearchPathManager,
    hypothesis_id: str,
    hypothesis_version: str,
) -> tuple[dict[str, Any], ...]:
    """Project final-holdout reads from the sole experiment-registry authority."""

    # Keep the phase policy independent of the authority implementation at
    # import time.  The experiment registry never imports this module.
    from .experiment_registry import (
        experiment_registry_chain_reasons,
        experiment_registry_path,
        load_experiment_registry_rows,
    )

    try:
        rows = load_experiment_registry_rows(experiment_registry_path(manager=manager))
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise SplitUsagePolicyError(
            reason=f"final_holdout_exposure_authority_unreadable:{exc}",
            split_name=ResearchPhase.FINAL_HOLDOUT.value,
            purpose=SplitExposurePurpose.FINAL_CONFIRMATION.value,
        ) from exc
    if experiment_registry_chain_reasons(rows):
        raise SplitUsagePolicyError(
            reason="final_holdout_exposure_authority_invalid",
            split_name=ResearchPhase.FINAL_HOLDOUT.value,
            purpose=SplitExposurePurpose.FINAL_CONFIRMATION.value,
        )
    reservations = {
        str(row.get("row_hash") or ""): row
        for row in rows
        if row.get("event_type") == "research_attempt_reserved"
        and str(row.get("hypothesis_id")) == hypothesis_id
        and str(row.get("hypothesis_version")) == hypothesis_version
        and row.get("final_holdout_authority_contract_schema_version") is not None
    }
    reads = [
        (row, reservations[str(row.get("reservation_row_hash") or "")])
        for row in rows
        if row.get("event_type") == "research_attempt_holdout_read_started"
        and str(row.get("reservation_row_hash") or "") in reservations
    ]
    projection: list[dict[str, Any]] = []
    for index, (row, reservation) in enumerate(reads):
        purpose = (
            SplitExposurePurpose.INDEPENDENT_REPRODUCTION
            if reservation.get("final_holdout_access_purpose")
            == "INDEPENDENT_REPRODUCTION"
            else SplitExposurePurpose.FINAL_CONFIRMATION
        )
        recorded_at = str(row.get("created_at") or "")
        parse_timestamp(recorded_at, "split_exposure.recorded_at")
        source_hash = str(row.get("pre_holdout_gate_hash") or "")
        require_hash(source_hash, "split_exposure.source_artifact_hash")
        authority_row_hash = str(row.get("row_hash") or "")
        require_hash(authority_row_hash, "split_exposure.authority_event_row_hash")
        projection.append(
            {
                "schema_version": SPLIT_EXPOSURE_SCHEMA_VERSION,
                "event_id": "final-holdout-authority:" + authority_row_hash[7:],
                "hypothesis_id": hypothesis_id,
                "hypothesis_version": hypothesis_version,
                "split_name": ResearchPhase.FINAL_HOLDOUT.value,
                "purpose": purpose.value,
                "actor_id": "final-holdout-authority",
                "actor_binding_hash": reservation.get("reservation_actor_binding_hash"),
                "recorded_at": recorded_at,
                "source_artifact_hash": source_hash,
                "authority_event_row_hash": authority_row_hash,
                "reservation_row_hash": row.get("reservation_row_hash"),
                "prior_phase_exposure_count": index,
                "purity_status": (
                    "PURE" if index == 0 else "REPEATED_CONFIRMATORY_ACCESS"
                ),
            }
        )
    return tuple(projection)


def require_successor_after_confirmatory_exposure(
    *,
    manager: ResearchPathManager,
    hypothesis_id: str,
    exposed_version: str,
    proposed_version: str,
) -> None:
    """Forbid material edits in-place after validation/holdout observation."""

    rows = split_exposure_rows(
        manager,
        hypothesis_id=hypothesis_id,
        hypothesis_version=exposed_version,
    )
    if (
        any(
            row.get("split_name")
            in {ResearchPhase.VALIDATION.value, ResearchPhase.FINAL_HOLDOUT.value}
            for row in rows
        )
        and proposed_version == exposed_version
    ):
        raise SplitUsagePolicyError(
            reason="material_amendment_requires_new_hypothesis_version",
            split_name="validation_or_final_holdout",
            purpose="material_amendment",
        )


def latest_confirmatory_exposure_at(
    *,
    manager: ResearchPathManager,
    hypothesis_id: str,
    hypothesis_version: str,
) -> datetime | None:
    timestamps = [
        parse_timestamp(str(row["recorded_at"]), "split_exposure.recorded_at")
        for row in split_exposure_rows(
            manager,
            hypothesis_id=hypothesis_id,
            hypothesis_version=hypothesis_version,
        )
        if row.get("split_name")
        in {ResearchPhase.VALIDATION.value, ResearchPhase.FINAL_HOLDOUT.value}
    ]
    return max(timestamps) if timestamps else None
