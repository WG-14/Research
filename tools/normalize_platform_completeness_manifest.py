#!/usr/bin/env python3
"""Normalize the immutable rubric and current review into the gate manifest.

The normalizer imports only criterion identity, executable acceptance text, and
the old report's declared score. It never turns that score into verified
evidence. Candidate test paths and commands are hash-bound, while receipt hashes
remain null until a real run emits an external receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RUBRIC_SHA256 = (
    "5534d1a9863e6b8d95513a1e7f6d4b8faeb3e6fa4203d556e7478e2cfc395e8f"
)
EXPECTED_INSTRUCTION_SHA256 = (
    "7e39fa3665d546fe017f23c093bf3b8db6ffafe743f7838c9d4ed1759577d376"
)
AREA_SPECS: tuple[tuple[str, str, int, int], ...] = (
    ("R", "Research operating model and end-to-end flow", 5, 6),
    ("D", "Core domain models and contracts", 6, 8),
    ("L", "Lifecycle and state transitions", 4, 5),
    ("DA", "Data platform and data model", 7, 11),
    ("P", "Point-in-time accuracy, lineage, and reproducibility", 8, 9),
    ("E", "Experiment design and execution engine", 7, 9),
    ("BT", "Backtest correctness and execution realism", 10, 13),
    ("V", "Validation, robustness, and metrics", 6, 11),
    ("S", "Strategy registry and execution contract", 6, 8),
    ("M", "Manual trading workflow", 5, 8),
    ("MON", "Live comparison and edge monitoring", 5, 7),
    ("K", "Knowledge, documentation, and auditability", 4, 7),
    ("UX", "GUI, API, and user experience", 4, 8),
    ("SEC", "Authorization, approval, security, and governance", 4, 7),
    ("OPS", "Deployment, operations, observability, and recovery", 5, 10),
    ("T", "Tests and engineering quality", 8, 15),
    ("A", "Repository structure, dependencies, and extensibility", 6, 11),
)
EVIDENCE_PROFILES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "R": (
        ("tests/test_strategy_extension_production_e2e.py",),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_strategy_extension_production_e2e.py",
        ),
    ),
    "D": (
        (
            "tests/test_hypothesis_contract.py",
            "tests/test_application_contracts_and_capabilities.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_hypothesis_contract.py",
            "tests/test_application_contracts_and_capabilities.py",
        ),
    ),
    "L": (
        (
            "tests/test_research_governance.py",
            "tests/test_governance_decision_records.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_research_governance.py",
            "tests/test_governance_decision_records.py",
        ),
    ),
    "DA": (
        (
            "tests/test_dataset_artifact_manifest_contract.py",
            "tests/test_dataset_evidence_binding.py",
            "tests/test_market_data_knowledge_time.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_dataset_artifact_manifest_contract.py",
            "tests/test_dataset_evidence_binding.py",
            "tests/test_market_data_knowledge_time.py",
        ),
    ),
    "P": (
        (
            "tests/test_code_provenance.py",
            "tests/test_research_reproduction.py",
            "tests/test_knowledge_registry.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_code_provenance.py",
            "tests/test_research_reproduction.py",
            "tests/test_knowledge_registry.py",
        ),
    ),
    "E": (
        (
            "tests/test_run_lifecycle.py",
            "tests/test_validation_admission_integration.py",
            "tests/test_common_engine_heartbeat.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_run_lifecycle.py",
            "tests/test_validation_admission_integration.py",
            "tests/test_common_engine_heartbeat.py",
        ),
    ),
    "BT": (
        (
            "tests/test_execution_timeline_invariants.py",
            "tests/test_portfolio_accounting_properties.py",
            "tests/test_common_simulation_risk_policy.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_execution_timeline_invariants.py",
            "tests/test_portfolio_accounting_properties.py",
            "tests/test_common_simulation_risk_policy.py",
        ),
    ),
    "V": (
        (
            "tests/test_validation_pipeline_gate.py",
            "tests/test_frozen_dataset_walk_forward_integration.py",
            "tests/test_validation_stress_suite_contract.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_validation_pipeline_gate.py",
            "tests/test_frozen_dataset_walk_forward_integration.py",
            "tests/test_validation_stress_suite_contract.py",
        ),
    ),
    "S": (
        (
            "tests/test_strategy_compilation_authority.py",
            "tests/test_strategy_research_package.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_strategy_compilation_authority.py",
            "tests/test_strategy_research_package.py",
        ),
    ),
    "K": (
        (
            "tests/test_knowledge_registry.py",
            "tests/test_governance_decision_records.py",
            "tests/test_documentation_contract.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_knowledge_registry.py",
            "tests/test_governance_decision_records.py",
            "tests/test_documentation_contract.py",
        ),
    ),
    "UX": (
        (
            "apps/internal_web/tests/test_browser_e2e.py",
            "apps/internal_web/tests/test_views_execution.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "apps/internal_web/tests/test_browser_e2e.py",
            "apps/internal_web/tests/test_views_execution.py",
        ),
    ),
    "SEC": (
        (
            "apps/internal_web/tests/test_authentication_audit_admin_boundary.py",
            "apps/internal_web/tests/test_security_storage.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "apps/internal_web/tests/test_authentication_audit_admin_boundary.py",
            "apps/internal_web/tests/test_security_storage.py",
        ),
    ),
    "OPS": (
        (
            "services/research_operations/tests/test_native_deployment.py",
            "services/research_operations/tests/test_ci_blank_restore_rehearsal.py",
            "services/research_operations/tests/test_postgresql_core.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "services/research_operations/tests/test_native_deployment.py",
            "services/research_operations/tests/test_ci_blank_restore_rehearsal.py",
            "services/research_operations/tests/test_postgresql_core.py",
        ),
    ),
    "T": (
        (
            "tests/test_future_suffix_invariance.py",
            "tests/test_portfolio_accounting_properties.py",
            "tests/test_monorepo_packaging.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_future_suffix_invariance.py",
            "tests/test_portfolio_accounting_properties.py",
            "tests/test_monorepo_packaging.py",
        ),
    ),
    "A": (
        (
            "tests/test_monorepo_architecture.py",
            "tests/test_architecture_strategy_boundaries.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_monorepo_architecture.py",
            "tests/test_architecture_strategy_boundaries.py",
        ),
    ),
}
CRITERION_EVIDENCE_PROFILES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "R-02": (
        (
            "src/market_research/research/knowledge_contract.py",
            "src/market_research/research/knowledge_registry.py",
            "tests/test_knowledge_registry.py",
        ),
        (".venv/bin/pytest", "-q", "tests/test_knowledge_registry.py"),
    ),
    "R-03": (
        (
            "src/market_research/research/knowledge_contract.py",
            "src/market_research/research/knowledge_registry.py",
            "tests/test_knowledge_registry.py",
        ),
        (".venv/bin/pytest", "-q", "tests/test_knowledge_registry.py"),
    ),
    "D-02": (
        (
            "src/market_research/research/instrument_contract.py",
            "src/market_research/research/knowledge_contract.py",
            "tests/test_instrument_domain_contracts.py",
            "tests/test_knowledge_registry.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_instrument_domain_contracts.py",
            "tests/test_knowledge_registry.py",
        ),
    ),
    "DA-02": (
        (
            "src/market_research/research/datasets/source_provenance.py",
            "tests/test_dataset_artifact_manifest_contract.py",
            "docs/research-data-policy.md",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_dataset_artifact_manifest_contract.py",
        ),
    ),
    "DA-04": (
        (
            "src/market_research/research/instrument_contract.py",
            "tests/test_instrument_domain_contracts.py",
        ),
        (".venv/bin/pytest", "-q", "tests/test_instrument_domain_contracts.py"),
    ),
    "DA-05": (
        (
            "src/market_research/research/corporate_action_contract.py",
            "tests/test_point_in_time_domain_contracts.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_point_in_time_domain_contracts.py",
        ),
    ),
    "DA-06": (
        (
            "src/market_research/research/market_calendar_contract.py",
            "tests/test_point_in_time_domain_contracts.py",
            "docs/research-data-policy.md",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_point_in_time_domain_contracts.py",
        ),
    ),
    "DA-07": (
        (
            "src/market_research/research/datasets/schema_dictionary.py",
            "tests/test_dataset_schema_dictionary.py",
            "docs/generated/research-data-dictionary.json",
        ),
        (".venv/bin/pytest", "-q", "tests/test_dataset_schema_dictionary.py"),
    ),
    "P-02": (
        (
            "src/market_research/research/universe_contract.py",
            "tests/test_point_in_time_domain_contracts.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_point_in_time_domain_contracts.py",
        ),
    ),
    "P-03": (
        (
            "src/market_research/research/universe_contract.py",
            "src/market_research/research/corporate_action_contract.py",
            "tests/test_point_in_time_domain_contracts.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_point_in_time_domain_contracts.py",
        ),
    ),
    "K-06": (
        (
            "tools/check_documentation.py",
            "tools/check_dataset_dictionary.py",
            "tools/check_internal_web_contracts.py",
            "tests/test_documentation_contract.py",
            "apps/internal_web/tests/test_api_contract.py",
        ),
        ("scripts/platform", "docs-check"),
    ),
    "E-06": (
        (
            "src/market_research/research/process_runtime.py",
            "tests/test_common_engine_failure_audit.py",
            "tests/test_run_lifecycle.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_common_engine_failure_audit.py",
            "tests/test_run_lifecycle.py",
        ),
    ),
    "E-09": (
        (
            "src/market_research/research/research_reporting.py",
            "tests/test_application_report_comparison.py",
            "tests/test_research_reporting.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_application_report_comparison.py",
            "tests/test_research_reporting.py",
        ),
    ),
    "V-06": (
        (
            "src/market_research/research/result_concentration.py",
            "tests/test_result_concentration.py",
        ),
        (".venv/bin/pytest", "-q", "tests/test_result_concentration.py"),
    ),
    "V-08": (
        (
            "src/market_research/research/metrics_contract.py",
            "src/market_research/research/report_writer.py",
            "tests/test_metrics_completeness_contract.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_metrics_completeness_contract.py",
        ),
    ),
    "V-09": (
        (
            "src/market_research/research/metrics_contract.py",
            "src/market_research/research/report_writer.py",
            "tests/test_metrics_completeness_contract.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_metrics_completeness_contract.py",
        ),
    ),
    "BT-12": (
        (
            "src/market_research/research/universe_contract.py",
            "tests/test_point_in_time_domain_contracts.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_point_in_time_domain_contracts.py",
        ),
    ),
    "UX-01": (
        (
            "apps/internal_web/tests/test_accessibility_contract.py",
            "apps/internal_web/tests/test_views_execution.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "apps/internal_web/tests/test_accessibility_contract.py",
            "apps/internal_web/tests/test_views_execution.py",
        ),
    ),
    "UX-02": (
        ("apps/internal_web/tests/test_accessibility_contract.py",),
        (
            ".venv/bin/pytest",
            "-q",
            "apps/internal_web/tests/test_accessibility_contract.py",
        ),
    ),
    "UX-03": (
        (
            "apps/internal_web/tests/test_accessibility_contract.py",
            "apps/internal_web/tests/test_reports.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "apps/internal_web/tests/test_accessibility_contract.py",
            "apps/internal_web/tests/test_reports.py",
        ),
    ),
    "UX-04": (
        (
            "apps/internal_web/tests/test_accessibility_contract.py",
            "apps/internal_web/tests/test_api_contract.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "apps/internal_web/tests/test_accessibility_contract.py",
            "apps/internal_web/tests/test_api_contract.py",
        ),
    ),
    "UX-05": (
        (
            "apps/internal_web/tests/test_browser_e2e.py",
            "apps/internal_web/tests/test_jobs_worker.py",
            "apps/internal_web/tests/test_api_contract.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "apps/internal_web/tests/test_browser_e2e.py",
            "apps/internal_web/tests/test_jobs_worker.py",
            "apps/internal_web/tests/test_api_contract.py",
        ),
    ),
    "UX-06": (
        (
            "apps/internal_web/src/portal/api_contract.py",
            "apps/internal_web/src/portal/api_views.py",
            "apps/internal_web/tests/test_api_contract.py",
            "docs/generated/internal-web-openapi.json",
        ),
        (".venv/bin/pytest", "-q", "apps/internal_web/tests/test_api_contract.py"),
    ),
    "UX-07": (
        (
            "apps/internal_web/tests/test_views_execution.py",
            "apps/internal_web/tests/test_api_contract.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "apps/internal_web/tests/test_views_execution.py",
            "apps/internal_web/tests/test_api_contract.py",
        ),
    ),
    "UX-08": (
        ("apps/internal_web/tests/test_accessibility_contract.py",),
        (
            ".venv/bin/pytest",
            "-q",
            "apps/internal_web/tests/test_accessibility_contract.py",
        ),
    ),
    "SEC-03": (
        (
            "apps/internal_web/src/portal/authorization.py",
            "apps/internal_web/tests/test_resource_authorization.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "apps/internal_web/tests/test_resource_authorization.py",
        ),
    ),
    "OPS-06": (
        (
            "services/research_operations/src/research_operations/alerting.py",
            "services/research_operations/tests/test_service_alert_unit.py",
            "services/research_operations/tests/test_service_alert_postgresql.py",
            "services/research_operations/docs/runbook.md",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "services/research_operations/tests/test_service_alert_unit.py",
            "services/research_operations/tests/test_service_alert_postgresql.py",
        ),
    ),
    "T-08": (
        (
            "apps/internal_web/tests/test_browser_e2e.py",
            "apps/internal_web/tests/test_jobs_worker.py",
            "apps/internal_web/tests/test_api_contract.py",
        ),
        (
            ".venv/bin/pytest",
            "-q",
            "apps/internal_web/tests/test_browser_e2e.py",
            "apps/internal_web/tests/test_jobs_worker.py",
            "apps/internal_web/tests/test_api_contract.py",
        ),
    ),
    "T-09": (
        (
            "apps/internal_web/src/portal/api_contract.py",
            "apps/internal_web/tests/test_api_contract.py",
        ),
        (".venv/bin/pytest", "-q", "apps/internal_web/tests/test_api_contract.py"),
    ),
    "A-08": (
        (
            "src/market_research/research/instrument_contract.py",
            "src/market_research/research/position_model.py",
            "tests/test_instrument_domain_contracts.py",
        ),
        (".venv/bin/pytest", "-q", "tests/test_instrument_domain_contracts.py"),
    ),
    "A-09": (
        (
            "src/market_research/research/instrument_contract.py",
            "src/market_research/research/position_model.py",
            "tests/test_instrument_domain_contracts.py",
        ),
        (".venv/bin/pytest", "-q", "tests/test_instrument_domain_contracts.py"),
    ),
    "A-10": (
        (
            "src/market_research/research/knowledge_contract.py",
            "src/market_research/research/knowledge_registry.py",
            "tests/test_ai_advisory_contract.py",
            "docs/investment-research-platform.md",
        ),
        (".venv/bin/pytest", "-q", "tests/test_ai_advisory_contract.py"),
    ),
}
BLOCKER_PROFILES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "B-01": (
        ("tests/test_future_suffix_invariance.py",),
        (".venv/bin/pytest", "-q", "tests/test_future_suffix_invariance.py"),
    ),
    "B-02": (
        ("tests/test_research_reproduction.py",),
        (".venv/bin/pytest", "-q", "tests/test_research_reproduction.py"),
    ),
    "B-03": (
        ("tests/test_knowledge_registry.py",),
        (".venv/bin/pytest", "-q", "tests/test_knowledge_registry.py"),
    ),
    "B-04": (
        ("tests/test_strategy_compilation_authority.py",),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_strategy_compilation_authority.py",
        ),
    ),
    "B-05": (
        ("tests/test_validation_pipeline_gate.py",),
        (".venv/bin/pytest", "-q", "tests/test_validation_pipeline_gate.py"),
    ),
    "B-07": (
        ("tests/test_strategy_extension_production_e2e.py",),
        (
            ".venv/bin/pytest",
            "-q",
            "tests/test_strategy_extension_production_e2e.py",
        ),
    ),
    "B-08": (
        ("services/research_operations/tests/test_ci_blank_restore_rehearsal.py",),
        (
            ".venv/bin/pytest",
            "-q",
            "services/research_operations/tests/test_ci_blank_restore_rehearsal.py",
        ),
    ),
}
E5_CRITERIA = {
    "R-06",
    "P-09",
    "E-04",
    "UX-05",
    "OPS-01",
    "OPS-03",
    "OPS-05",
    "OPS-06",
    "OPS-07",
    "OPS-08",
    "OPS-09",
    "T-07",
    "T-08",
    "T-10",
    "T-11",
    "T-14",
    *(f"M-{number:02d}" for number in range(1, 9)),
    *(f"MON-{number:02d}" for number in range(1, 8)),
}


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _path_entry(relative: str) -> dict[str, str]:
    path = PROJECT_ROOT / relative
    if not path.is_file():
        raise ValueError(f"candidate evidence path missing: {relative}")
    return {"path": relative, "sha256": _sha256(path.read_bytes())}


def _evidence(
    subject: str,
    profile: tuple[tuple[str, ...], tuple[str, ...]] | None,
    *,
    minimum_level: str,
) -> dict[str, Any]:
    if profile is None:
        return {
            "minimum_level": minimum_level,
            "paths": [],
            "commands": [],
            "receipts": [],
        }
    paths, argv = profile
    command_id = f"{subject}-verification"
    return {
        "minimum_level": minimum_level,
        "paths": [_path_entry(path) for path in paths],
        "commands": [{"id": command_id, "argv": list(argv)}],
        "receipts": [
            {
                "command_id": command_id,
                "path": f"receipts/{subject}.json",
                "sha256": None,
            }
        ],
    }


def _normalized_rows(review: str) -> dict[str, tuple[str, str, str]]:
    rows: dict[str, tuple[str, str, str]] = {}
    pattern = re.compile(
        r"^\| (?P<id>[A-Z]+-[0-9]{2}) \| (?P<acceptance>.*?) \| "
        r"(?P<verification>.*?) \| (?P<priority>P[0-3] / (?:Critical|High|Medium|Low)) \|$"
    )
    for line in review.splitlines():
        match = pattern.fullmatch(line)
        if match:
            criterion_id = match.group("id")
            if criterion_id in rows:
                raise ValueError(f"duplicate normalized criterion: {criterion_id}")
            rows[criterion_id] = (
                match.group("acceptance"),
                match.group("verification"),
                match.group("priority"),
            )
    return rows


def _declared_scores(review: str) -> dict[str, int | None]:
    marker = "## Final criterion decisions (iteration 15/15)"
    if marker not in review:
        raise ValueError("review is missing final criterion decisions")
    section = review.split(marker, 1)[1].split("### Final score", 1)[0]
    scores: dict[str, int | None] = {}
    for criterion_id, raw in re.findall(r"\b([A-Z]+-[0-9]{2})=(N/A|[0-5])\b", section):
        if criterion_id in scores:
            raise ValueError(f"duplicate declared score: {criterion_id}")
        scores[criterion_id] = None if raw == "N/A" else int(raw)
    return scores


def _rubric_sections(rubric: str) -> dict[str, tuple[str, str]]:
    pattern = re.compile(
        r"^## (?P<id>[A-Z]+-[0-9]{2})\. (?P<title>[^\r\n]+)", re.MULTILINE
    )
    matches = list(pattern.finditer(rubric))
    sections: dict[str, tuple[str, str]] = {}
    for match in matches:
        criterion_id = match.group("id")
        boundary = re.search(
            r"^(?:# [^#]|## [A-Z]+-[0-9]{2}\.)",
            rubric[match.end() :],
            re.MULTILINE,
        )
        end = match.end() + boundary.start() if boundary is not None else len(rubric)
        section = rubric[match.start() : end]
        if criterion_id in sections:
            raise ValueError(f"duplicate rubric criterion: {criterion_id}")
        sections[criterion_id] = (
            match.group("title").strip(),
            _sha256(section.encode("utf-8")),
        )
    return sections


def _criterion_ids_hash(ids: list[str]) -> str:
    return _sha256(("\n".join(sorted(ids)) + "\n").encode("utf-8"))


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def build_manifest(
    *, rubric_path: Path, instruction_path: Path, review_path: Path
) -> dict[str, Any]:
    rubric_bytes = rubric_path.read_bytes()
    instruction_bytes = instruction_path.read_bytes()
    rubric_hash = _sha256(rubric_bytes)
    instruction_hash = _sha256(instruction_bytes)
    if rubric_hash != EXPECTED_RUBRIC_SHA256:
        raise ValueError(f"unexpected rubric SHA-256: {rubric_hash}")
    if instruction_hash != EXPECTED_INSTRUCTION_SHA256:
        raise ValueError(f"unexpected instruction SHA-256: {instruction_hash}")
    rubric = rubric_bytes.decode("utf-8")
    review = review_path.read_text(encoding="utf-8")
    rubric_sections = _rubric_sections(rubric)
    rubric_ids = [
        criterion_id
        for criterion_id in rubric_sections
        if not criterion_id.startswith("B-")
    ]
    normalized = _normalized_rows(review)
    scores = _declared_scores(review)
    expected_ids = [
        f"{prefix}-{number:02d}"
        for prefix, _title, _weight, count in AREA_SPECS
        for number in range(1, count + 1)
    ]
    if rubric_ids != expected_ids:
        raise ValueError("rubric criterion IDs do not match the 153-ID contract")
    if set(normalized) != set(expected_ids) | {
        f"B-{number:02d}" for number in range(1, 9)
    }:
        raise ValueError("normalized checklist does not match rubric IDs and blockers")
    if set(scores) != set(expected_ids):
        raise ValueError("declared score matrix does not match rubric IDs")

    areas = [
        {
            "id": prefix,
            "name": title,
            "weight": weight,
            "criterion_ids": [
                f"{prefix}-{number:02d}" for number in range(1, count + 1)
            ],
        }
        for prefix, title, weight, count in AREA_SPECS
    ]
    area_weights = {prefix: weight for prefix, _title, weight, _count in AREA_SPECS}
    criteria: list[dict[str, Any]] = []
    for criterion_id in expected_ids:
        prefix = criterion_id.rsplit("-", 1)[0]
        acceptance, verification, priority = normalized[criterion_id]
        capability = "supported"
        if prefix in {"M", "MON"}:
            capability = "unsupported"
        criteria.append(
            {
                "id": criterion_id,
                "rubric_title": rubric_sections[criterion_id][0],
                "rubric_section_sha256": rubric_sections[criterion_id][1],
                "area_id": prefix,
                "area_weight": area_weights[prefix],
                "acceptance": acceptance,
                "verification_expectation": verification,
                "priority_and_risk": priority,
                "required_score": 5,
                "declared_score": scores[criterion_id],
                "capability_status": capability,
                "evidence": _evidence(
                    criterion_id,
                    CRITERION_EVIDENCE_PROFILES.get(
                        criterion_id, EVIDENCE_PROFILES.get(prefix)
                    ),
                    minimum_level="E5" if criterion_id in E5_CRITERIA else "E4",
                ),
            }
        )

    blocker_statuses = {
        "B-01": "cleared",
        "B-02": "open",
        "B-03": "cleared",
        "B-04": "open",
        "B-05": "cleared",
        "B-06": "blocked_by_repository_policy",
        "B-07": "open",
        "B-08": "open",
    }
    blockers = []
    for number in range(1, 9):
        blocker_id = f"B-{number:02d}"
        acceptance, verification, priority = normalized[blocker_id]
        blockers.append(
            {
                "id": blocker_id,
                "rubric_title": rubric_sections[blocker_id][0],
                "rubric_section_sha256": rubric_sections[blocker_id][1],
                "acceptance": acceptance,
                "verification_expectation": verification,
                "priority_and_risk": priority,
                "required_status": "cleared",
                "status": blocker_statuses[blocker_id],
                "evidence": _evidence(
                    blocker_id,
                    BLOCKER_PROFILES.get(blocker_id),
                    minimum_level="E5"
                    if blocker_id in {"B-06", "B-07", "B-08"}
                    else "E4",
                ),
            }
        )

    return {
        "schema_version": 1,
        "assessment_basis": (
            "Declared scores are imported from the previous review only as an "
            "unverified snapshot. Candidate files and commands are not completion "
            "evidence until repository-external, hash-bound receipts verify them. "
            "Repository verification receipts are capped at E4; E5 requires a "
            "separate site or organization attestation."
        ),
        "completion_policy": {
            "criterion_count": 153,
            "required_score": 5,
            "required_capability_status": "supported",
            "allow_not_applicable": False,
            "blocker_ids": [f"B-{number:02d}" for number in range(1, 9)],
            "missing_or_invalid_evidence_is_failure": True,
        },
        "rubric": {
            "source_sha256": rubric_hash,
            "instruction_sha256": instruction_hash,
            "criterion_ids_sha256": _criterion_ids_hash(expected_ids),
            "normalization_source": "docs/platform-completeness-review.md",
        },
        "areas": areas,
        "blockers": blockers,
        "criteria": criteria,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the completeness manifest.")
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--instruction", type=Path, required=True)
    parser.add_argument(
        "--review",
        type=Path,
        default=PROJECT_ROOT / "docs" / "platform-completeness-review.md",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(
            rubric_path=args.rubric,
            instruction_path=args.instruction,
            review_path=args.review,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"manifest normalization failed: {exc}", file=sys.stderr)
        return 2
    payload = json.dumps(manifest, ensure_ascii=True, indent=2) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        _atomic_write(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
