"""Published read-only queries for research knowledge and final evidence.

The internal web adapter consumes this module instead of reimplementing
research rules.  Every query first relies on the owning registry's validation
contract, returns stable identifiers and hashes, and projects no filesystem
paths, secrets, or raw final-holdout values.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from collections.abc import Sequence
from typing import Any, Mapping, cast

from market_research.paths import ResearchPathManager

from .hash_chain import read_hash_chained_jsonl_snapshot
from .knowledge_contract import KnowledgeRef
from .knowledge_registry import (
    get_knowledge_record,
    knowledge_registry_path,
    query_inbound_refs,
    query_outbound_refs,
    validate_knowledge_registry,
)
from .prospective_validation import (
    PROSPECTIVE_VALIDATION_HASH_LABEL,
    prospective_registry_path,
    research_conclusion_registry_path,
    validate_prospective_registry,
)
from .research_package_registry import (
    diff_registered_research_packages,
    get_research_package,
    research_package_lineage,
    search_research_packages,
)
from .validation_decision import query_validation_decisions


EXPLORATION_QUERY_SCHEMA_VERSION = 1
_LINEAGE_TYPES = frozenset({"observation", "research_question", "hypothesis"})
_DETAIL_LEVELS = frozenset({"summary", "technical"})
_SENSITIVE_KEY_FRAGMENTS = (
    "secret",
    "password",
    "credential",
    "token",
    "cookie",
    "authorization",
    "api_key",
    "apikey",
    "private_key",
)
_PATH_KEY_NAMES = frozenset(
    {
        "directory",
        "file_path",
        "filepath",
        "filename",
        "locator",
        "path",
        "paths",
        "uri",
    }
)
_PATH_KEY_SUFFIXES = (
    "_directory",
    "_file_path",
    "_filepath",
    "_filename",
    "_locator",
    "_path",
    "_paths",
    "_uri",
)
_SAFE_HOLDOUT_SUFFIXES = (
    "_hash",
    "_ref",
    "_id",
    "_status",
    "_version",
)


class ResearchExplorationQueryError(ValueError):
    """A read-only query is invalid or its evidence authority is unavailable."""


@dataclass(frozen=True, slots=True)
class ExplorationRecord:
    kind: str
    logical_id: str
    version: str
    status: str
    summary: dict[str, Any]
    technical: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXPLORATION_QUERY_SCHEMA_VERSION,
            "kind": self.kind,
            "logical_id": self.logical_id,
            "version": self.version,
            "status": self.status,
            "summary": safe_research_projection(self.summary),
            "technical": (
                safe_research_projection(self.technical)
                if self.technical is not None
                else None
            ),
        }


def query_lineage_records(
    *,
    manager: ResearchPathManager,
    record_type: str | None = None,
    logical_id: str | None = None,
    detail_level: str = "summary",
) -> tuple[ExplorationRecord, ...]:
    _require_detail_level(detail_level)
    if record_type is not None and record_type not in _LINEAGE_TYPES:
        raise ResearchExplorationQueryError("lineage_record_type_invalid")
    validation = validate_knowledge_registry(manager)
    if validation.get("status") != "PASS":
        raise ResearchExplorationQueryError("knowledge_registry_invalid")
    snapshot = read_hash_chained_jsonl_snapshot(
        path=knowledge_registry_path(manager),
        label="research_knowledge_registry",
    )
    if snapshot.status != "PASS":
        raise ResearchExplorationQueryError("knowledge_registry_invalid")
    records = []
    for row in snapshot.rows:
        row_type = str(row.get("record_type") or "")
        row_id = str(row.get("logical_id") or "")
        if row_type not in _LINEAGE_TYPES:
            continue
        if record_type is not None and row_type != record_type:
            continue
        if logical_id is not None and row_id != logical_id:
            continue
        records.append(
            _lineage_record(
                row,
                technical=(detail_level == "technical"),
            )
        )
    return tuple(records)


def query_lineage_detail(
    *,
    manager: ResearchPathManager,
    record_type: str,
    logical_id: str,
    version: str,
    detail_level: str = "summary",
) -> ExplorationRecord:
    _require_detail_level(detail_level)
    if record_type not in _LINEAGE_TYPES:
        raise ResearchExplorationQueryError("lineage_record_type_invalid")
    try:
        row = get_knowledge_record(
            manager=manager,
            record_type=record_type,
            logical_id=logical_id,
            version=version,
        )
    except ValueError as exc:
        if str(exc) == "knowledge_record_missing":
            raise ResearchExplorationQueryError("research_resource_not_found") from exc
        raise ResearchExplorationQueryError("knowledge_registry_invalid") from exc
    record = _lineage_record(row, technical=(detail_level == "technical"))
    if detail_level == "technical":
        try:
            outbound = query_outbound_refs(
                manager=manager,
                record_type=record_type,
                logical_id=logical_id,
                version=version,
            )
            target = KnowledgeRef(
                record_type=record_type,
                logical_id=logical_id,
                version=version,
                record_hash=str(row.get("record_hash") or ""),
            )
            inbound = query_inbound_refs(manager=manager, target=target)
        except ValueError as exc:
            raise ResearchExplorationQueryError("knowledge_registry_invalid") from exc
        technical = dict(record.technical or {})
        technical.update(
            {
                "outbound_lineage": [_record_identity(item) for item in outbound],
                "inbound_lineage": [_record_identity(item) for item in inbound],
            }
        )
        record = ExplorationRecord(
            kind=record.kind,
            logical_id=record.logical_id,
            version=record.version,
            status=record.status,
            summary=record.summary,
            technical=technical,
        )
    return record


def query_structured_validation_decisions(
    *,
    manager: ResearchPathManager,
    hypothesis_id: str | None = None,
    decision: str | None = None,
    failure_type: str | None = None,
    negative_only: bool = False,
    detail_level: str = "summary",
) -> tuple[ExplorationRecord, ...]:
    _require_detail_level(detail_level)
    if decision is not None and decision not in {
        "REJECTED",
        "INCONCLUSIVE",
        "VALIDATED",
    }:
        raise ResearchExplorationQueryError("validation_decision_filter_invalid")
    try:
        rows = query_validation_decisions(
            manager=manager,
            hypothesis_id=hypothesis_id,
            decision=decision,
            failure_type=failure_type,
        )
    except ValueError as exc:
        raise ResearchExplorationQueryError(
            "validation_decision_registry_invalid"
        ) from exc
    records = []
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise ResearchExplorationQueryError("validation_decision_registry_invalid")
        outcome = str(payload.get("decision") or "")
        if negative_only and outcome not in {"REJECTED", "INCONCLUSIVE"}:
            continue
        records.append(_validation_record(row, technical=(detail_level == "technical")))
    return tuple(records)


def query_validation_decision_detail(
    *,
    manager: ResearchPathManager,
    decision_id: str,
    version: str,
    detail_level: str = "summary",
) -> ExplorationRecord:
    records = query_structured_validation_decisions(
        manager=manager,
        detail_level=detail_level,
    )
    matches = [
        item
        for item in records
        if item.logical_id == decision_id and item.version == version
    ]
    if len(matches) != 1:
        raise ResearchExplorationQueryError("research_resource_not_found")
    return matches[0]


def query_prospective_validations(
    *,
    manager: ResearchPathManager,
    validation_id: str | None = None,
    status: str | None = None,
    detail_level: str = "summary",
) -> tuple[ExplorationRecord, ...]:
    _require_detail_level(detail_level)
    if status is not None and status not in {
        "PENDING",
        "CONFIRMED",
        "DEGRADED",
        "INVALIDATED",
        "INCONCLUSIVE",
    }:
        raise ResearchExplorationQueryError("prospective_status_filter_invalid")
    validation = validate_prospective_registry(manager)
    if validation.get("status") != "PASS":
        raise ResearchExplorationQueryError("prospective_registry_invalid")
    spec_snapshot = read_hash_chained_jsonl_snapshot(
        path=prospective_registry_path(manager),
        label=PROSPECTIVE_VALIDATION_HASH_LABEL,
    )
    conclusion_snapshot = read_hash_chained_jsonl_snapshot(
        path=research_conclusion_registry_path(manager),
        label="research_conclusion",
    )
    if spec_snapshot.status != "PASS" or conclusion_snapshot.status != "PASS":
        raise ResearchExplorationQueryError("prospective_registry_invalid")
    specs = [
        row
        for row in spec_snapshot.rows
        if row.get("record_type") == "PROSPECTIVE_VALIDATION_SPEC"
    ]
    evaluations = [
        row
        for row in spec_snapshot.rows
        if row.get("record_type") == "PROSPECTIVE_EVALUATION"
    ]
    records: list[ExplorationRecord] = []
    for spec_row in specs:
        logical_id = str(spec_row.get("logical_id") or "")
        version = str(spec_row.get("version") or "")
        if validation_id is not None and logical_id != validation_id:
            continue
        matches = [
            row
            for row in evaluations
            if row.get("logical_id") == logical_id and row.get("version") == version
        ]
        if len(matches) > 1:
            raise ResearchExplorationQueryError("prospective_registry_invalid")
        evaluation = matches[0] if matches else None
        conclusion_rows = [
            row
            for row in conclusion_snapshot.rows
            if _conclusion_targets(row, logical_id=logical_id, version=version)
        ]
        record = _prospective_record(
            spec_row,
            evaluation=evaluation,
            conclusions=conclusion_rows,
            technical=(detail_level == "technical"),
        )
        if status is None or record.status == status:
            records.append(record)
    return tuple(records)


def query_prospective_detail(
    *,
    manager: ResearchPathManager,
    validation_id: str,
    version: str,
    detail_level: str = "summary",
) -> ExplorationRecord:
    matches = [
        item
        for item in query_prospective_validations(
            manager=manager,
            validation_id=validation_id,
            detail_level=detail_level,
        )
        if item.version == version
    ]
    if len(matches) != 1:
        raise ResearchExplorationQueryError("research_resource_not_found")
    return matches[0]


def query_final_research_packages(
    *,
    manager: ResearchPathManager,
    detail_level: str = "summary",
    **filters: str | None,
) -> tuple[ExplorationRecord, ...]:
    _require_detail_level(detail_level)
    try:
        packages = search_research_packages(manager=manager, **filters)
    except TypeError as exc:
        raise ResearchExplorationQueryError("research_package_query_invalid") from exc
    except ValueError as exc:
        if str(exc) == "research_package_search_period_invalid":
            raise ResearchExplorationQueryError(
                "research_package_query_invalid"
            ) from exc
        raise ResearchExplorationQueryError(
            "research_package_registry_invalid"
        ) from exc
    return tuple(
        _package_record(package, technical=(detail_level == "technical"))
        for package in packages
    )


def query_final_research_package_detail(
    *,
    manager: ResearchPathManager,
    package_id: str,
    version: str,
    detail_level: str = "summary",
) -> ExplorationRecord:
    _require_detail_level(detail_level)
    try:
        package = get_research_package(
            manager=manager, package_id=package_id, version=version
        )
    except ValueError as exc:
        if str(exc) == "research_package_not_found":
            raise ResearchExplorationQueryError("research_resource_not_found") from exc
        raise ResearchExplorationQueryError(
            "research_package_registry_invalid"
        ) from exc
    return _package_record(package, technical=(detail_level == "technical"))


def query_final_research_package_lineage(
    *, manager: ResearchPathManager, package_id: str, version: str
) -> dict[str, Any]:
    try:
        return cast(
            dict[str, Any],
            safe_research_projection(
                research_package_lineage(
                    manager=manager, package_id=package_id, version=version
                )
            ),
        )
    except ValueError as exc:
        if str(exc) == "research_package_not_found":
            raise ResearchExplorationQueryError("research_resource_not_found") from exc
        raise ResearchExplorationQueryError(
            "research_package_registry_invalid"
        ) from exc


def query_final_research_package_diff(
    *,
    manager: ResearchPathManager,
    left_package_id: str,
    left_version: str,
    right_package_id: str,
    right_version: str,
) -> dict[str, Any]:
    try:
        result = diff_registered_research_packages(
            manager=manager,
            left_package_id=left_package_id,
            left_version=left_version,
            right_package_id=right_package_id,
            right_version=right_version,
        )
    except ValueError as exc:
        if str(exc) == "research_package_not_found":
            raise ResearchExplorationQueryError("research_resource_not_found") from exc
        raise ResearchExplorationQueryError(
            "research_package_registry_invalid"
        ) from exc
    return cast(dict[str, Any], safe_research_projection(result))


def safe_research_projection(value: Any) -> Any:
    """Remove adapter-forbidden raw paths, secrets, and holdout observations."""

    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        holdout_context = (
            "holdout"
            in str(
                value.get("criterion_id")
                or value.get("split")
                or value.get("partition")
                or value.get("evidence_scope")
                or ""
            ).lower()
        )
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.lower()
            if normalized != "changed_paths" and (
                normalized in _PATH_KEY_NAMES
                or normalized.endswith(_PATH_KEY_SUFFIXES)
                or normalized.endswith(
                    ("directory", "filename", "locator", "path", "paths")
                )
            ):
                continue
            if any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS):
                projected[key] = "<redacted>"
                continue
            if holdout_context and normalized in {
                "data",
                "metrics",
                "observed",
                "records",
                "result",
                "results",
                "rows",
                "value",
                "values",
            }:
                projected[key] = "<redacted-holdout-evidence>"
                continue
            if "holdout" in normalized and not normalized.endswith(
                _SAFE_HOLDOUT_SUFFIXES
            ):
                projected[key] = "<redacted-holdout-evidence>"
                continue
            projected[key] = safe_research_projection(item)
        return projected
    if isinstance(value, (list, tuple)):
        return [safe_research_projection(item) for item in value]
    if isinstance(value, Path):
        return "<redacted-path>"
    if isinstance(value, str):
        text = value.strip()
        if Path(text).is_absolute() or PureWindowsPath(text).is_absolute():
            return "<redacted-path>"
        return value
    return deepcopy(value)


def _lineage_record(row: Mapping[str, Any], *, technical: bool) -> ExplorationRecord:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        raise ResearchExplorationQueryError("knowledge_registry_invalid")
    record_type = str(row.get("record_type") or "")
    status = str(
        payload.get("status")
        or payload.get("registration_status")
        or payload.get("fact_status")
        or "RECORDED"
    )
    common = {
        "record_hash": row.get("record_hash"),
        "actor_id": payload.get("actor_id"),
        "recorded_at": payload.get("recorded_at") or payload.get("created_at"),
    }
    if record_type == "observation":
        summary = {
            **common,
            "statement": payload.get("statement"),
            "market": payload.get("market"),
            "interval": payload.get("interval"),
            "observed_at": payload.get("observed_at"),
            "fact_status": payload.get("fact_status"),
        }
    elif record_type == "research_question":
        summary = {
            **common,
            "question_text": payload.get("question_text"),
            "observation_count": len(payload.get("observation_refs") or []),
            "competing_hypothesis_count": len(
                payload.get("competing_hypotheses") or []
            ),
        }
    else:
        summary = {
            **common,
            "phenomenon": payload.get("phenomenon"),
            "mechanism": payload.get("mechanism"),
            "hypothesis_text": payload.get("hypothesis_text"),
            "experiment_family_id": payload.get("experiment_family_id"),
            "registration_status": payload.get("registration_status"),
        }
    technical_payload = (
        {
            "payload": dict(payload),
            "record_hash": row.get("record_hash"),
            "row_hash": row.get("row_hash"),
            "outbound_refs": list(row.get("outbound_refs") or []),
            "authority_refs": list(row.get("authority_refs") or []),
        }
        if technical
        else None
    )
    return ExplorationRecord(
        kind=record_type,
        logical_id=str(row.get("logical_id") or ""),
        version=str(row.get("version") or ""),
        status=status,
        summary=safe_research_projection(summary),
        technical=safe_research_projection(technical_payload),
    )


def _validation_record(row: Mapping[str, Any], *, technical: bool) -> ExplorationRecord:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        raise ResearchExplorationQueryError("validation_decision_registry_invalid")
    hypothesis_ref = payload.get("hypothesis_ref")
    summary = {
        "decision": payload.get("decision"),
        "hypothesis_id": (
            hypothesis_ref.get("logical_id")
            if isinstance(hypothesis_ref, Mapping)
            else None
        ),
        "experiment_id": payload.get("experiment_id"),
        "run_id": payload.get("run_id"),
        "failure_type": payload.get("failure_type"),
        "researcher_interpretation": payload.get("researcher_interpretation"),
        "decided_by": payload.get("decided_by"),
        "decided_at": payload.get("decided_at"),
        "failed_criterion_count": sum(
            not bool(item.get("passed"))
            for item in payload.get("criterion_results") or []
            if isinstance(item, Mapping)
        ),
        "is_negative_outcome": payload.get("decision") in {"REJECTED", "INCONCLUSIVE"},
    }
    technical_payload = (
        {
            "criterion_results": payload.get("criterion_results") or [],
            "evidence_hashes": payload.get("evidence_hashes") or [],
            "reviewer_comment": payload.get("reviewer_comment"),
            "learned": payload.get("learned") or [],
            "followup_hypothesis_refs": payload.get("followup_hypothesis_refs") or [],
            "hypothesis_ref": hypothesis_ref,
            "decision_content_hash": row.get("record_hash"),
            "hypothesis_outcome_record_hash": row.get("hypothesis_outcome_record_hash"),
            "hypothesis_outcome_row_hash": row.get("hypothesis_outcome_row_hash"),
        }
        if technical
        else None
    )
    return ExplorationRecord(
        kind="validation_decision",
        logical_id=str(row.get("logical_id") or ""),
        version=str(row.get("version") or ""),
        status=str(payload.get("decision") or ""),
        summary=safe_research_projection(summary),
        technical=safe_research_projection(technical_payload),
    )


def _prospective_record(
    spec_row: Mapping[str, Any],
    *,
    evaluation: Mapping[str, Any] | None,
    conclusions: Sequence[Mapping[str, Any]],
    technical: bool,
) -> ExplorationRecord:
    spec = spec_row.get("payload")
    if not isinstance(spec, Mapping):
        raise ResearchExplorationQueryError("prospective_registry_invalid")
    evaluation_payload = evaluation.get("payload") if evaluation is not None else None
    if evaluation_payload is not None and not isinstance(evaluation_payload, Mapping):
        raise ResearchExplorationQueryError("prospective_registry_invalid")
    status = (
        str(evaluation_payload.get("status") or "PENDING")
        if isinstance(evaluation_payload, Mapping)
        else "PENDING"
    )
    classifications = [
        str(item.get("classification") or "")
        for item in (
            evaluation_payload.get("comparison") or []
            if isinstance(evaluation_payload, Mapping)
            else []
        )
        if isinstance(item, Mapping)
    ]
    summary = {
        "status": status,
        "start_at": spec.get("start_at"),
        "end_at": spec.get("end_at"),
        "frozen_at": spec.get("frozen_at"),
        "frozen_by": spec.get("frozen_by"),
        "observation_count": (
            evaluation_payload.get("observation_count")
            if isinstance(evaluation_payload, Mapping)
            else 0
        ),
        "outcome_count": (
            evaluation_payload.get("outcome_count")
            if isinstance(evaluation_payload, Mapping)
            else 0
        ),
        "missing_count": (
            evaluation_payload.get("missing_count")
            if isinstance(evaluation_payload, Mapping)
            else 0
        ),
        "missing_rate": (
            evaluation_payload.get("missing_rate")
            if isinstance(evaluation_payload, Mapping)
            else None
        ),
        "late_count": (
            evaluation_payload.get("late_count")
            if isinstance(evaluation_payload, Mapping)
            else 0
        ),
        "late_rate": (
            evaluation_payload.get("late_rate")
            if isinstance(evaluation_payload, Mapping)
            else None
        ),
        "metric_classification_counts": {
            name: classifications.count(name)
            for name in ("CONFIRMED", "DEGRADED", "INVALIDATED")
        },
        "review_required": (
            evaluation_payload.get("review_required")
            if isinstance(evaluation_payload, Mapping)
            else False
        ),
        "stopping_triggered": (
            evaluation_payload.get("stopping_triggered")
            if isinstance(evaluation_payload, Mapping)
            else False
        ),
        "conclusion_count": len(conclusions),
    }
    technical_payload = None
    if technical:
        technical_payload = {
            "source_package_ref": spec.get("source_package_ref"),
            "hypothesis_ref": spec.get("hypothesis_ref"),
            "validation_decision_ref": spec.get("validation_decision_ref"),
            "validated_rule_set_hash": spec.get("validated_rule_set_hash"),
            "feature_definition_hash": spec.get("feature_definition_hash"),
            "cost_assumption_hash": spec.get("cost_assumption_hash"),
            "fill_assumption_hash": spec.get("fill_assumption_hash"),
            "historical_distribution_hash": spec.get("historical_distribution_hash"),
            "metric_guards": spec.get("metric_guards") or [],
            "evaluation": (
                {
                    "content_hash": evaluation.get("record_hash"),
                    "reasons": evaluation_payload.get("reasons") or [],
                    "comparison": evaluation_payload.get("comparison") or [],
                    "observed_metrics": evaluation_payload.get("observed_metrics")
                    or {},
                    "observation_stream_hash": evaluation_payload.get(
                        "observation_stream_hash"
                    ),
                }
                if evaluation is not None and isinstance(evaluation_payload, Mapping)
                else None
            ),
            "conclusions": [
                {
                    "content_hash": row.get("record_hash"),
                    "payload": row.get("payload"),
                }
                for row in conclusions
            ],
        }
    return ExplorationRecord(
        kind="prospective_validation",
        logical_id=str(spec_row.get("logical_id") or ""),
        version=str(spec_row.get("version") or ""),
        status=status,
        summary=safe_research_projection(summary),
        technical=safe_research_projection(technical_payload),
    )


def _package_record(package: Any, *, technical: bool) -> ExplorationRecord:
    summary = {
        **package.index.as_dict(),
        "content_hash": package.content_hash,
        "source_package_ref": package.refs.source_package.as_dict(),
        "hypothesis_ref": package.refs.hypothesis.as_dict(),
        "dataset_snapshot_ref": package.refs.dataset_snapshot.as_dict(),
        "prospective_validation_ref": package.refs.prospective_validation.as_dict(),
        "research_conclusion_ref": package.refs.research_conclusion.as_dict(),
        "supersedes": package.supersedes.as_dict() if package.supersedes else None,
    }
    technical_payload = None
    if technical:
        payload = package.as_dict()
        technical_payload = {
            "evidence_refs": payload["refs"],
            "validated_rule_set": payload["validated_rule_set"],
            "validated_rule_set_hash": payload["validated_rule_set_hash"],
            "assumptions": payload["assumptions"],
            "limitations": payload["limitations"],
            "reproduction_recipe": payload["reproduction_recipe"],
            "prospective_validation": payload["prospective_validation"],
            "prospective_evaluation": payload["prospective_evaluation"],
            "research_conclusion": payload["research_conclusion"],
        }
    return ExplorationRecord(
        kind="research_package",
        logical_id=package.package_id,
        version=package.version,
        status=package.index.status,
        summary=safe_research_projection(summary),
        technical=safe_research_projection(technical_payload),
    )


def _record_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("reference_kind") == "authority":
        return cast(dict[str, Any], safe_research_projection(dict(value)))
    return cast(
        dict[str, Any],
        safe_research_projection(
            {
                "record_type": value.get("record_type"),
                "logical_id": value.get("logical_id"),
                "version": value.get("version"),
                "record_hash": value.get("record_hash"),
                "row_hash": value.get("row_hash"),
            }
        ),
    )


def _conclusion_targets(
    row: Mapping[str, Any], *, logical_id: str, version: str
) -> bool:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return False
    ref = payload.get("prospective_validation_ref")
    return bool(
        isinstance(ref, Mapping)
        and ref.get("logical_id") == logical_id
        and ref.get("version") == version
    )


def _require_detail_level(value: str) -> None:
    if value not in _DETAIL_LEVELS:
        raise ResearchExplorationQueryError("research_detail_level_invalid")
