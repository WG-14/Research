"""Authenticated internal-web adapter for published research query contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from django.core.exceptions import PermissionDenied
from django.http import HttpRequest

from market_research.application.exploration import (
    ExplorationRecord,
    ResearchExplorationQueryError,
    query_dataset_artifact_detail,
    query_dataset_artifacts,
    query_feature_definition_detail,
    query_feature_definitions,
    query_final_research_package_detail,
    query_final_research_package_diff,
    query_final_research_package_lineage,
    query_final_research_packages,
    query_lineage_detail,
    query_lineage_records,
    query_prospective_detail,
    query_prospective_validations,
    query_structured_validation_decisions,
    query_validation_decision_detail,
    safe_research_projection,
)
from market_research.application.platform_contracts import ResearchPathManager
from market_research.research_composition import builtin_strategy_registry

from .audit import append_web_audit_event
from .security import actor_snapshot


RESEARCH_EXPLORATION_PERMISSION = "portal.view_researchjob"
_LINEAGE_SECTION_TYPES = {
    "observations": "observation",
    "questions": "research_question",
    "hypotheses": "hypothesis",
}
_SECTIONS = frozenset(
    {
        *_LINEAGE_SECTION_TYPES,
        "lineage",
        "decisions",
        "datasets",
        "features",
        "prospective",
        "packages",
    }
)


@dataclass(frozen=True, slots=True)
class ResearchExplorerService:
    manager: ResearchPathManager

    def list_records(
        self,
        *,
        section: str,
        filters: dict[str, str],
        detail_level: str = "summary",
    ) -> tuple[dict[str, Any], ...]:
        if section not in _SECTIONS:
            raise ResearchExplorationQueryError("research_section_invalid")
        if section in {*_LINEAGE_SECTION_TYPES, "lineage"}:
            record_type = _LINEAGE_SECTION_TYPES.get(section) or (
                filters.get("record_type") or None
            )
            records = query_lineage_records(
                manager=self.manager,
                record_type=record_type,
                logical_id=filters.get("logical_id") or None,
                detail_level=detail_level,
            )
        elif section == "decisions":
            records = query_structured_validation_decisions(
                manager=self.manager,
                hypothesis_id=filters.get("hypothesis_id") or None,
                decision=filters.get("decision") or None,
                failure_type=filters.get("failure_type") or None,
                negative_only=filters.get("negative_only") == "true",
                detail_level=detail_level,
            )
        elif section == "prospective":
            records = query_prospective_validations(
                manager=self.manager,
                validation_id=filters.get("validation_id") or None,
                status=filters.get("status") or None,
                detail_level=detail_level,
            )
        elif section == "datasets":
            records = query_dataset_artifacts(
                manager=self.manager,
                artifact_id=filters.get("artifact_id") or None,
                market=filters.get("market") or None,
                interval=filters.get("interval") or None,
                provider_id=filters.get("provider_id") or None,
                dataset_id=filters.get("dataset_id") or None,
                quality_status=filters.get("quality_status") or None,
                start_ts=filters.get("start_ts") or None,
                end_ts=filters.get("end_ts") or None,
                as_of_ts=filters.get("as_of_ts") or None,
                known_at=filters.get("known_at") or None,
                detail_level=detail_level,
            )
        elif section == "features":
            records = query_feature_definitions(
                registry=builtin_strategy_registry(),
                feature_id=filters.get("feature_id") or None,
                strategy=filters.get("strategy") or None,
                input_name=filters.get("input_name") or None,
                detail_level=detail_level,
            )
        else:
            package_filters = {
                key: filters[key]
                for key in (
                    "market",
                    "instrument",
                    "hypothesis_type",
                    "status",
                    "researcher",
                    "dataset",
                    "period_start",
                    "period_end",
                    "prospective_status",
                )
                if filters.get(key)
            }
            records = query_final_research_packages(
                manager=self.manager,
                detail_level=detail_level,
                **package_filters,
            )
        return tuple(self._project_record(item) for item in records)

    def get_record(
        self,
        *,
        section: str,
        logical_id: str,
        version: str,
        detail_level: str = "technical",
        record_type: str | None = None,
    ) -> dict[str, Any]:
        if section in _LINEAGE_SECTION_TYPES or section == "lineage":
            resolved_type = _LINEAGE_SECTION_TYPES.get(section) or record_type
            if resolved_type is None:
                raise ResearchExplorationQueryError("lineage_record_type_invalid")
            record = query_lineage_detail(
                manager=self.manager,
                record_type=resolved_type,
                logical_id=logical_id,
                version=version,
                detail_level=detail_level,
            )
        elif section == "decisions":
            record = query_validation_decision_detail(
                manager=self.manager,
                decision_id=logical_id,
                version=version,
                detail_level=detail_level,
            )
        elif section == "prospective":
            record = query_prospective_detail(
                manager=self.manager,
                validation_id=logical_id,
                version=version,
                detail_level=detail_level,
            )
        elif section == "datasets":
            record = query_dataset_artifact_detail(
                manager=self.manager,
                artifact_id=logical_id,
                version=version,
                detail_level=detail_level,
            )
        elif section == "features":
            record = query_feature_definition_detail(
                registry=builtin_strategy_registry(),
                feature_id=logical_id,
                version=version,
                detail_level=detail_level,
            )
        elif section == "packages":
            record = query_final_research_package_detail(
                manager=self.manager,
                package_id=logical_id,
                version=version,
                detail_level=detail_level,
            )
        else:
            raise ResearchExplorationQueryError("research_section_invalid")
        return self._project_record(record)

    def package_lineage(self, *, package_id: str, version: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            safe_research_projection(
                query_final_research_package_lineage(
                    manager=self.manager,
                    package_id=package_id,
                    version=version,
                )
            ),
        )

    def package_diff(
        self,
        *,
        left_package_id: str,
        left_version: str,
        right_package_id: str,
        right_version: str,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            safe_research_projection(
                query_final_research_package_diff(
                    manager=self.manager,
                    left_package_id=left_package_id,
                    left_version=left_version,
                    right_package_id=right_package_id,
                    right_version=right_version,
                )
            ),
        )

    @staticmethod
    def _project_record(record: ExplorationRecord) -> dict[str, Any]:
        payload = record.as_dict()
        section = {
            "observation": "observations",
            "research_question": "questions",
            "hypothesis": "hypotheses",
            "validation_decision": "decisions",
            "prospective_validation": "prospective",
            "dataset_artifact": "datasets",
            "feature_definition": "features",
            "research_package": "packages",
        }.get(record.kind, record.kind)
        if record.kind in {"observation", "research_question", "hypothesis"}:
            technical_link = (
                f"/api/v1/research/lineage/{record.kind}/{record.logical_id}/"
                f"{record.version}/?detail=technical"
            )
        else:
            api_section = {
                "decisions": "validation-decisions",
                "prospective": "prospective",
                "datasets": "datasets",
                "features": "features",
                "packages": "packages",
            }.get(section, section)
            technical_link = (
                f"/api/v1/research/{api_section}/{record.logical_id}/"
                f"{record.version}/?detail=technical"
            )
        payload["links"] = {
            "web": f"/research/{section}/{record.logical_id}/{record.version}/",
            "technical": technical_link,
        }
        # Registry material was already projected by ``record.as_dict``.  The
        # links are adapter-owned root-relative routes, not filesystem paths.
        return payload


def audit_research_exploration_read(
    request: HttpRequest,
    *,
    object_type: str,
    object_id: str,
    filters: dict[str, Any] | None = None,
    detail_level: str,
) -> None:
    """Audit the actor, resolved permission, and bounded query material."""

    actor_id, roles, permissions = actor_snapshot(request.user)
    if "research.view" not in permissions:
        raise PermissionDenied("research_view_application_permission_required")
    append_web_audit_event(
        action="research_exploration_read",
        actor_id=actor_id,
        object_type=object_type,
        object_id=object_id,
        correlation_id=str(getattr(request, "correlation_id", "unknown")),
        details={
            "django_permission": RESEARCH_EXPLORATION_PERMISSION,
            "application_permission": "research.view",
            "roles": roles,
            "detail_level": detail_level,
            "filters": safe_research_projection(filters or {}),
        },
    )


__all__ = [
    "RESEARCH_EXPLORATION_PERMISSION",
    "ResearchExplorerService",
    "audit_research_exploration_read",
]
