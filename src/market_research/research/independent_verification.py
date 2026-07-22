"""Immutable independent-verification authority for research reproduction.

Reproduction reports are diagnostic run outputs.  This module turns a
verifier's comparison into a separately role-bound, hash-bound authority
record.  Every result, including drift and execution failure, is preserved in
an append-only registry and in a create-or-verify snapshot.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from market_research.paths import ResearchPathManager
from market_research.storage_io import write_json_atomic_create_or_verify

from .artifact_store import ArtifactStore
from .hash_chain import (
    append_hash_chained_jsonl_idempotent,
    read_hash_chained_jsonl_snapshot,
)
from .hashing import content_hash_payload, sha256_prefixed
from .hashing import report_content_hash_payload
from .experiment_registry import (
    experiment_registry_path,
    validate_experiment_registry_binding,
)
from .final_selection import (
    compute_final_holdout_result_hash,
    validate_confirmation_artifact,
)
from .reproduction import (
    ReproductionContractError,
    compare_reproduction_fingerprints,
    load_reproduction_receipt,
    validate_reproduction_receipt_report_binding,
)


INDEPENDENT_VERIFICATION_SCHEMA_VERSION = 2
INDEPENDENT_VERIFICATION_HASH_LABEL = "independent_verification"
INDEPENDENT_VERIFIER_ROLE = "independent_verifier"
INDEPENDENT_REPRODUCTION_RESULT_HASH_LABEL = (
    "independent_verification_reproduction_result"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_STATUSES = frozenset({"PASS", "DRIFT", "FAILED"})
_SOURCE_REPORT_ARTIFACT_HASH_LABEL = "independent_verification_source_report_artifact"
_REPRODUCED_REPORT_ARTIFACT_HASH_LABEL = (
    "independent_verification_reproduced_report_artifact"
)
_REPRODUCED_TERMINAL_ARTIFACT_HASH_LABEL = (
    "independent_verification_reproduced_terminal_artifact"
)


class IndependentVerificationError(ValueError):
    """The independent-verification authority contract was violated."""


@dataclass(frozen=True, slots=True)
class IndependentVerificationRef:
    verification_id: str
    version: str
    content_hash: str

    def __post_init__(self) -> None:
        _require_identifier(self.verification_id, "verification_id")
        _require_identifier(self.version, "version")
        _require_hash(self.content_hash, "content_hash")

    def as_dict(self) -> dict[str, str]:
        return {
            "verification_id": self.verification_id,
            "version": self.version,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class IndependentVerificationResult:
    """One verifier-owned result for one immutable source research version."""

    verification_id: str
    version: str
    verifier_id: str
    verifier_role: str
    verified_at: str
    experiment_id: str
    research_version: str
    source_report_hash: str
    manifest_hash: str
    baseline_receipt_hash: str
    baseline_receipt_path: str
    reproduction_result_hash: str
    reproduction_result_path: str
    reproduced_receipt_hash: str | None
    reproduced_receipt_path: str | None
    code_binding_hash: str
    data_binding_hash: str
    environment_binding_hash: str
    expected_fingerprint_hash: str
    actual_fingerprint_hash: str | None
    status: str
    comparison_deltas: tuple[Mapping[str, Any], ...] = ()
    unresolved_issues: tuple[str, ...] = ()
    failure_code: str | None = None
    failure_evidence_hash: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.verification_id, "verification_id")
        _require_identifier(self.version, "version")
        _require_identifier(self.verifier_id, "verifier_id")
        _require_identifier(self.experiment_id, "experiment_id")
        if self.verifier_role != INDEPENDENT_VERIFIER_ROLE:
            raise IndependentVerificationError(
                "independent_verification_verifier_role_invalid"
            )
        verified_at = _require_timezone(self.verified_at)
        if verified_at.astimezone(timezone.utc) > datetime.now(timezone.utc):
            raise IndependentVerificationError(
                "independent_verification_verified_at_in_future"
            )
        if (
            not isinstance(self.research_version, str)
            or not self.research_version.strip()
        ):
            raise IndependentVerificationError(
                "independent_verification_research_version_required"
            )
        for name, value in (
            ("source_report_hash", self.source_report_hash),
            ("manifest_hash", self.manifest_hash),
            ("baseline_receipt_hash", self.baseline_receipt_hash),
            ("reproduction_result_hash", self.reproduction_result_hash),
            ("code_binding_hash", self.code_binding_hash),
            ("data_binding_hash", self.data_binding_hash),
            ("environment_binding_hash", self.environment_binding_hash),
            ("expected_fingerprint_hash", self.expected_fingerprint_hash),
        ):
            _require_hash(value, name)
        if self.actual_fingerprint_hash is not None:
            _require_hash(self.actual_fingerprint_hash, "actual_fingerprint_hash")
        if self.status not in _STATUSES:
            raise IndependentVerificationError(
                "independent_verification_status_invalid"
            )
        _require_absolute_path(self.baseline_receipt_path, "baseline_receipt_path")
        _require_absolute_path(
            self.reproduction_result_path,
            "reproduction_result_path",
        )
        if (self.reproduced_receipt_hash is None) != (
            self.reproduced_receipt_path is None
        ):
            raise IndependentVerificationError(
                "independent_verification_reproduced_receipt_ref_invalid"
            )
        if self.reproduced_receipt_hash is not None:
            _require_hash(self.reproduced_receipt_hash, "reproduced_receipt_hash")
            _require_absolute_path(
                self.reproduced_receipt_path,
                "reproduced_receipt_path",
            )
        if any(not isinstance(item, str) for item in self.unresolved_issues):
            raise IndependentVerificationError(
                "independent_verification_unresolved_issues_invalid"
            )
        normalized_issues = tuple(item.strip() for item in self.unresolved_issues)
        if (
            any(not item for item in normalized_issues)
            or len(set(normalized_issues)) != len(normalized_issues)
            or normalized_issues != self.unresolved_issues
        ):
            raise IndependentVerificationError(
                "independent_verification_unresolved_issues_invalid"
            )
        for delta in self.comparison_deltas:
            if not isinstance(delta, Mapping):
                raise IndependentVerificationError(
                    "independent_verification_comparison_delta_invalid"
                )
            if (
                not str(delta.get("path") or "").strip()
                or not str(delta.get("kind") or "").strip()
            ):
                raise IndependentVerificationError(
                    "independent_verification_comparison_delta_invalid"
                )
        object.__setattr__(
            self,
            "comparison_deltas",
            tuple(_deep_freeze(delta) for delta in self.comparison_deltas),
        )
        if self.status == "PASS":
            if (
                self.actual_fingerprint_hash != self.expected_fingerprint_hash
                or self.comparison_deltas
                or self.unresolved_issues
                or self.failure_code is not None
                or self.failure_evidence_hash is not None
                or self.reproduced_receipt_hash is None
            ):
                raise IndependentVerificationError(
                    "independent_verification_pass_contract_invalid"
                )
        elif self.status == "DRIFT":
            if (
                self.actual_fingerprint_hash is None
                or not self.comparison_deltas
                or not self.unresolved_issues
                or self.failure_code is not None
                or self.failure_evidence_hash is not None
                or self.reproduced_receipt_hash is None
            ):
                raise IndependentVerificationError(
                    "independent_verification_drift_contract_invalid"
                )
        else:
            if (
                not isinstance(self.failure_code, str)
                or not self.failure_code.strip()
                or self.failure_code != self.failure_code.strip()
                or self.failure_evidence_hash is None
                or not self.unresolved_issues
                or self.reproduced_receipt_hash is not None
            ):
                raise IndependentVerificationError(
                    "independent_verification_failure_contract_invalid"
                )
            _require_hash(self.failure_evidence_hash, "failure_evidence_hash")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": INDEPENDENT_VERIFICATION_SCHEMA_VERSION,
            "verification_id": self.verification_id,
            "version": self.version,
            "verifier_id": self.verifier_id,
            "verifier_role": self.verifier_role,
            "verified_at": self.verified_at,
            "experiment_id": self.experiment_id,
            "research_version": self.research_version,
            "source_report_hash": self.source_report_hash,
            "manifest_hash": self.manifest_hash,
            "baseline_receipt_hash": self.baseline_receipt_hash,
            "baseline_receipt_path": self.baseline_receipt_path,
            "reproduction_result_hash": self.reproduction_result_hash,
            "reproduction_result_path": self.reproduction_result_path,
            "reproduced_receipt_hash": self.reproduced_receipt_hash,
            "reproduced_receipt_path": self.reproduced_receipt_path,
            "code_binding_hash": self.code_binding_hash,
            "data_binding_hash": self.data_binding_hash,
            "environment_binding_hash": self.environment_binding_hash,
            "expected_fingerprint_hash": self.expected_fingerprint_hash,
            "actual_fingerprint_hash": self.actual_fingerprint_hash,
            "status": self.status,
            "comparison_deltas": [_deep_thaw(item) for item in self.comparison_deltas],
            "unresolved_issues": list(self.unresolved_issues),
            "failure_code": self.failure_code,
            "failure_evidence_hash": self.failure_evidence_hash,
        }

    def content_hash(self) -> str:
        return sha256_prefixed(
            content_hash_payload(self.as_dict()),
            label="independent_verification_result",
        )

    def ref(self) -> IndependentVerificationRef:
        return IndependentVerificationRef(
            verification_id=self.verification_id,
            version=self.version,
            content_hash=self.content_hash(),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "IndependentVerificationResult":
        expected = {
            "schema_version",
            "verification_id",
            "version",
            "verifier_id",
            "verifier_role",
            "verified_at",
            "experiment_id",
            "research_version",
            "source_report_hash",
            "manifest_hash",
            "baseline_receipt_hash",
            "baseline_receipt_path",
            "reproduction_result_hash",
            "reproduction_result_path",
            "reproduced_receipt_hash",
            "reproduced_receipt_path",
            "code_binding_hash",
            "data_binding_hash",
            "environment_binding_hash",
            "expected_fingerprint_hash",
            "actual_fingerprint_hash",
            "status",
            "comparison_deltas",
            "unresolved_issues",
            "failure_code",
            "failure_evidence_hash",
        }
        if (
            set(payload) != expected
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != INDEPENDENT_VERIFICATION_SCHEMA_VERSION
        ):
            raise IndependentVerificationError(
                "independent_verification_schema_invalid"
            )
        deltas = payload.get("comparison_deltas")
        issues = payload.get("unresolved_issues")
        if (
            not isinstance(deltas, list)
            or not all(isinstance(item, Mapping) for item in deltas)
            or not isinstance(issues, list)
            or not all(isinstance(item, str) for item in issues)
        ):
            raise IndependentVerificationError(
                "independent_verification_collections_invalid"
            )
        return cls(
            verification_id=_payload_string(payload, "verification_id"),
            version=_payload_string(payload, "version"),
            verifier_id=_payload_string(payload, "verifier_id"),
            verifier_role=_payload_string(payload, "verifier_role"),
            verified_at=_payload_string(payload, "verified_at"),
            experiment_id=_payload_string(payload, "experiment_id"),
            research_version=_payload_string(payload, "research_version"),
            source_report_hash=_payload_string(payload, "source_report_hash"),
            manifest_hash=_payload_string(payload, "manifest_hash"),
            baseline_receipt_hash=_payload_string(payload, "baseline_receipt_hash"),
            baseline_receipt_path=_payload_string(payload, "baseline_receipt_path"),
            reproduction_result_hash=_payload_string(
                payload, "reproduction_result_hash"
            ),
            reproduction_result_path=_payload_string(
                payload, "reproduction_result_path"
            ),
            reproduced_receipt_hash=(
                _payload_string(payload, "reproduced_receipt_hash")
                if payload["reproduced_receipt_hash"] is not None
                else None
            ),
            reproduced_receipt_path=(
                _payload_string(payload, "reproduced_receipt_path")
                if payload["reproduced_receipt_path"] is not None
                else None
            ),
            code_binding_hash=_payload_string(payload, "code_binding_hash"),
            data_binding_hash=_payload_string(payload, "data_binding_hash"),
            environment_binding_hash=_payload_string(
                payload, "environment_binding_hash"
            ),
            expected_fingerprint_hash=_payload_string(
                payload, "expected_fingerprint_hash"
            ),
            actual_fingerprint_hash=(
                _payload_string(payload, "actual_fingerprint_hash")
                if payload["actual_fingerprint_hash"] is not None
                else None
            ),
            status=_payload_string(payload, "status"),
            comparison_deltas=tuple(dict(item) for item in deltas),
            unresolved_issues=tuple(issues),
            failure_code=(
                _payload_string(payload, "failure_code")
                if payload["failure_code"] is not None
                else None
            ),
            failure_evidence_hash=(
                _payload_string(payload, "failure_evidence_hash")
                if payload["failure_evidence_hash"] is not None
                else None
            ),
        )


def independent_verification_registry_path(manager: ResearchPathManager) -> Path:
    return manager.artifact_path(
        "reports", "research", "_registry", "independent_verifications.jsonl"
    )


def independent_verification_result_path(
    manager: ResearchPathManager,
    ref: IndependentVerificationRef,
) -> Path:
    return manager.artifact_path(
        "reports",
        "research",
        "_registry",
        "independent_verifications",
        ref.verification_id,
        f"{ref.version}.json",
    )


def independent_reproduction_result_path(
    manager: ResearchPathManager,
    reproduction_result_hash: str,
) -> Path:
    _require_hash(reproduction_result_hash, "reproduction_result_hash")
    return manager.artifact_path(
        "reports",
        "research",
        "_registry",
        "independent_verification_evidence",
        "reproduction_results",
        f"{reproduction_result_hash.removeprefix('sha256:')}.json",
    )


def bind_reproduction_result_snapshot(
    *,
    manager: ResearchPathManager,
    payload: Mapping[str, Any],
) -> tuple[Path, str]:
    """Create-or-verify one immutable reproduction diagnostic snapshot."""

    material = dict(payload)
    if "independent_verification" in material:
        raise IndependentVerificationError(
            "independent_verification_result_contains_recursive_binding"
        )
    result_hash = sha256_prefixed(
        content_hash_payload(material),
        label=INDEPENDENT_REPRODUCTION_RESULT_HASH_LABEL,
    )
    path = independent_reproduction_result_path(manager, result_hash)
    try:
        write_json_atomic_create_or_verify(path, material)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise IndependentVerificationError(
            f"independent_verification_reproduction_snapshot_failed:{exc}"
        ) from exc
    return path.resolve(), result_hash


def independent_code_binding_hash(stable_fingerprint: Mapping[str, Any]) -> str:
    return sha256_prefixed(
        {
            "strategy_contract_hashes": stable_fingerprint.get(
                "strategy_contract_hashes"
            ),
            "execution_assumption_hashes": stable_fingerprint.get(
                "execution_assumption_hashes"
            ),
        },
        label="independent_verification_code_binding",
    )


def independent_reproduction_evidence(
    *,
    manager: ResearchPathManager,
    baseline_receipt_path: str | Path,
    reproduced_receipt_path: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve source and completed-run evidence from canonical artifacts.

    The returned values are declarations suitable for the immutable diagnostic
    snapshot.  Publication does not trust them: it resolves these paths and
    hashes again and requires an exact match.
    """

    baseline_path = Path(baseline_receipt_path).expanduser()
    try:
        baseline = load_reproduction_receipt(baseline_path)
    except ReproductionContractError as exc:
        raise IndependentVerificationError(
            f"independent_verification_baseline_receipt_invalid:{exc}"
        ) from exc
    stable = baseline.get("stable_fingerprint")
    if not isinstance(stable, dict):
        raise IndependentVerificationError(
            "independent_verification_baseline_fingerprint_missing"
        )
    experiment_id = str(baseline.get("experiment_id") or "")
    manifest_hash = str(baseline.get("manifest_hash") or "")
    report_kind = str(stable.get("report_kind") or "")
    if baseline.get("evidence_scope") == "validated_research_result":
        source_binding = baseline.get("source_evidence_binding")
        source_path_value = (
            source_binding.get("terminal_source_report_path")
            if isinstance(source_binding, dict)
            else None
        )
        if (
            not isinstance(source_path_value, str)
            or not source_path_value
            or not Path(source_path_value).is_absolute()
        ):
            raise IndependentVerificationError(
                "independent_verification_terminal_source_report_path_invalid"
            )
        source_path = Path(source_path_value).resolve()
        source = _load_bound_report(
            path=source_path,
            expected_content_hash=str(baseline.get("source_report_hash") or ""),
            expected_experiment_id=experiment_id,
            expected_manifest_hash=manifest_hash,
            expected_schema_version=3,
            expected_report_kind=None,
            expected_artifact_type="validated_research_result",
            artifact_hash_label=_SOURCE_REPORT_ARTIFACT_HASH_LABEL,
        )
    else:
        source_path = manager.report_path(
            "research", experiment_id, f"{report_kind}_report.json"
        ).resolve()
        source = _load_bound_report(
            path=source_path,
            expected_content_hash=str(baseline.get("source_report_hash") or ""),
            expected_experiment_id=experiment_id,
            expected_manifest_hash=manifest_hash,
            expected_schema_version=2,
            expected_report_kind=report_kind,
            expected_artifact_type=None,
            artifact_hash_label=_SOURCE_REPORT_ARTIFACT_HASH_LABEL,
        )
        try:
            validate_reproduction_receipt_report_binding(
                report=source["payload"],
                receipt=baseline,
            )
        except ReproductionContractError as exc:
            raise IndependentVerificationError(
                f"independent_verification_source_fingerprint_invalid:{exc}"
            ) from exc
    evidence: dict[str, Any] = {
        "source_report_path": str(source_path),
        "source_report_content_hash": source["content_hash"],
        "source_report_artifact_hash": source["artifact_hash"],
        "source_report_generated_at": source["generated_at"],
    }
    if reproduced_receipt_path is None:
        return evidence

    reproduced_path = Path(reproduced_receipt_path).expanduser()
    try:
        reproduced = load_reproduction_receipt(reproduced_path)
    except ReproductionContractError as exc:
        raise IndependentVerificationError(
            f"independent_verification_reproduced_receipt_invalid:{exc}"
        ) from exc
    prefix = str(baseline.get("receipt_content_hash") or "").removeprefix("sha256:")[
        :12
    ]
    reproduction_manager = _isolated_reproduction_manager(
        manager=manager,
        experiment_id=experiment_id,
        prefix=prefix,
    )
    expected_report_path = reproduction_manager.report_path(
        "research", experiment_id, f"{report_kind}_report.json"
    ).resolve()
    report = _load_bound_report(
        path=expected_report_path,
        expected_content_hash=str(reproduced.get("source_report_hash") or ""),
        expected_experiment_id=experiment_id,
        expected_manifest_hash=manifest_hash,
        expected_schema_version=2,
        expected_report_kind=report_kind,
        expected_artifact_type=None,
        artifact_hash_label=_REPRODUCED_REPORT_ARTIFACT_HASH_LABEL,
    )
    try:
        validate_reproduction_receipt_report_binding(
            report=report["payload"],
            receipt=reproduced,
        )
    except ReproductionContractError as exc:
        raise IndependentVerificationError(
            f"independent_verification_reproduced_fingerprint_invalid:{exc}"
        ) from exc
    artifact_paths = report["payload"].get("artifact_paths")
    if (
        not isinstance(artifact_paths, dict)
        or Path(str(artifact_paths.get("report_path") or "")).resolve()
        != expected_report_path
    ):
        raise IndependentVerificationError(
            "independent_verification_reproduced_report_path_mismatch"
        )
    evidence.update(
        {
            "reproduced_report_path": str(expected_report_path),
            "reproduced_report_content_hash": report["content_hash"],
            "reproduced_report_artifact_hash": report["artifact_hash"],
            "reproduced_report_generated_at": report["generated_at"],
            "reproduction_completion_authority_path": str(expected_report_path),
            "reproduction_completion_authority_hash": report["artifact_hash"],
            "reproduction_completed_at": report["generated_at"],
        }
    )
    if baseline.get("evidence_scope") == "validated_research_result":
        confirmation_path = (
            expected_report_path.parent / "final_holdout_confirmation.json"
        )
        confirmation = _load_reproduced_terminal_confirmation(
            path=confirmation_path,
            expected_manifest_hash=manifest_hash,
            selection_artifact=report["payload"].get("selection_artifact"),
            expected_registry_path=experiment_registry_path(
                manager=reproduction_manager
            ),
        )
        evidence.update(
            {
                "reproduced_final_holdout_confirmation_path": str(confirmation_path),
                "reproduced_final_holdout_confirmation_hash": confirmation[
                    "content_hash"
                ],
                "reproduced_final_holdout_confirmation_artifact_hash": confirmation[
                    "artifact_hash"
                ],
                "reproduced_final_holdout_result_hash": confirmation["result_hash"],
                "reproduced_final_holdout_generated_at": confirmation["generated_at"],
                "reproduction_completion_authority_path": str(confirmation_path),
                "reproduction_completion_authority_hash": confirmation["artifact_hash"],
                "reproduction_completed_at": confirmation["generated_at"],
            }
        )
    return evidence


