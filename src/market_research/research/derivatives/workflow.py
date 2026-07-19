"""Fail-closed file workflow for immutable derivative research evidence.

The evidence graph owns the domain validation and repository-external registry.
This module adds one strict transport envelope so the same complete graph can be
registered or replay-verified through the public research CLI without importing
the spot-only manifest workflow.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from market_research.paths import ResearchPathManager
from market_research.research.hashing import sha256_prefixed

from .common import (
    DERIVATIVE_RESEARCH_SCHEMA_VERSION,
    DerivativeDatasetSnapshot,
    DerivativeExperimentRun,
    DerivativeExperimentSpec,
    InstrumentKind,
    QualityDecision,
    QualityResult,
    RunType,
    derivative_dataset_filter_from_dict,
)
from .evidence import (
    DerivativeEvidenceError,
    DerivativeEvidenceRegistry,
    DerivativeResearchPackageManifest,
    EvidenceRef,
    ProspectiveValidationEvidence,
    ReplayVerificationReceipt,
    ResearchConclusion,
    RobustnessResult,
    ValidationDecision,
    _supporting_payload_hash,
)


DERIVATIVE_EVIDENCE_BUNDLE_SCHEMA_VERSION = DERIVATIVE_RESEARCH_SCHEMA_VERSION
_MAX_BUNDLE_BYTES = 16 * 1024 * 1024
_BUNDLE_FIELDS = {
    "schema_version",
    "artifact_type",
    "package",
    "dataset",
    "experiment_spec",
    "experiment_run",
    "decision",
    "robustness",
    "prospective",
    "conclusion",
    "supporting_evidence",
    "content_hash",
}
_FORBIDDEN_KEYS = frozenset(
    {
        "approval",
        "approved",
        "approval_status",
        "live_approval",
        "approved_for_live",
        "account",
        "account_id",
        "broker_account",
        "deployment",
        "deployment_id",
        "deployment_target",
        "capital",
        "capital_allocation",
        "order_route",
        "order_submission",
        "broker_api_key",
    }
)


class DerivativeEvidenceWorkflowError(DerivativeEvidenceError):
    """An external derivative evidence bundle is unsafe or inconsistent."""


@dataclass(frozen=True, slots=True)
class DerivativeEvidenceBundle:
    """Complete immutable transport envelope for one derivative evidence graph."""

    package: DerivativeResearchPackageManifest
    dataset: DerivativeDatasetSnapshot
    experiment_spec: DerivativeExperimentSpec
    experiment_run: DerivativeExperimentRun
    decision: ValidationDecision
    robustness: RobustnessResult
    prospective: ProspectiveValidationEvidence
    conclusion: ResearchConclusion
    supporting_evidence: Mapping[EvidenceRef, Mapping[str, object]]
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_EVIDENCE_BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DERIVATIVE_EVIDENCE_BUNDLE_SCHEMA_VERSION:
            raise DerivativeEvidenceWorkflowError(
                "derivative_evidence_bundle_schema_unsupported"
            )
        normalized: dict[EvidenceRef, dict[str, object]] = {}
        for ref, raw_payload in self.supporting_evidence.items():
            if not isinstance(ref, EvidenceRef):
                raise DerivativeEvidenceWorkflowError(
                    "derivative_bundle_supporting_ref_invalid"
                )
            payload = _json_object(raw_payload, "supporting_evidence.payload")
            _reject_forbidden_fields(payload, "supporting_evidence.payload")
            observed_hash = _supporting_payload_hash(ref, payload)
            if observed_hash != ref.content_hash:
                raise DerivativeEvidenceWorkflowError(
                    "derivative_bundle_supporting_evidence_hash_mismatch:"
                    f"{_ref_key(ref)}"
                )
            normalized[ref] = payload
        object.__setattr__(self, "supporting_evidence", normalized)
        _validate_transport_bindings(self)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="derivative_evidence_bundle"
            ),
        )

    @classmethod
    def from_dict(cls, value: object) -> "DerivativeEvidenceBundle":
        payload = _mapping(value, "derivative_bundle")
        _reject_forbidden_fields(payload, "derivative_bundle")
        _require_exact_fields(payload, _BUNDLE_FIELDS, "derivative_bundle")
        if payload["artifact_type"] != "derivative_evidence_bundle":
            raise DerivativeEvidenceWorkflowError(
                "derivative_evidence_bundle_artifact_type_invalid"
            )
        supporting_rows = _sequence(
            payload["supporting_evidence"], "derivative_bundle.supporting_evidence"
        )
        supporting: dict[EvidenceRef, dict[str, object]] = {}
        for index, raw_row in enumerate(supporting_rows):
            label = f"derivative_bundle.supporting_evidence[{index}]"
            row = _mapping(raw_row, label)
            _require_exact_fields(row, {"ref", "payload"}, label)
            ref = EvidenceRef.from_dict(row["ref"], f"{label}.ref")
            if ref in supporting:
                raise DerivativeEvidenceWorkflowError(
                    f"derivative_bundle_supporting_ref_duplicate:{_ref_key(ref)}"
                )
            supporting[ref] = _json_object(row["payload"], f"{label}.payload")
        result = cls(
            schema_version=_integer(
                payload["schema_version"], "derivative_bundle.schema_version"
            ),
            package=DerivativeResearchPackageManifest.from_dict(payload["package"]),
            dataset=_dataset_from_dict(payload["dataset"]),
            experiment_spec=_experiment_spec_from_dict(payload["experiment_spec"]),
            experiment_run=_experiment_run_from_dict(payload["experiment_run"]),
            decision=ValidationDecision.from_dict(payload["decision"]),
            robustness=RobustnessResult.from_dict(payload["robustness"]),
            prospective=ProspectiveValidationEvidence.from_dict(payload["prospective"]),
            conclusion=ResearchConclusion.from_dict(payload["conclusion"]),
            supporting_evidence=supporting,
        )
        if payload["content_hash"] != result.content_hash:
            raise DerivativeEvidenceWorkflowError(
                "derivative_evidence_bundle_content_hash_mismatch"
            )
        return result

    @classmethod
    def load(
        cls, path: str | Path, manager: ResearchPathManager
    ) -> "DerivativeEvidenceBundle":
        return cls.from_dict(_read_external_json(path, manager, "bundle"))

    def identity_payload(self) -> dict[str, object]:
        supporting = [
            {"ref": ref.as_dict(), "payload": dict(payload)}
            for ref, payload in sorted(
                self.supporting_evidence.items(), key=lambda item: _ref_key(item[0])
            )
        ]
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_evidence_bundle",
            "package": self.package.as_dict(),
            "dataset": self.dataset.as_dict(),
            "experiment_spec": self.experiment_spec.as_dict(),
            "experiment_run": self.experiment_run.as_dict(),
            "decision": self.decision.as_dict(),
            "robustness": self.robustness.as_dict(),
            "prospective": self.prospective.as_dict(),
            "conclusion": self.conclusion.as_dict(),
            "supporting_evidence": supporting,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def register(self, manager: ResearchPathManager) -> EvidenceRef:
        registry = DerivativeEvidenceRegistry(manager)
        return registry.register(
            self.package,
            dataset=self.dataset,
            experiment_spec=self.experiment_spec,
            experiment_run=self.experiment_run,
            decision=self.decision,
            robustness=self.robustness,
            prospective=self.prospective,
            conclusion=self.conclusion,
            supporting_evidence=self.supporting_evidence,
        )

    def verify_replay(
        self, manager: ResearchPathManager, *, verified_at: str
    ) -> ReplayVerificationReceipt:
        registry = DerivativeEvidenceRegistry(manager)
        return registry.verify_replay(
            self.package.package_id,
            self.package.version,
            dataset=self.dataset,
            experiment_spec=self.experiment_spec,
            experiment_run=self.experiment_run,
            decision=self.decision,
            robustness=self.robustness,
            prospective=self.prospective,
            conclusion=self.conclusion,
            supporting_evidence=self.supporting_evidence,
            verified_at=verified_at,
        )


def register_derivative_evidence_bundle(
    manager: ResearchPathManager, bundle_path: str | Path
) -> EvidenceRef:
    """Parse and atomically register a complete external evidence bundle."""

    return DerivativeEvidenceBundle.load(bundle_path, manager).register(manager)


def replay_derivative_evidence_bundle(
    manager: ResearchPathManager,
    bundle_path: str | Path,
    *,
    verified_at: str,
) -> ReplayVerificationReceipt:
    """Replay-verify a bundle against the already registered immutable graph."""

    return DerivativeEvidenceBundle.load(bundle_path, manager).verify_replay(
        manager, verified_at=verified_at
    )


def diff_derivative_evidence_packages(
    manager: ResearchPathManager,
    *,
    left_package_id: str,
    left_version: str,
    right_package_id: str,
    right_version: str,
) -> dict[str, object]:
    """Hash-verify and compare two package identities from the registry."""

    return DerivativeEvidenceRegistry(manager).diff(
        left_package_id,
        left_version,
        right_package_id,
        right_version,
    )


def _validate_transport_bindings(bundle: DerivativeEvidenceBundle) -> None:
    package = bundle.package
    dataset_ref = EvidenceRef(
        authority="derivative_dataset_snapshot",
        logical_id=bundle.dataset.snapshot_id,
        version=str(bundle.dataset.schema_version),
        content_hash=bundle.dataset.content_hash,
    )
    experiment_spec_ref = EvidenceRef(
        authority="derivative_experiment_spec",
        logical_id=bundle.experiment_spec.experiment_id,
        version=str(bundle.experiment_spec.schema_version),
        content_hash=bundle.experiment_spec.content_hash,
    )
    experiment_run_ref = EvidenceRef(
        authority="derivative_experiment_run",
        logical_id=bundle.experiment_run.run_id,
        version=str(bundle.experiment_run.schema_version),
        content_hash=bundle.experiment_run.content_hash,
    )
    if package.inputs.dataset_snapshot_ref != dataset_ref:
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_dataset_snapshot_ref_mismatch"
        )
    if package.inputs.experiment_spec_ref != experiment_spec_ref:
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_experiment_spec_ref_mismatch"
        )
    if package.inputs.experiment_run_ref != experiment_run_ref:
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_experiment_run_ref_mismatch"
        )
    if bundle.experiment_spec.dataset_snapshot_hash != bundle.dataset.content_hash:
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_experiment_dataset_hash_mismatch"
        )
    if (
        bundle.experiment_run.experiment_spec_hash
        != bundle.experiment_spec.content_hash
    ):
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_run_experiment_hash_mismatch"
        )
    if bundle.experiment_run.dataset_snapshot_hash != bundle.dataset.content_hash:
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_run_dataset_hash_mismatch"
        )
    if package.validation_decision_ref != bundle.decision.ref():
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_validation_decision_ref_mismatch"
        )
    if package.knowledge_archive_ref not in bundle.supporting_evidence:
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_knowledge_archive_payload_missing"
        )
    if package.robustness_result_ref != bundle.robustness.ref():
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_robustness_result_ref_mismatch"
        )
    if package.risk_evidence_ref != bundle.robustness.risk_evidence_ref:
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_risk_evidence_ref_mismatch"
        )
    if package.risk_evidence_ref not in bundle.supporting_evidence:
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_risk_evidence_payload_missing"
        )
    if bundle.conclusion.risk_evidence_ref != package.risk_evidence_ref:
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_conclusion_risk_evidence_ref_mismatch"
        )
    if package.prospective_validation_ref != bundle.prospective.ref():
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_prospective_validation_ref_mismatch"
        )
    if package.research_conclusion_ref != bundle.conclusion.ref():
        raise DerivativeEvidenceWorkflowError(
            "derivative_bundle_research_conclusion_ref_mismatch"
        )


def _dataset_from_dict(value: object) -> DerivativeDatasetSnapshot:
    payload = _mapping(value, "dataset")
    expected = {
        "schema_version",
        "snapshot_id",
        "instrument_kind",
        "knowledge_time",
        "raw_manifest_hashes",
        "normalized_dataset_hash",
        "chain_snapshot_hashes",
        "feature_definition_hashes",
        "calendar_hash",
        "policy_hashes",
        "quality_results",
        "universe_ids",
        "period_start",
        "period_end",
        "filter_contract",
        "content_hash",
    }
    _require_exact_fields(payload, expected, "dataset")
    quality: list[QualityResult] = []
    for index, raw in enumerate(
        _sequence(payload["quality_results"], "dataset.quality_results")
    ):
        label = f"dataset.quality_results[{index}]"
        row = _mapping(raw, label)
        _require_exact_fields(
            row,
            {"check_id", "check_version", "decision", "affected_ids", "diagnostics"},
            label,
        )
        quality.append(
            QualityResult(
                check_id=_text(row["check_id"], f"{label}.check_id"),
                check_version=_text(row["check_version"], f"{label}.check_version"),
                decision=_enum(QualityDecision, row["decision"], f"{label}.decision"),
                affected_ids=_texts(row["affected_ids"], f"{label}.affected_ids"),
                diagnostics=_texts(row["diagnostics"], f"{label}.diagnostics"),
            )
        )
    instrument_kind = _enum(
        InstrumentKind, payload["instrument_kind"], "dataset.instrument_kind"
    )
    result = DerivativeDatasetSnapshot(
        schema_version=_integer(payload["schema_version"], "dataset.schema_version"),
        snapshot_id=_text(payload["snapshot_id"], "dataset.snapshot_id"),
        instrument_kind=instrument_kind,
        knowledge_time=_text(payload["knowledge_time"], "dataset.knowledge_time"),
        raw_manifest_hashes=_texts(
            payload["raw_manifest_hashes"], "dataset.raw_manifest_hashes"
        ),
        normalized_dataset_hash=_text(
            payload["normalized_dataset_hash"], "dataset.normalized_dataset_hash"
        ),
        chain_snapshot_hashes=_texts(
            payload["chain_snapshot_hashes"], "dataset.chain_snapshot_hashes"
        ),
        feature_definition_hashes=_texts(
            payload["feature_definition_hashes"],
            "dataset.feature_definition_hashes",
        ),
        calendar_hash=_text(payload["calendar_hash"], "dataset.calendar_hash"),
        policy_hashes=_texts(payload["policy_hashes"], "dataset.policy_hashes"),
        quality_results=tuple(quality),
        universe_ids=_texts(payload["universe_ids"], "dataset.universe_ids"),
        period_start=_text(payload["period_start"], "dataset.period_start"),
        period_end=_text(payload["period_end"], "dataset.period_end"),
        filter_contract=derivative_dataset_filter_from_dict(
            payload["filter_contract"], instrument_kind
        ),
    )
    _require_content_hash(payload, result.content_hash, "dataset")
    return result


def _experiment_spec_from_dict(value: object) -> DerivativeExperimentSpec:
    payload = _mapping(value, "experiment_spec")
    expected = {
        "schema_version",
        "experiment_id",
        "hypothesis_version_hash",
        "dataset_snapshot_hash",
        "feature_version_hashes",
        "run_type",
        "signal_policy_hash",
        "simulation_policy_hash",
        "cost_model_hash",
        "fill_model_hash",
        "position_sizing_hash",
        "metric_policy_hash",
        "acceptance_policy_hash",
        "robustness_policy_hash",
        "random_seed",
        "frozen_at",
        "code_version",
        "environment_hash",
        "dirty_worktree",
        "content_hash",
    }
    _require_exact_fields(payload, expected, "experiment_spec")
    dirty_worktree = payload["dirty_worktree"]
    if not isinstance(dirty_worktree, bool):
        raise DerivativeEvidenceWorkflowError(
            "experiment_spec_dirty_worktree_boolean_required"
        )
    result = DerivativeExperimentSpec(
        schema_version=_integer(
            payload["schema_version"], "experiment_spec.schema_version"
        ),
        experiment_id=_text(payload["experiment_id"], "experiment_spec.experiment_id"),
        hypothesis_version_hash=_text(
            payload["hypothesis_version_hash"],
            "experiment_spec.hypothesis_version_hash",
        ),
        dataset_snapshot_hash=_text(
            payload["dataset_snapshot_hash"], "experiment_spec.dataset_snapshot_hash"
        ),
        feature_version_hashes=_texts(
            payload["feature_version_hashes"],
            "experiment_spec.feature_version_hashes",
        ),
        run_type=_enum(RunType, payload["run_type"], "experiment_spec.run_type"),
        signal_policy_hash=_text(
            payload["signal_policy_hash"], "experiment_spec.signal_policy_hash"
        ),
        simulation_policy_hash=_text(
            payload["simulation_policy_hash"],
            "experiment_spec.simulation_policy_hash",
        ),
        cost_model_hash=_text(
            payload["cost_model_hash"], "experiment_spec.cost_model_hash"
        ),
        fill_model_hash=_text(
            payload["fill_model_hash"], "experiment_spec.fill_model_hash"
        ),
        position_sizing_hash=_text(
            payload["position_sizing_hash"],
            "experiment_spec.position_sizing_hash",
        ),
        metric_policy_hash=_text(
            payload["metric_policy_hash"], "experiment_spec.metric_policy_hash"
        ),
        acceptance_policy_hash=_text(
            payload["acceptance_policy_hash"],
            "experiment_spec.acceptance_policy_hash",
        ),
        robustness_policy_hash=_text(
            payload["robustness_policy_hash"],
            "experiment_spec.robustness_policy_hash",
        ),
        random_seed=_integer(payload["random_seed"], "experiment_spec.random_seed"),
        frozen_at=_text(payload["frozen_at"], "experiment_spec.frozen_at"),
        code_version=_text(payload["code_version"], "experiment_spec.code_version"),
        environment_hash=_text(
            payload["environment_hash"], "experiment_spec.environment_hash"
        ),
        dirty_worktree=dirty_worktree,
    )
    _require_content_hash(payload, result.content_hash, "experiment_spec")
    return result


def _experiment_run_from_dict(value: object) -> DerivativeExperimentRun:
    payload = _mapping(value, "experiment_run")
    expected = {
        "schema_version",
        "run_id",
        "experiment_spec_hash",
        "dataset_snapshot_hash",
        "started_at",
        "finished_at",
        "status",
        "event_stream_hash",
        "result_artifact_hash",
        "failure_code",
        "content_hash",
    }
    _require_exact_fields(payload, expected, "experiment_run")
    failure_code = payload["failure_code"]
    if failure_code is not None and not isinstance(failure_code, str):
        raise DerivativeEvidenceWorkflowError("experiment_run_failure_code_invalid")
    result = DerivativeExperimentRun(
        schema_version=_integer(
            payload["schema_version"], "experiment_run.schema_version"
        ),
        run_id=_text(payload["run_id"], "experiment_run.run_id"),
        experiment_spec_hash=_text(
            payload["experiment_spec_hash"],
            "experiment_run.experiment_spec_hash",
        ),
        dataset_snapshot_hash=_text(
            payload["dataset_snapshot_hash"],
            "experiment_run.dataset_snapshot_hash",
        ),
        started_at=_text(payload["started_at"], "experiment_run.started_at"),
        finished_at=_text(payload["finished_at"], "experiment_run.finished_at"),
        status=_text(payload["status"], "experiment_run.status"),
        event_stream_hash=_text(
            payload["event_stream_hash"], "experiment_run.event_stream_hash"
        ),
        result_artifact_hash=_text(
            payload["result_artifact_hash"],
            "experiment_run.result_artifact_hash",
        ),
        failure_code=failure_code,
    )
    _require_content_hash(payload, result.content_hash, "experiment_run")
    return result


def _read_external_json(
    value: str | Path, manager: ResearchPathManager, label: str
) -> dict[str, object]:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise DerivativeEvidenceWorkflowError(
            f"derivative_{label}_path_must_be_absolute"
        )
    resolved = raw.resolve()
    if manager.is_within(resolved, manager.project_root):
        raise DerivativeEvidenceWorkflowError(
            f"derivative_{label}_path_must_be_repository_external:{resolved}"
        )
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise DerivativeEvidenceWorkflowError("derivative_bundle_no_follow_unavailable")
    try:
        descriptor = os.open(raw, os.O_RDONLY | no_follow)
    except OSError as exc:
        raise DerivativeEvidenceWorkflowError(
            f"derivative_{label}_unreadable:{resolved}"
        ) from exc
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise DerivativeEvidenceWorkflowError(
                f"derivative_{label}_must_be_regular_file"
            )
        if file_stat.st_size <= 0 or file_stat.st_size > _MAX_BUNDLE_BYTES:
            raise DerivativeEvidenceWorkflowError(f"derivative_{label}_size_invalid")
        chunks: list[bytes] = []
        remaining = file_stat.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise DerivativeEvidenceWorkflowError(f"derivative_{label}_truncated")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1) or os.fstat(descriptor).st_size != file_stat.st_size:
            raise DerivativeEvidenceWorkflowError(
                f"derivative_{label}_changed_during_read"
            )
    finally:
        os.close(descriptor)
    try:
        decoded = json.loads(
            b"".join(chunks).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DerivativeEvidenceWorkflowError(
            f"derivative_{label}_json_invalid"
        ) from exc
    return _json_object(decoded, label)


def _reject_forbidden_fields(value: object, path: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if key in _FORBIDDEN_KEYS:
                raise DerivativeEvidenceWorkflowError(
                    f"derivative_bundle_live_field_forbidden:{path}.{key}"
                )
            _reject_forbidden_fields(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_forbidden_fields(child, f"{path}[{index}]")


def _reject_duplicate_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DerivativeEvidenceWorkflowError(
                f"derivative_bundle_duplicate_json_key:{key}"
            )
        result[key] = value
    return result


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise DerivativeEvidenceWorkflowError(f"{label}_must_be_object")
    return value


def _json_object(value: object, label: str) -> dict[str, object]:
    payload = _mapping(value, label)
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        result = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise DerivativeEvidenceWorkflowError(f"{label}_not_json_safe") from exc
    if not isinstance(result, dict):
        raise DerivativeEvidenceWorkflowError(f"{label}_must_be_object")
    return result


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise DerivativeEvidenceWorkflowError(f"{label}_must_be_array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DerivativeEvidenceWorkflowError(f"{label}_must_be_text")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DerivativeEvidenceWorkflowError(f"{label}_must_be_integer")
    return value


def _texts(value: object, label: str) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{label}[{index}]")
        for index, item in enumerate(_sequence(value, label))
    )


def _enum(enum_type: type, value: object, label: str):  # type: ignore[no-untyped-def]
    text = _text(value, label)
    try:
        return enum_type(text)
    except ValueError as exc:
        raise DerivativeEvidenceWorkflowError(f"{label}_unknown:{text}") from exc


def _require_exact_fields(
    payload: Mapping[str, object], expected: set[str], label: str
) -> None:
    observed = set(payload)
    if observed != expected:
        missing = ",".join(sorted(expected - observed)) or "none"
        unknown = ",".join(sorted(observed - expected)) or "none"
        raise DerivativeEvidenceWorkflowError(
            f"{label}_fields_invalid:missing={missing}:unknown={unknown}"
        )


def _require_content_hash(
    payload: Mapping[str, object], expected: str, label: str
) -> None:
    if payload.get("content_hash") != expected:
        raise DerivativeEvidenceWorkflowError(f"{label}_content_hash_mismatch")


def _ref_key(ref: EvidenceRef) -> str:
    return f"{ref.authority}:{ref.logical_id}:{ref.version}:{ref.content_hash}"
