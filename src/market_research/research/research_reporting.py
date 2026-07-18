"""Deterministic comparison and operator-readable rendering of decision reports."""

from __future__ import annotations

import json
from typing import Any

from .hashing import content_hash_payload, sha256_prefixed
from .research_decision_report import REPORT_SECTIONS, validate_research_decision_report


class ResearchReportingError(ValueError):
    pass


_COMPARISON_CATEGORIES = (
    "parameters",
    "data",
    "code",
    "signals",
    "fills",
    "costs",
    "metrics",
    "regimes",
)


def compare_research_decision_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if len(reports) < 2:
        raise ResearchReportingError(
            "research_comparison_requires_at_least_two_reports"
        )
    for index, report in enumerate(reports):
        reasons = validate_research_decision_report(report)
        if reasons:
            raise ResearchReportingError(
                f"research_comparison_report_invalid:{index}:" + ",".join(reasons)
            )
    ordered = sorted(
        reports,
        key=lambda item: (
            str(item.get("experiment_id") or ""),
            str(item.get("content_hash") or ""),
        ),
    )
    conditions = [
        item["sections"]["hypothesis_and_experiment_conditions"] for item in ordered
    ]
    dimensions = {
        name: sorted({str(item.get(name)) for item in conditions})
        for name in ("market", "interval", "strategy_name", "strategy_version")
    }
    incompatible = [name for name, values in dimensions.items() if len(values) > 1]
    evidence = [_comparison_evidence(item) for item in ordered]
    material = {
        "schema_version": 1,
        "artifact_type": "research_decision_report_comparison",
        "comparison_compatibility": "PASS" if not incompatible else "WARN",
        "incompatible_dimensions": incompatible,
        "dimension_values": dimensions,
        "difference_summary": {
            category: _category_difference(
                category=category,
                reports=ordered,
                values=[item[category] for item in evidence],
            )
            for category in _COMPARISON_CATEGORIES
        },
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
    return {
        **material,
        "content_hash": sha256_prefixed(
            content_hash_payload(material), label="research_report_comparison"
        ),
    }


def _comparison_evidence(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sections = report["sections"]
    conditions = sections["hypothesis_and_experiment_conditions"]
    trade = sections["trade_analysis"]
    return {
        "parameters": {
            "selected_candidate_id": report.get("selected_candidate_id"),
            "hypothesis_spec": conditions.get("hypothesis_spec"),
            "parameter_space_hash": conditions.get("parameter_space_hash"),
            "portfolio_policy": conditions.get("portfolio_policy"),
            "risk_policy": conditions.get("risk_policy"),
            "execution_timing_policy": conditions.get("execution_timing_policy"),
        },
        "data": {
            "dataset_splits": conditions.get("dataset_splits"),
            "data_quality": sections["data_quality"],
        },
        "code": {
            "manifest_hash": report.get("manifest_hash"),
            "strategy_name": conditions.get("strategy_name"),
            "strategy_version": conditions.get("strategy_version"),
            "code_evidence": conditions.get("code_evidence"),
        },
        "signals": {
            "participation_summary": trade.get("participation_summary"),
            "closed_trade_diagnostics": trade.get("closed_trade_diagnostics"),
        },
        "fills": {
            "execution_event_summary": trade.get("execution_event_summary"),
        },
        "costs": sections["cost_analysis"],
        "metrics": {
            "core_performance": sections["core_performance"],
            "out_of_sample_results": sections["out_of_sample_results"],
        },
        "regimes": sections["market_regime_analysis"],
    }


def _category_difference(
    *,
    category: str,
    reports: list[dict[str, Any]],
    values: list[dict[str, Any]],
) -> dict[str, Any]:
    changed_paths = _changed_paths(values, prefix=category)
    return {
        "status": "DIFFERENT" if changed_paths else "SAME",
        "changed_paths": changed_paths,
        "evidence_by_report": [
            {
                "experiment_id": report.get("experiment_id"),
                "source_report_hash": report.get("content_hash"),
                "value": value,
            }
            for report, value in zip(reports, values)
        ],
    }


def _changed_paths(values: list[Any], *, prefix: str) -> list[str]:
    if all(isinstance(value, dict) for value in values):
        keys = sorted({str(key) for value in values for key in value})
        paths: list[str] = []
        for key in keys:
            child_values = [
                value.get(key, {"__comparison_missing__": True}) for value in values
            ]
            paths.extend(_changed_paths(child_values, prefix=f"{prefix}.{key}"))
        return paths
    if all(isinstance(value, list) for value in values):
        lengths = {len(value) for value in values}
        if len(lengths) != 1:
            return [prefix]
        paths = []
        for index in range(len(values[0])):
            paths.extend(
                _changed_paths(
                    [value[index] for value in values],
                    prefix=f"{prefix}[{index}]",
                )
            )
        return paths
    canonical = {
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for value in values
    }
    return [prefix] if len(canonical) > 1 else []


def render_research_decision_report_markdown(report: dict[str, Any]) -> str:
    reasons = validate_research_decision_report(report)
    if reasons:
        raise ResearchReportingError(
            "research_render_report_invalid:" + ",".join(reasons)
        )
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
        lines.extend(
            json.dumps(
                report["sections"][section_name],
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ).splitlines()
        )
        lines.extend(("```", ""))
    return "\n".join(lines)