def find_independent_verification(
    *,
    manager: ResearchPathManager,
    verification_id: str,
    version: str,
) -> IndependentVerificationResult | None:
    """Resolve one existing logical identity for deterministic CLI retries."""

    _require_identifier(verification_id, "verification_id")
    _require_identifier(version, "version")
    path = independent_verification_registry_path(manager)
    if not path.exists():
        return None
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path,
            label=INDEPENDENT_VERIFICATION_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise IndependentVerificationError(
            "independent_verification_registry_invalid"
        ) from exc
    if snapshot.as_validation()["status"] != "PASS":
        raise IndependentVerificationError("independent_verification_registry_invalid")
    rows = [
        row
        for row in snapshot.rows
        if row.get("logical_id") == verification_id and row.get("version") == version
    ]
    if not rows:
        return None
    if len(rows) != 1:
        raise IndependentVerificationError(
            "independent_verification_identity_not_unique"
        )
    return load_independent_verification(
        manager=manager,
        ref=IndependentVerificationRef(
            verification_id=verification_id,
            version=version,
            content_hash=str(rows[0].get("record_hash") or ""),
        ),
    )


def publish_independent_verification(
    *,
    manager: ResearchPathManager,
    result: IndependentVerificationResult,
) -> dict[str, Any]:
    """Create-or-verify the result and append its identity exactly once."""

    ref = result.ref()
    _validate_reproduction_evidence(manager=manager, result=result)
    artifact = {**result.as_dict(), "content_hash": ref.content_hash}
    path = independent_verification_result_path(manager, ref)
    try:
        write_json_atomic_create_or_verify(path, artifact)
        return append_hash_chained_jsonl_idempotent(
            store=ArtifactStore(root=manager.artifact_root),
            path=independent_verification_registry_path(manager),
            payload={
                "event_id": (
                    f"independent-verification:{ref.verification_id}:{ref.version}"
                ),
                "record_type": "INDEPENDENT_VERIFICATION_RESULT",
                "logical_id": ref.verification_id,
                "version": ref.version,
                "record_hash": ref.content_hash,
                "artifact_path": str(path.resolve()),
                "payload": result.as_dict(),
            },
            label=INDEPENDENT_VERIFICATION_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise IndependentVerificationError(
            f"independent_verification_publication_failed:{exc}"
        ) from exc


def load_independent_verification(
    *,
    manager: ResearchPathManager,
    ref: IndependentVerificationRef,
) -> IndependentVerificationResult:
    """Resolve a ref only from the validated canonical append-only authority."""

    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=independent_verification_registry_path(manager),
            label=INDEPENDENT_VERIFICATION_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise IndependentVerificationError(
            "independent_verification_registry_invalid"
        ) from exc
    if snapshot.as_validation()["status"] != "PASS":
        raise IndependentVerificationError("independent_verification_registry_invalid")
    rows = [
        row
        for row in snapshot.rows
        if row.get("logical_id") == ref.verification_id
        and row.get("version") == ref.version
    ]
    if len(rows) != 1:
        raise IndependentVerificationError(
            "independent_verification_reference_not_found"
        )
    row = rows[0]
    expected_event_id = f"independent-verification:{ref.verification_id}:{ref.version}"
    if (
        row.get("event_id") != expected_event_id
        or row.get("record_type") != "INDEPENDENT_VERIFICATION_RESULT"
        or row.get("logical_id") != ref.verification_id
        or row.get("version") != ref.version
    ):
        raise IndependentVerificationError(
            "independent_verification_row_identity_mismatch"
        )
    if row.get("record_hash") != ref.content_hash:
        raise IndependentVerificationError(
            "independent_verification_reference_hash_mismatch"
        )
    payload = row.get("payload")
    if not isinstance(payload, dict):
        raise IndependentVerificationError("independent_verification_payload_invalid")
    result = IndependentVerificationResult.from_dict(payload)
    if result.verification_id != ref.verification_id or result.version != ref.version:
        raise IndependentVerificationError(
            "independent_verification_row_identity_mismatch"
        )
    if result.content_hash() != ref.content_hash:
        raise IndependentVerificationError(
            "independent_verification_content_hash_mismatch"
        )
    expected_path = independent_verification_result_path(manager, ref).resolve()
    if Path(str(row.get("artifact_path") or "")).resolve() != expected_path:
        raise IndependentVerificationError(
            "independent_verification_artifact_path_mismatch"
        )
    try:
        artifact = json.loads(expected_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndependentVerificationError(
            "independent_verification_artifact_invalid"
        ) from exc
    if artifact != {**result.as_dict(), "content_hash": ref.content_hash}:
        raise IndependentVerificationError("independent_verification_artifact_mismatch")
    _validate_reproduction_evidence(manager=manager, result=result)
    return result


def validate_independent_verification_registry(
    manager: ResearchPathManager,
) -> dict[str, Any]:
    """Validate the hash chain and every canonical immutable result snapshot."""

    path = independent_verification_registry_path(manager)
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path,
            label=INDEPENDENT_VERIFICATION_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "reasons": [
                f"independent_verification_registry_invalid:{type(exc).__name__}"
            ],
            "row_count": 0,
            "stream_hash": None,
            "path": str(path.resolve()),
        }
    chain = snapshot.as_validation()
    reasons = [str(item) for item in chain["reasons"]]
    if chain["status"] == "PASS":
        for row in snapshot.rows:
            sequence = row.get("sequence")
            try:
                ref = IndependentVerificationRef(
                    verification_id=str(row.get("logical_id") or ""),
                    version=str(row.get("version") or ""),
                    content_hash=str(row.get("record_hash") or ""),
                )
                if row.get("record_type") != "INDEPENDENT_VERIFICATION_RESULT":
                    raise IndependentVerificationError(
                        "independent_verification_record_type_invalid"
                    )
                load_independent_verification(manager=manager, ref=ref)
            except IndependentVerificationError as exc:
                reasons.append(f"row_{sequence}:{exc}")
    return {
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "row_count": chain["row_count"],
        "stream_hash": chain["stream_hash"],
        "path": str(path.resolve()),
    }


def _validate_reproduction_evidence(
    *,
    manager: ResearchPathManager,
    result: IndependentVerificationResult,
) -> None:
    expected_baseline_paths = {
        manager.report_path(
            "research",
            result.experiment_id,
            "reproduction_receipt.json",
        ).resolve(),
        manager.report_path(
            "research",
            result.experiment_id,
            "validated_research_reproduction_receipt.json",
        ).resolve(),
    }
    baseline_path = Path(result.baseline_receipt_path)
    expected_baseline_path = baseline_path.resolve()
    if (
        baseline_path.is_symlink()
        or expected_baseline_path not in expected_baseline_paths
    ):
        raise IndependentVerificationError(
            "independent_verification_baseline_receipt_path_mismatch"
        )
    try:
        baseline = load_reproduction_receipt(baseline_path)
    except ReproductionContractError as exc:
        raise IndependentVerificationError(
            f"independent_verification_baseline_receipt_invalid:{exc}"
        ) from exc
    stable = baseline.get("stable_fingerprint")
    if not isinstance(stable, dict):
        raise IndependentVerificationError(
            "independent_verification_baseline_fingerprint_missing"
        )
    if (
        baseline.get("receipt_content_hash") != result.baseline_receipt_hash
        or baseline.get("experiment_id") != result.experiment_id
        or baseline.get("manifest_hash") != result.manifest_hash
        or baseline.get("source_report_hash") != result.source_report_hash
        or result.research_version != result.manifest_hash
        or stable.get("stable_fingerprint_hash") != result.expected_fingerprint_hash
        or stable.get("dataset_fingerprint") != result.data_binding_hash
        or stable.get("strict_environment_hash") != result.environment_binding_hash
        or independent_code_binding_hash(stable) != result.code_binding_hash
    ):
        raise IndependentVerificationError(
            "independent_verification_baseline_binding_mismatch"
        )
    if baseline.get("evidence_scope") == "validated_research_result":
        _validate_terminal_baseline_binding(
            manager=manager,
            result=result,
            baseline=baseline,
            stable=stable,
        )

    expected_evidence = independent_reproduction_evidence(
        manager=manager,
        baseline_receipt_path=baseline_path,
        reproduced_receipt_path=(
            result.reproduced_receipt_path
            if result.status in {"PASS", "DRIFT"}
            else None
        ),
    )

    expected_result_path = independent_reproduction_result_path(
        manager,
        result.reproduction_result_hash,
    ).resolve()
    result_path = Path(result.reproduction_result_path)
    if result_path.is_symlink() or result_path.resolve() != expected_result_path:
        raise IndependentVerificationError(
            "independent_verification_reproduction_result_path_mismatch"
        )
    try:
        reproduction_payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndependentVerificationError(
            "independent_verification_reproduction_result_invalid"
        ) from exc
    if not isinstance(reproduction_payload, dict):
        raise IndependentVerificationError(
            "independent_verification_reproduction_result_invalid"
        )
    actual_result_hash = sha256_prefixed(
        content_hash_payload(reproduction_payload),
        label=INDEPENDENT_REPRODUCTION_RESULT_HASH_LABEL,
    )
    if actual_result_hash != result.reproduction_result_hash:
        raise IndependentVerificationError(
            "independent_verification_reproduction_result_hash_mismatch"
        )
    if (
        reproduction_payload.get("schema_version") != 1
        or reproduction_payload.get("experiment_id") != result.experiment_id
        or reproduction_payload.get("manifest_hash") != result.manifest_hash
        or Path(str(reproduction_payload.get("baseline_receipt_path") or "")).resolve()
        != expected_baseline_path
        or reproduction_payload.get("baseline_receipt_hash")
        != result.baseline_receipt_hash
    ):
        raise IndependentVerificationError(
            "independent_verification_reproduction_result_binding_mismatch"
        )
    if any(
        reproduction_payload.get(key) != value
        for key, value in expected_evidence.items()
    ):
        raise IndependentVerificationError(
            "independent_verification_reproduction_authority_mismatch"
        )
    verified_at = _require_timezone(result.verified_at).astimezone(timezone.utc)
    source_generated_at = _require_timestamp(
        str(expected_evidence["source_report_generated_at"]),
        "source_report_generated_at",
    ).astimezone(timezone.utc)
    if verified_at < source_generated_at:
        raise IndependentVerificationError(
            "independent_verification_verified_before_source_completion"
        )

    if result.status in {"PASS", "DRIFT"}:
        reproduced_at = _require_timestamp(
            str(expected_evidence["reproduction_completed_at"]),
            "reproduction_completed_at",
        ).astimezone(timezone.utc)
        if verified_at < reproduced_at:
            raise IndependentVerificationError(
                "independent_verification_verified_before_reproduction_completion"
            )
        _validate_completed_reproduction_evidence(
            manager=manager,
            result=result,
            baseline=baseline,
            reproduction_payload=reproduction_payload,
        )
        return
    if (
        reproduction_payload.get("status") != "REPRODUCTION_FAILED"
        or reproduction_payload.get("error_code") != result.failure_code
    ):
        raise IndependentVerificationError(
            "independent_verification_failure_result_mismatch"
        )
    failure_hash = sha256_prefixed(
        {
            "phase": reproduction_payload.get("phase"),
            "error_code": result.failure_code,
            "error": reproduction_payload.get("error"),
        },
        label="independent_verification_failure_evidence",
    )
    if failure_hash != result.failure_evidence_hash:
        raise IndependentVerificationError(
            "independent_verification_failure_evidence_mismatch"
        )


def _validate_completed_reproduction_evidence(
    *,
    manager: ResearchPathManager,
    result: IndependentVerificationResult,
    baseline: Mapping[str, Any],
    reproduction_payload: Mapping[str, Any],
) -> None:
    if result.reproduced_receipt_path is None or result.reproduced_receipt_hash is None:
        raise IndependentVerificationError(
            "independent_verification_reproduced_receipt_required"
        )
    prefix = result.baseline_receipt_hash.removeprefix("sha256:")[:12]
    expected_reproduced_path = manager.report_path(
        "reproductions",
        result.experiment_id,
        prefix,
        "research",
        result.experiment_id,
        "reproduction_receipt.json",
    ).resolve()
    reproduced_path = Path(result.reproduced_receipt_path)
    if (
        reproduced_path.is_symlink()
        or reproduced_path.resolve() != expected_reproduced_path
        or Path(
            str(reproduction_payload.get("reproduced_receipt_path") or "")
        ).resolve()
        != expected_reproduced_path
    ):
        raise IndependentVerificationError(
            "independent_verification_reproduced_receipt_path_mismatch"
        )
    try:
        reproduced = load_reproduction_receipt(reproduced_path)
    except ReproductionContractError as exc:
        raise IndependentVerificationError(
            f"independent_verification_reproduced_receipt_invalid:{exc}"
        ) from exc
    expected_stable = baseline.get("stable_fingerprint")
    actual_stable = reproduced.get("stable_fingerprint")
    if not isinstance(expected_stable, dict) or not isinstance(actual_stable, dict):
        raise IndependentVerificationError(
            "independent_verification_reproduced_fingerprint_missing"
        )
    comparison = compare_reproduction_fingerprints(expected_stable, actual_stable)
    expected_deltas = [dict(item) for item in comparison.mismatches]
    if baseline.get("evidence_scope") == "validated_research_result":
        reproduction_manager = _isolated_reproduction_manager(
            manager=manager,
            experiment_id=result.experiment_id,
            prefix=prefix,
        )
        report_kind = str(expected_stable.get("report_kind") or "")
        reproduced_report_path = reproduction_manager.report_path(
            "research",
            result.experiment_id,
            f"{report_kind}_report.json",
        ).resolve()
        reproduced_report = _load_bound_report(
            path=reproduced_report_path,
            expected_content_hash=str(reproduced.get("source_report_hash") or ""),
            expected_experiment_id=result.experiment_id,
            expected_manifest_hash=result.manifest_hash,
            expected_schema_version=2,
            expected_report_kind=report_kind,
            expected_artifact_type=None,
            artifact_hash_label=_REPRODUCED_REPORT_ARTIFACT_HASH_LABEL,
        )
        try:
            validate_reproduction_receipt_report_binding(
                report=reproduced_report["payload"],
                receipt=reproduced,
            )
        except ReproductionContractError as exc:
            raise IndependentVerificationError(
                f"independent_verification_reproduced_fingerprint_invalid:{exc}"
            ) from exc
        confirmation_path = reproduction_manager.report_path(
            "research",
            result.experiment_id,
            "final_holdout_confirmation.json",
        ).resolve()
        terminal = _load_reproduced_terminal_confirmation(
            path=confirmation_path,
            expected_manifest_hash=result.manifest_hash,
            selection_artifact=reproduced_report["payload"].get("selection_artifact"),
            expected_registry_path=experiment_registry_path(
                manager=reproduction_manager
            ),
        )
        expected_deltas.extend(
            _terminal_holdout_comparison_deltas(
                baseline=baseline,
                confirmation=terminal["payload"],
            )
        )
    expected_status = "PASS" if not expected_deltas else "DRIFT"
    if (
        reproduction_payload.get("phase") != "fingerprint_comparison"
        or reproduction_payload.get("error_code") is not None
        or reproduction_payload.get("error") is not None
        or reproduced.get("receipt_content_hash") != result.reproduced_receipt_hash
        or reproduced.get("experiment_id") != result.experiment_id
        or reproduced.get("manifest_hash") != result.manifest_hash
        or expected_status != result.status
        or comparison.expected_fingerprint_hash != result.expected_fingerprint_hash
        or comparison.actual_fingerprint_hash != result.actual_fingerprint_hash
        or expected_deltas != reproduction_payload.get("mismatches")
        or reproduction_payload.get("status") != result.status
        or reproduction_payload.get("expected_fingerprint_hash")
        != result.expected_fingerprint_hash
        or reproduction_payload.get("actual_fingerprint_hash")
        != result.actual_fingerprint_hash
        or [_deep_thaw(item) for item in result.comparison_deltas]
        != reproduction_payload.get("mismatches")
    ):
        raise IndependentVerificationError(
            "independent_verification_completed_result_mismatch"
        )


def _validate_terminal_baseline_binding(
    *,
    manager: ResearchPathManager,
    result: IndependentVerificationResult,
    baseline: Mapping[str, Any],
    stable: Mapping[str, Any],
) -> None:
    binding = baseline.get("source_evidence_binding")
    if baseline.get("evidence_scope") != "validated_research_result" or not isinstance(
        binding, dict
    ):
        raise IndependentVerificationError(
            "independent_verification_terminal_binding_missing"
        )
    binding_material = {
        key: value for key, value in binding.items() if key != "content_hash"
    }
    if (
        binding.get("schema_version") != 1
        or binding.get("artifact_type") != "validated_research_reproduction_binding"
        or binding.get("content_hash")
        != sha256_prefixed(
            binding_material,
            label="validated_research_reproduction_binding",
        )
        or binding.get("terminal_source_report_hash") != result.source_report_hash
        or not isinstance(binding.get("terminal_source_report_path"), str)
        or not Path(str(binding["terminal_source_report_path"])).is_absolute()
        or binding.get("manifest_hash") != result.manifest_hash
    ):
        raise IndependentVerificationError(
            "independent_verification_terminal_binding_invalid"
        )
    selection_receipt_path = manager.report_path(
        "research",
        result.experiment_id,
        "reproduction_receipt.json",
    ).resolve()
    try:
        selection_receipt = load_reproduction_receipt(selection_receipt_path)
    except ReproductionContractError as exc:
        raise IndependentVerificationError(
            "independent_verification_selection_receipt_invalid"
        ) from exc
    if (
        selection_receipt.get("receipt_content_hash")
        != binding.get("selection_reproduction_receipt_hash")
        or selection_receipt.get("source_report_hash")
        != binding.get("selection_report_hash")
        or selection_receipt.get("experiment_id") != result.experiment_id
        or selection_receipt.get("manifest_hash") != result.manifest_hash
        or selection_receipt.get("stable_fingerprint") != stable
    ):
        raise IndependentVerificationError(
            "independent_verification_selection_receipt_mismatch"
        )
    report_kind = str(stable.get("report_kind") or "")
    selection_report_path = manager.report_path(
        "research",
        result.experiment_id,
        f"{report_kind}_report.json",
    ).resolve()
    selection_report = _load_bound_report(
        path=selection_report_path,
        expected_content_hash=str(selection_receipt.get("source_report_hash") or ""),
        expected_experiment_id=result.experiment_id,
        expected_manifest_hash=result.manifest_hash,
        expected_schema_version=2,
        expected_report_kind=report_kind,
        expected_artifact_type=None,
        artifact_hash_label=_SOURCE_REPORT_ARTIFACT_HASH_LABEL,
    )
    try:
        validate_reproduction_receipt_report_binding(
            report=selection_report["payload"],
            receipt=selection_receipt,
        )
    except ReproductionContractError as exc:
        raise IndependentVerificationError(
            f"independent_verification_selection_fingerprint_invalid:{exc}"
        ) from exc
    selection_artifact_paths = selection_report["payload"].get("artifact_paths")
    if (
        not isinstance(selection_artifact_paths, dict)
        or Path(str(selection_artifact_paths.get("report_path") or "")).resolve()
        != selection_report_path
    ):
        raise IndependentVerificationError(
            "independent_verification_selection_report_path_mismatch"
        )
    confirmation_path = manager.report_path(
        "research",
        result.experiment_id,
        "final_holdout_confirmation.json",
    )
    try:
        confirmation = json.loads(confirmation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndependentVerificationError(
            "independent_verification_holdout_confirmation_invalid"
        ) from exc
    if not isinstance(confirmation, dict):
        raise IndependentVerificationError(
            "independent_verification_holdout_confirmation_invalid"
        )
    confirmation_material = {
        key: value
        for key, value in confirmation.items()
        if key not in {"content_hash", "confirmation_artifact_path"}
    }
    if (
        confirmation.get("content_hash")
        != binding.get("final_holdout_confirmation_hash")
        or confirmation.get("content_hash")
        != sha256_prefixed(
            confirmation_material,
            label="final_holdout_confirmation",
        )
        or confirmation.get("final_holdout_result_hash")
        != binding.get("final_holdout_result_hash")
        or confirmation.get("final_holdout_result_hash")
        != compute_final_holdout_result_hash(confirmation)
        or confirmation.get("selection_artifact_hash")
        != binding.get("selection_artifact_hash")
        or any(
            confirmation.get(field) != binding.get(field)
            for field in (
                "final_holdout_query_hash",
                "final_holdout_data_hash",
                "final_holdout_fingerprint_hash",
                "final_holdout_quality_hash",
            )
        )
    ):
        raise IndependentVerificationError(
            "independent_verification_holdout_confirmation_mismatch"
        )
    confirmation_reasons = validate_confirmation_artifact(
        confirmation,
        selection_artifact=selection_report["payload"].get("selection_artifact"),
    )
    registry_reasons = validate_experiment_registry_binding(
        report=confirmation,
        require_complete=True,
        expected_registry_path=experiment_registry_path(manager=manager),
    )
    if confirmation_reasons or registry_reasons:
        raise IndependentVerificationError(
            "independent_verification_holdout_confirmation_invalid:"
            + ",".join(sorted({*confirmation_reasons, *registry_reasons}))
        )


def _load_bound_report(
    *,
    path: Path,
    expected_content_hash: str,
    expected_experiment_id: str,
    expected_manifest_hash: str,
    expected_schema_version: int,
    expected_report_kind: str | None,
    expected_artifact_type: str | None,
    artifact_hash_label: str,
) -> dict[str, Any]:
    if path.is_symlink():
        raise IndependentVerificationError(
            "independent_verification_report_path_mismatch"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndependentVerificationError(
            "independent_verification_report_invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise IndependentVerificationError("independent_verification_report_invalid")
    computed_content_hash = sha256_prefixed(report_content_hash_payload(payload))
    if (
        payload.get("schema_version") != expected_schema_version
        or payload.get("experiment_id") != expected_experiment_id
        or payload.get("manifest_hash") != expected_manifest_hash
        or payload.get("content_hash") != expected_content_hash
        or computed_content_hash != expected_content_hash
        or (
            expected_report_kind is not None
            and payload.get("report_kind") != expected_report_kind
        )
        or (
            expected_artifact_type is not None
            and payload.get("artifact_type") != expected_artifact_type
        )
    ):
        raise IndependentVerificationError(
            "independent_verification_report_binding_mismatch"
        )
    generated_at_value = payload.get("generated_at")
    if not isinstance(generated_at_value, str):
        raise IndependentVerificationError(
            "independent_verification_report_generated_at_invalid"
        )
    _require_timestamp(generated_at_value, "report_generated_at")
    return {
        "payload": payload,
        "content_hash": expected_content_hash,
        "artifact_hash": sha256_prefixed(payload, label=artifact_hash_label),
        "generated_at": generated_at_value,
    }


def _load_reproduced_terminal_confirmation(
    *,
    path: Path,
    expected_manifest_hash: str,
    selection_artifact: object,
    expected_registry_path: Path,
) -> dict[str, Any]:
    if path.is_symlink():
        raise IndependentVerificationError(
            "independent_verification_terminal_reproduction_path_mismatch"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndependentVerificationError(
            "independent_verification_terminal_reproduction_invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise IndependentVerificationError(
            "independent_verification_terminal_reproduction_invalid"
        )
    generated_at = payload.get("generated_at")
    result_hash = payload.get("final_holdout_result_hash")
    reasons = validate_confirmation_artifact(
        payload,
        selection_artifact=(
            dict(selection_artifact) if isinstance(selection_artifact, Mapping) else {}
        ),
    ) + validate_experiment_registry_binding(
        report=payload,
        require_complete=True,
        expected_registry_path=expected_registry_path,
    )
    if (
        payload.get("manifest_hash") != expected_manifest_hash
        or reasons
        or not isinstance(result_hash, str)
        or _SHA256.fullmatch(result_hash) is None
        or not isinstance(generated_at, str)
    ):
        raise IndependentVerificationError(
            "independent_verification_terminal_reproduction_binding_mismatch"
        )
    _require_timestamp(generated_at, "terminal_reproduction_generated_at")
    return {
        "payload": payload,
        "content_hash": payload["content_hash"],
        "artifact_hash": sha256_prefixed(
            payload,
            label=_REPRODUCED_TERMINAL_ARTIFACT_HASH_LABEL,
        ),
        "result_hash": result_hash,
        "generated_at": generated_at,
    }


def _isolated_reproduction_manager(
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


def _terminal_holdout_comparison_deltas(
    *,
    baseline: Mapping[str, Any],
    confirmation: Mapping[str, Any],
) -> list[dict[str, object]]:
    binding = baseline.get("source_evidence_binding")
    if not isinstance(binding, dict):
        raise IndependentVerificationError(
            "independent_verification_terminal_binding_missing"
        )
    fields = (
        "selection_artifact_hash",
        "final_holdout_result_hash",
        "final_holdout_query_hash",
        "final_holdout_data_hash",
        "final_holdout_fingerprint_hash",
        "final_holdout_quality_hash",
    )
    deltas: list[dict[str, object]] = []
    for field in fields:
        expected = binding.get(field)
        actual = confirmation.get(field)
        if (
            not isinstance(expected, str)
            or _SHA256.fullmatch(expected) is None
            or not isinstance(actual, str)
            or _SHA256.fullmatch(actual) is None
        ):
            raise IndependentVerificationError(
                "independent_verification_terminal_comparison_binding_invalid"
            )
        if expected != actual:
            deltas.append(
                {
                    "path": f"terminal_holdout.{field}",
                    "expected": expected,
                    "actual": actual,
                    "kind": "value_mismatch",
                }
            )
    return deltas


def _require_identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise IndependentVerificationError(f"independent_verification_{name}_invalid")


def _payload_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise IndependentVerificationError(f"independent_verification_{key}_invalid")
    return value


def _require_hash(value: str, name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise IndependentVerificationError(f"independent_verification_{name}_invalid")


def _require_absolute_path(value: str | None, name: str) -> None:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise IndependentVerificationError(f"independent_verification_{name}_invalid")


def _require_timezone(value: str) -> datetime:
    if not isinstance(value, str):
        raise IndependentVerificationError(
            "independent_verification_verified_at_invalid"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise IndependentVerificationError(
            "independent_verification_verified_at_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IndependentVerificationError(
            "independent_verification_verified_at_timezone_required"
        )
    return parsed


def _require_timestamp(value: str, name: str) -> datetime:
    if not isinstance(value, str):
        raise IndependentVerificationError(f"independent_verification_{name}_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise IndependentVerificationError(
            f"independent_verification_{name}_invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IndependentVerificationError(
            f"independent_verification_{name}_timezone_required"
        )
    return parsed


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    return value
