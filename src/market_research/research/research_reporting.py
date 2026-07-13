"""Deterministic comparison and operator-readable rendering of decision reports."""

from __future__ import annotations

import json
from typing import Any

from .hashing import content_hash_payload, sha256_prefixed
from .research_decision_report import REPORT_SECTIONS, validate_research_decision_report


class ResearchReportingError(ValueError):
    pass


def compare_research_decision_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if len(reports) < 2:
        raise ResearchReportingError("research_comparison_requires_at_least_two_reports")
    for index, report in enumerate(reports):
        reasons = validate_research_decision_report(report)
        if reasons:
            raise ResearchReportingError(
                f"research_comparison_report_invalid:{index}:" + ",".join(reasons)
            )
    ordered = sorted(
        reports,
        key=lambda item: (str(item.get("experiment_id") or ""), str(item.get("content_hash") or "")),
    )
    conditions = [item["sections"]["hypothesis_and_experiment_conditions"] for item in ordered]
    dimensions = {
        name: sorted({str(item.get(name)) for item in conditions})
        for name in ("market", "interval", "strategy_name", "strategy_version")
    }
    incompatible = [name for name, values in dimensions.items() if len(values) > 1]
    material = {
        "schema_version": 1,
        "artifact_type": "research_decision_report_comparison",
        "comparison_compatibility": "PASS" if not incompatible else "WARN",
        "incompatible_dimensions": incompatible,
        "dimension_values": dimensions,
        "reports": [
            {
                "experiment_id": item.get("experiment_id"),
                "selected_candidate_id": item.get("selected_candidate_id"),
                "validation_result": item.get("validation_result"),
                "source_report_hash": item.get("content_hash"),
                "core_performance": item["sections"]["core_performance"],
                "parameter_robustness": item["sections"]["parameter_robustness"],
                "out_of_sample_results": item["sections"]["out_of_sample_results"],
                "known_limitations": item["sections"]["known_limitations"],
                "research_conclusion": item["sections"]["research_conclusion"],
            }
            for item in ordered
        ],
    }
    return {**material, "content_hash": sha256_prefixed(content_hash_payload(material), label="research_report_comparison")}


def render_research_decision_report_markdown(report: dict[str, Any]) -> str:
    reasons = validate_research_decision_report(report)
    if reasons:
        raise ResearchReportingError("research_render_report_invalid:" + ",".join(reasons))
    lines = [
        f"# Research Decision Report: {report['experiment_id']}",
        "",
        f"- Validation result: `{report['validation_result']}`",
        f"- Selected candidate: `{report.get('selected_candidate_id')}`",
        f"- Manifest hash: `{report['manifest_hash']}`",
        f"- Evidence report hash: `{report['content_hash']}`",
        "",
    ]
    for section_name in REPORT_SECTIONS:
        title = section_name.replace("_", " ").title()
        lines.extend((f"## {title}", "", "```json"))
        lines.extend(json.dumps(
            report["sections"][section_name], ensure_ascii=False, sort_keys=True, indent=2,
        ).splitlines())
        lines.extend(("```", ""))
    return "\n".join(lines)
