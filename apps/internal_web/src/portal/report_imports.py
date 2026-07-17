"""Safe, bounded admission of historical CLI decision reports.

This module never executes or reproduces research.  It reads one explicitly
allowlisted immutable report, validates its evidence bindings, publishes a
content-addressed managed copy, and transactionally records catalog metadata
plus an audit outbox intent.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from market_research.application.adapter_contracts import (
    content_hash_payload,
    sha256_prefixed,
    validate_research_decision_report,
)
from market_research.storage_io import write_json_atomic_create_or_verify

from .audit import record_web_audit_event
from .models import ImportedDecisionReport
from .security import actor_snapshot, reject_symlink_components, validate_sha256
from .storage import SafeArtifactRef, make_artifact_ref


CODE_REVISION_PATTERN = re.compile(r"[0-9a-f]{7,64}\Z")
ALLOWED_VALIDATION_RESULTS = frozenset({"PASS", "FAIL", "INSUFFICIENT_EVIDENCE"})


class HistoricalReportImportConflict(ValidationError):
    def __init__(self) -> None:
        super().__init__("historical_report_import_binding_conflict")


@dataclass(frozen=True, slots=True)
class HistoricalReportImportResult:
    record: ImportedDecisionReport
    created: bool


def import_historical_decision_report(
    *,
    actor: Any,
    owner: Any,
    source_path: str,
    expected_report_hash: str,
    expected_manifest_hash: str,
    expected_experiment_id: str,
    expected_run_id: str,
    expected_dataset_snapshot_id: str,
    expected_dataset_content_hash: str,
    code_revision: str,
    visibility: str,
    correlation_id: str,
) -> HistoricalReportImportResult:
    """Verify and import one report without retaining its original path."""

    if not getattr(actor, "is_authenticated", False) or not actor.has_perm(
        "portal.import_research_report"
    ):
        raise PermissionDenied("historical_report_import_permission_required")
    if (
        not getattr(owner, "is_authenticated", False)
        or not getattr(owner, "is_active", False)
        or getattr(owner, "pk", None) is None
    ):
        raise ValidationError("historical_report_owner_invalid")
    if visibility not in ImportedDecisionReport.Visibility.values:
        raise ValidationError("historical_report_visibility_invalid")
    normalized_code_revision = str(code_revision).strip()
    if not CODE_REVISION_PATTERN.fullmatch(normalized_code_revision):
        raise ValidationError("historical_report_code_revision_invalid")
    normalized_correlation_id = str(correlation_id).strip()
    if not normalized_correlation_id or len(normalized_correlation_id) > 128:
        raise ValidationError("historical_report_correlation_id_invalid")

    expected = {
        "report_hash": validate_sha256(
            expected_report_hash,
            field="historical_report_expected_hash",
        ),
        "manifest_hash": validate_sha256(
            expected_manifest_hash,
            field="historical_report_expected_manifest_hash",
        ),
        "experiment_id": _required_text(
            expected_experiment_id,
            field="historical_report_expected_experiment_id",
        ),
        "run_id": _required_text(
            expected_run_id,
            field="historical_report_expected_run_id",
        ),
        "dataset_snapshot_id": _required_text(
            expected_dataset_snapshot_id,
            field="historical_report_expected_dataset_snapshot_id",
        ),
        "dataset_content_hash": validate_sha256(
            expected_dataset_content_hash,
            field="historical_report_expected_dataset_content_hash",
        ),
    }
    payload, source_size = _read_allowlisted_report(source_path)
    binding = _validate_report_binding(
        payload,
        expected=expected,
        code_revision=normalized_code_revision,
    )
    storage_ref = _publish_managed_report_copy(
        payload=payload,
        report_hash=binding["report_hash"],
    )
    values: dict[str, Any] = {
        **binding,
        "report_id": _opaque_report_id(binding["report_hash"]),
        "storage_ref": str(storage_ref),
        "source_size_bytes": source_size,
        "code_revision": normalized_code_revision,
        "owner_id": owner.pk,
        "visibility": visibility,
    }
    values["import_manifest_hash"] = _import_manifest_hash(values)
    actor_id, _roles, _permissions = actor_snapshot(actor)

    try:
        with transaction.atomic():
            record = (
                ImportedDecisionReport.objects.select_for_update()
                .filter(report_hash=binding["report_hash"])
                .first()
            )
            created = record is None
            if record is None:
                if ImportedDecisionReport.objects.filter(
                    experiment_id=binding["experiment_id"],
                    run_id=binding["run_id"],
                ).exists():
                    raise HistoricalReportImportConflict()
                record = ImportedDecisionReport.objects.create(
                    imported_by=actor,
                    **values,
                )
            elif not _record_matches(record, values):
                raise HistoricalReportImportConflict()
            _record_import_audit(
                record=record,
                actor_id=actor_id,
                correlation_id=normalized_correlation_id,
                created=created,
            )
            return HistoricalReportImportResult(record=record, created=created)
    except IntegrityError as exc:
        # A concurrent exact import may win either unique constraint after the
        # initial lookup. Resolve only a byte/evidence-identical winner.
        with transaction.atomic():
            record = (
                ImportedDecisionReport.objects.select_for_update()
                .filter(report_hash=binding["report_hash"])
                .first()
            )
            if record is None or not _record_matches(record, values):
                raise HistoricalReportImportConflict() from exc
            _record_import_audit(
                record=record,
                actor_id=actor_id,
                correlation_id=normalized_correlation_id,
                created=False,
            )
            return HistoricalReportImportResult(record=record, created=False)


def _read_allowlisted_report(source_path: str) -> tuple[dict[str, Any], int]:
    raw = str(source_path)
    if not raw or raw != raw.strip() or "\x00" in raw:
        raise ValidationError("historical_report_source_path_invalid")
    candidate = Path(raw)
    if (
        not candidate.is_absolute()
        or any(part in {".", ".."} for part in candidate.parts)
        or candidate.name in {"", ".", ".."}
    ):
        raise ValidationError("historical_report_source_path_invalid")

    matches: list[tuple[Path, Path]] = []
    for root in _validated_import_roots():
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if relative.parts:
            matches.append((root, relative))
    if not matches:
        raise ValidationError("historical_report_source_outside_allowlist")
    root, relative = max(matches, key=lambda item: len(item[0].parts))
    reject_symlink_components(candidate)
    content = _read_relative_no_follow(
        root=root,
        relative=relative,
        limit=int(settings.INTERNAL_WEB_MAX_RESULT_BYTES),
    )
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("historical_report_json_invalid") from exc
    if not isinstance(payload, dict):
        raise ValidationError("historical_report_must_be_object")
    return payload, len(content)


def _validated_import_roots() -> tuple[Path, ...]:
    configured = tuple(
        Path(value) for value in settings.INTERNAL_WEB_REPORT_IMPORT_ROOTS
    )
    if not configured:
        raise ValidationError("historical_report_import_roots_not_configured")
    roots: list[Path] = []
    for root in configured:
        if (
            not root.is_absolute()
            or any(part in {".", ".."} for part in root.parts)
            or root == Path(root.anchor)
        ):
            raise ValidationError("historical_report_import_root_invalid")
        reject_symlink_components(root)
        if not root.is_dir():
            raise ValidationError("historical_report_import_root_unavailable")
        if settings.RESEARCH_PATHS.is_within(
            root,
            settings.RESEARCH_PATHS.project_root,
        ):
            raise ValidationError("historical_report_import_root_in_repository")
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _read_relative_no_follow(
    *,
    root: Path,
    relative: Path,
    limit: int,
) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_flag is None:
        raise RuntimeError("historical_report_no_follow_unavailable")
    if limit <= 0:
        raise ValidationError("historical_report_read_limit_invalid")
    directory_flags = os.O_RDONLY | no_follow | directory_flag
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
    file_flags = os.O_RDONLY | no_follow
    if hasattr(os, "O_CLOEXEC"):
        file_flags |= os.O_CLOEXEC
    directory_fd = -1
    descriptor = -1
    try:
        expected_root = os.stat(root, follow_symlinks=False)
        directory_fd = os.open(root, directory_flags)
        opened_root = os.fstat(directory_fd)
        if not stat.S_ISDIR(expected_root.st_mode) or (
            expected_root.st_dev,
            expected_root.st_ino,
        ) != (opened_root.st_dev, opened_root.st_ino):
            raise ValidationError("historical_report_import_root_changed")
        for part in relative.parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(
            relative.parts[-1],
            file_flags,
            dir_fd=directory_fd,
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
            raise ValidationError("historical_report_source_not_regular_file")
        if metadata.st_size > limit:
            raise ValidationError("historical_report_too_large_to_verify")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > limit:
            raise ValidationError("historical_report_too_large_to_verify")
        final_metadata = os.fstat(descriptor)
        if (
            metadata.st_size != final_metadata.st_size
            or metadata.st_mtime_ns != final_metadata.st_mtime_ns
            or metadata.st_ctime_ns != final_metadata.st_ctime_ns
        ):
            raise ValidationError("historical_report_source_changed_during_read")
        return content
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError("historical_report_source_unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_fd >= 0:
            os.close(directory_fd)


def _validate_report_binding(
    report: dict[str, Any],
    *,
    expected: dict[str, str],
    code_revision: str,
) -> dict[str, str]:
    reasons = validate_research_decision_report(report)
    if reasons:
        raise ValidationError("historical_report_schema_or_hash_invalid")
    sections = report.get("sections")
    if not isinstance(sections, dict):
        raise ValidationError("historical_report_sections_invalid")
    conditions = sections.get("hypothesis_and_experiment_conditions")
    data_quality = sections.get("data_quality")
    conclusion = sections.get("research_conclusion")
    if not all(
        isinstance(value, dict) for value in (conditions, data_quality, conclusion)
    ):
        raise ValidationError("historical_report_evidence_sections_incomplete")

    report_hash = validate_sha256(
        str(report.get("content_hash") or ""),
        field="historical_report_hash",
    )
    manifest_hash = validate_sha256(
        str(report.get("manifest_hash") or ""),
        field="historical_report_manifest_hash",
    )
    validate_sha256(
        str(report.get("selection_report_hash") or ""),
        field="historical_report_selection_hash",
    )
    dataset_content_hash = validate_sha256(
        str(data_quality.get("dataset_content_hash") or ""),
        field="historical_report_dataset_content_hash",
    )
    validation_result = _required_text(
        report.get("validation_result"),
        field="historical_report_validation_result",
        maximum=32,
    )
    if validation_result not in ALLOWED_VALIDATION_RESULTS:
        raise ValidationError("historical_report_validation_result_invalid")
    if conclusion.get("validation_result") != validation_result:
        raise ValidationError("historical_report_conclusion_binding_mismatch")

    selected_candidate = report.get("selected_candidate_id")
    selected_candidate_id = (
        ""
        if selected_candidate is None
        else _required_text(
            selected_candidate,
            field="historical_report_selected_candidate_id",
        )
    )
    if validation_result == "PASS" and not selected_candidate_id:
        raise ValidationError("historical_report_pass_candidate_required")
    binding = {
        "report_hash": report_hash,
        "manifest_hash": manifest_hash,
        "experiment_id": _required_text(
            report.get("experiment_id"),
            field="historical_report_experiment_id",
        ),
        "run_id": _required_text(
            report.get("run_id"),
            field="historical_report_run_id",
        ),
        "validation_result": validation_result,
        "selected_candidate_id": selected_candidate_id,
        "market": _required_text(
            conditions.get("market"),
            field="historical_report_market",
        ),
        "interval": _required_text(
            conditions.get("interval"),
            field="historical_report_interval",
            maximum=64,
        ),
        "strategy_name": _required_text(
            conditions.get("strategy_name"),
            field="historical_report_strategy_name",
        ),
        "strategy_version": _required_text(
            conditions.get("strategy_version"),
            field="historical_report_strategy_version",
        ),
        "dataset_snapshot_id": _required_text(
            data_quality.get("dataset_snapshot_id"),
            field="historical_report_dataset_snapshot_id",
        ),
        "dataset_content_hash": dataset_content_hash,
    }
    for key, expected_value in expected.items():
        if binding[key] != expected_value:
            raise ValidationError("historical_report_expected_binding_mismatch")

    embedded_revisions = _embedded_code_revisions(report, conditions)
    if embedded_revisions and embedded_revisions != {code_revision}:
        raise ValidationError("historical_report_code_revision_binding_mismatch")
    return binding


def validate_managed_import_record(
    record: ImportedDecisionReport,
    payload: dict[str, Any],
) -> dict[str, str]:
    """Revalidate a catalog row against its managed report copy."""

    expected = {
        "report_hash": record.report_hash,
        "manifest_hash": record.manifest_hash,
        "experiment_id": record.experiment_id,
        "run_id": record.run_id,
        "dataset_snapshot_id": record.dataset_snapshot_id,
        "dataset_content_hash": record.dataset_content_hash,
    }
    binding = _validate_report_binding(
        payload,
        expected=expected,
        code_revision=record.code_revision,
    )
    values: dict[str, Any] = {
        **binding,
        "report_id": _opaque_report_id(binding["report_hash"]),
        "storage_ref": _managed_storage_ref(binding["report_hash"]),
        "code_revision": record.code_revision,
        "owner_id": record.owner_id,
        "visibility": record.visibility,
    }
    values["import_manifest_hash"] = _import_manifest_hash(values)
    if (
        record.report_id != values["report_id"]
        or record.storage_ref != values["storage_ref"]
        or record.import_manifest_hash != values["import_manifest_hash"]
        or not _record_matches(record, values)
    ):
        raise ValidationError("historical_report_catalog_binding_invalid")
    return binding


def _embedded_code_revisions(
    report: dict[str, Any],
    conditions: dict[str, Any],
) -> set[str]:
    candidates: list[Any] = [
        report.get("code_revision"),
        conditions.get("code_revision"),
    ]
    run_environment = conditions.get("run_environment")
    if isinstance(run_environment, dict):
        candidates.append(run_environment.get("code_revision"))
    return {
        str(value).strip()
        for value in candidates
        if value is not None and str(value).strip()
    }


def _publish_managed_report_copy(
    *,
    payload: dict[str, Any],
    report_hash: str,
) -> SafeArtifactRef:
    digest = validate_sha256(
        report_hash,
        field="historical_report_hash",
    ).removeprefix("sha256:")
    target = settings.RESEARCH_PATHS.report_path(
        "_internal_web",
        "imported_reports",
        digest[:2],
        f"{digest}.json",
    )
    reject_symlink_components(target.parent)
    try:
        write_json_atomic_create_or_verify(target, payload)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValidationError("historical_report_managed_copy_conflict") from exc
    reject_symlink_components(target)
    return make_artifact_ref("report", target)


def _managed_storage_ref(report_hash: str) -> str:
    digest = validate_sha256(
        report_hash,
        field="historical_report_hash",
    ).removeprefix("sha256:")
    return f"report:_internal_web/imported_reports/{digest[:2]}/{digest}.json"


def _import_manifest_hash(values: dict[str, Any]) -> str:
    material = {
        "schema_version": 1,
        "artifact_type": "historical_cli_decision_report_import",
        "report_id": values["report_id"],
        "report_hash": values["report_hash"],
        "storage_ref": values["storage_ref"],
        "manifest_hash": values["manifest_hash"],
        "experiment_id": values["experiment_id"],
        "run_id": values["run_id"],
        "validation_result": values["validation_result"],
        "selected_candidate_id": values["selected_candidate_id"],
        "market": values["market"],
        "interval": values["interval"],
        "strategy_name": values["strategy_name"],
        "strategy_version": values["strategy_version"],
        "dataset_snapshot_id": values["dataset_snapshot_id"],
        "dataset_content_hash": values["dataset_content_hash"],
        "code_revision": values["code_revision"],
        "owner_id": str(values["owner_id"]),
        "visibility": values["visibility"],
    }
    return sha256_prefixed(
        content_hash_payload(material),
        label="historical_cli_decision_report_import",
    )


def _record_matches(
    record: ImportedDecisionReport,
    values: dict[str, Any],
) -> bool:
    fields = (
        "report_id",
        "report_hash",
        "storage_ref",
        "import_manifest_hash",
        "manifest_hash",
        "experiment_id",
        "run_id",
        "validation_result",
        "selected_candidate_id",
        "market",
        "interval",
        "strategy_name",
        "strategy_version",
        "dataset_snapshot_id",
        "dataset_content_hash",
        "code_revision",
        "owner_id",
        "visibility",
    )
    return all(getattr(record, field) == values[field] for field in fields)


def _record_import_audit(
    *,
    record: ImportedDecisionReport,
    actor_id: str,
    correlation_id: str,
    created: bool,
) -> None:
    record_web_audit_event(
        action=(
            "historical_research_report_imported"
            if created
            else "historical_research_report_import_reused"
        ),
        actor_id=actor_id,
        object_type="imported_decision_report",
        object_id=str(record.pk),
        correlation_id=correlation_id,
        details={
            "report_id": record.report_id,
            "report_hash": record.report_hash,
            "import_manifest_hash": record.import_manifest_hash,
            "manifest_hash": record.manifest_hash,
            "experiment_id": record.experiment_id,
            "run_id": record.run_id,
            "dataset_snapshot_id": record.dataset_snapshot_id,
            "dataset_content_hash": record.dataset_content_hash,
            "code_revision": record.code_revision,
            "owner_id": str(record.owner_id),
            "visibility": record.visibility,
        },
    )


def _required_text(
    value: Any,
    *,
    field: str,
    maximum: int = 255,
) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ValidationError(f"{field}_invalid")
    return normalized


def _opaque_report_id(report_hash: str) -> str:
    return "report_" + validate_sha256(
        report_hash,
        field="historical_report_hash",
    ).removeprefix("sha256:")


__all__ = [
    "HistoricalReportImportConflict",
    "HistoricalReportImportResult",
    "import_historical_decision_report",
    "validate_managed_import_record",
]
