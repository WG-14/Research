from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.platform_completeness import (
    DEFAULT_MANIFEST,
    DEFAULT_REPORT,
    criterion_ids_sha256,
    evaluate_manifest,
    main,
    render_report,
    sha256_path,
)


RUBRIC_SHA256 = "5534d1a9863e6b8d95513a1e7f6d4b8faeb3e6fa4203d556e7478e2cfc395e8f"
AREA_COUNTS = {
    "R": (5, 6),
    "D": (6, 8),
    "L": (4, 5),
    "DA": (7, 11),
    "P": (8, 9),
    "E": (7, 9),
    "BT": (10, 13),
    "V": (6, 11),
    "S": (6, 8),
    "M": (5, 8),
    "MON": (5, 7),
    "K": (4, 7),
    "UX": (4, 8),
    "SEC": (4, 7),
    "OPS": (5, 10),
    "T": (8, 15),
    "A": (6, 11),
}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )


def _literal_declared_score(manifest: dict[str, Any]) -> float:
    criteria = {item["id"]: item for item in manifest["criteria"]}
    total = 0.0
    for area in manifest["areas"]:
        values = [criteria[item]["declared_score"] for item in area["criterion_ids"]]
        assert all(isinstance(value, int) for value in values)
        total += (sum(values) / len(values)) / 5.0 * area["weight"]
    return total


def _valid_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repository_root = tmp_path / "repository"
    evidence_root = tmp_path / "evidence"
    source = repository_root / "src" / "authority.py"
    source.parent.mkdir(parents=True)
    source.write_text("AUTHORITY = 'single'\n", encoding="utf-8")
    source_hash = sha256_path(source)
    rubric_hash = "a" * 64
    argv = ["pytest", "-q", "tests/test_authority.py"]
    receipt = {
        "schema_version": 1,
        "criterion_id": "X-01",
        "rubric_sha256": rubric_hash,
        "command_id": "X-01-verification",
        "argv": argv,
        "exit_code": 0,
        "kind": "test",
        "evidence_level": "E4",
        "started_at": "2026-07-17T00:00:00Z",
        "finished_at": "2026-07-17T00:00:01Z",
        "repository_commit": "b" * 40,
        "dirty_diff_sha256": "c" * 64,
        "stdout_sha256": "d" * 64,
        "path_hashes": {"src/authority.py": source_hash},
    }
    receipt_path = evidence_root / "receipts" / "X-01.json"
    _write_json(receipt_path, receipt)
    manifest = {
        "schema_version": 1,
        "completion_policy": {
            "criterion_count": 1,
            "required_score": 5,
            "required_capability_status": "supported",
            "allow_not_applicable": False,
            "blocker_ids": [],
        },
        "rubric": {
            "source_sha256": rubric_hash,
            "criterion_ids_sha256": criterion_ids_sha256(["X-01"]),
        },
        "areas": [
            {
                "id": "X",
                "name": "Fixture",
                "weight": 100,
                "criterion_ids": ["X-01"],
            }
        ],
        "blockers": [],
        "criteria": [
            {
                "id": "X-01",
                "rubric_title": "Fixture authority is singular",
                "rubric_section_sha256": "e" * 64,
                "acceptance": "One authority exists.",
                "verification_expectation": "Focused authority test.",
                "priority_and_risk": "P0 / Critical",
                "area_id": "X",
                "area_weight": 100,
                "required_score": 5,
                "declared_score": 5,
                "capability_status": "supported",
                "evidence": {
                    "minimum_level": "E4",
                    "paths": [{"path": "src/authority.py", "sha256": source_hash}],
                    "commands": [{"id": "X-01-verification", "argv": argv}],
                    "receipts": [
                        {
                            "command_id": "X-01-verification",
                            "path": "receipts/X-01.json",
                            "sha256": sha256_path(receipt_path),
                        }
                    ],
                },
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path, repository_root, evidence_root, source


def test_checked_in_manifest_is_exact_and_keeps_all_153_required_criteria() -> None:
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    criteria = manifest["criteria"]
    ids = [criterion["id"] for criterion in criteria]
    expected_ids = [
        f"{prefix}-{number:02d}"
        for prefix, (_weight, count) in AREA_COUNTS.items()
        for number in range(1, count + 1)
    ]

    assert manifest["schema_version"] == 1
    assert manifest["rubric"]["source_sha256"] == RUBRIC_SHA256
    assert ids == expected_ids
    assert len(ids) == len(set(ids)) == 153
    assert manifest["rubric"]["criterion_ids_sha256"] == criterion_ids_sha256(ids)
    assert sum(area["weight"] for area in manifest["areas"]) == 100
    assert manifest["completion_policy"] == {
        "criterion_count": 153,
        "required_score": 5,
        "required_capability_status": "supported",
        "allow_not_applicable": False,
        "blocker_ids": [f"B-{number:02d}" for number in range(1, 9)],
        "missing_or_invalid_evidence_is_failure": True,
    }
    for criterion in criteria:
        assert criterion["required_score"] == 5
        assert criterion["rubric_title"]
        assert len(criterion["rubric_section_sha256"]) == 64
        assert set(criterion["evidence"]) == {
            "minimum_level",
            "paths",
            "commands",
            "receipts",
        }
        assert criterion["evidence"]["minimum_level"] in {"E4", "E5"}
        for path in criterion["evidence"]["paths"]:
            assert len(path["sha256"]) == 64
        for receipt in criterion["evidence"]["receipts"]:
            assert set(receipt) == {"command_id", "path", "sha256"}
            assert receipt["sha256"] is None
        if criterion["capability_status"] == "supported":
            assert criterion["evidence"]["paths"]
            assert criterion["evidence"]["commands"]
            assert criterion["evidence"]["receipts"]
    ai_advisory = next(item for item in criteria if item["id"] == "A-10")
    assert ai_advisory["capability_status"] == "supported"
    assert ai_advisory["declared_score"] == 5
    assert all(criterion["declared_score"] is not None for criterion in criteria)
    assert [item["path"] for item in ai_advisory["evidence"]["paths"]] == [
        "src/market_research/research/knowledge_contract.py",
        "src/market_research/research/knowledge_registry.py",
        "tests/test_ai_advisory_contract.py",
        "docs/investment-research-platform.md",
    ]
    criterion_map = {item["id"]: item for item in criteria}
    assert criterion_map["DA-04"]["evidence"]["commands"][0]["argv"] == [
        ".venv/bin/pytest",
        "-q",
        "tests/test_instrument_domain_contracts.py",
    ]
    assert criterion_map["UX-06"]["evidence"]["commands"][0]["argv"] == [
        ".venv/bin/pytest",
        "-q",
        "apps/internal_web/tests/test_api_contract.py",
    ]
    assert criterion_map["T-08"]["evidence"]["commands"][0]["argv"] == [
        ".venv/bin/pytest",
        "-q",
        "apps/internal_web/tests/test_browser_e2e.py",
        "apps/internal_web/tests/test_jobs_worker.py",
        "apps/internal_web/tests/test_api_contract.py",
    ]
    assert criterion_map["T-09"]["evidence"]["commands"][0]["argv"] == [
        ".venv/bin/pytest",
        "-q",
        "apps/internal_web/tests/test_api_contract.py",
    ]
    for criterion_id, focused_selector in {
        "D-02": "tests/test_instrument_domain_contracts.py",
        "DA-02": "tests/test_dataset_artifact_manifest_contract.py",
        "E-06": "tests/test_common_engine_failure_audit.py",
        "E-09": "tests/test_application_report_comparison.py",
        "V-06": "tests/test_result_concentration.py",
        "OPS-06": "services/research_operations/tests/test_service_alert_unit.py",
        "A-08": "tests/test_instrument_domain_contracts.py",
        "A-09": "tests/test_instrument_domain_contracts.py",
    }.items():
        assert (
            focused_selector
            in criterion_map[criterion_id]["evidence"]["commands"][0]["argv"]
        )


def test_current_repository_fails_closed_without_inventing_receipts() -> None:
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    evaluation = evaluate_manifest(DEFAULT_MANIFEST)
    codes = {(finding.subject, finding.code) for finding in evaluation.findings}

    assert evaluation.complete is False
    assert evaluation.expected_criteria == 153
    assert evaluation.verified_criteria == 0
    assert evaluation.declared_score == pytest.approx(_literal_declared_score(manifest))
    assert ("D-02", "score_below_required") in codes
    assert ("M-01", "capability_not_supported") in codes
    assert ("A-10", "score_missing") not in codes
    assert ("A-10", "capability_not_supported") not in codes
    assert ("B-06", "blocker_not_cleared") in codes
    assert ("B-08", "blocker_not_cleared") in codes
    assert ("R-01", "receipt_hash_missing") in codes
    assert ("M-01", "evidence_paths_missing") in codes
    assert not any(
        finding.code
        in {
            "evidence_path_missing",
            "evidence_path_hash_missing",
            "evidence_path_hash_mismatch",
        }
        for finding in evaluation.findings
    )
    assert main(["--manifest", str(DEFAULT_MANIFEST), "--quiet"]) == 1


def test_valid_hash_bound_fixture_is_complete(tmp_path: Path) -> None:
    manifest, repository_root, evidence_root, _source = _valid_fixture(tmp_path)

    evaluation = evaluate_manifest(
        manifest,
        repository_root=repository_root,
        evidence_root=evidence_root,
    )

    assert evaluation.findings == ()
    assert evaluation.complete is True
    assert evaluation.declared_score == 100.0
    assert evaluation.verified_criteria == 1


def test_status_report_exposes_per_criterion_evidence_catalog(tmp_path: Path) -> None:
    manifest, repository_root, evidence_root, source = _valid_fixture(tmp_path)

    evaluation = evaluate_manifest(
        manifest,
        repository_root=repository_root,
        evidence_root=evidence_root,
    )
    report = render_report(evaluation)
    result = evaluation.criteria[0]

    assert result.required_evidence_level == "E4"
    assert result.evidence_paths == (("src/authority.py", sha256_path(source)),)
    assert result.verification_commands == (
        (
            "X-01-verification",
            ("pytest", "-q", "tests/test_authority.py"),
        ),
    )
    assert result.receipt_bindings == (
        (
            "X-01-verification",
            "receipts/X-01.json",
            sha256_path(evidence_root / "receipts" / "X-01.json"),
        ),
    )
    assert "## Criterion evidence catalog" in report
    assert (
        "| Criterion | Final score | Production / implementation evidence paths"
        in report
    )
    assert "| X-01 | 5/5 |" in report
    assert "`src/authority.py`" in report
    assert '`["pytest", "-q", "tests/test_authority.py"]`' in report
    assert "`E4` (required `E4`)" in report
    assert "`receipts/X-01.json`" in report
    assert report.count("`src/authority.py`") == 2
    assert report.count("| X-01 |") == 2


def test_status_report_catalog_covers_every_required_criterion() -> None:
    evaluation = evaluate_manifest(DEFAULT_MANIFEST)
    report = render_report(evaluation)
    catalog = report.split("## Criterion evidence catalog", maxsplit=1)[1].split(
        "## Findings", maxsplit=1
    )[0]
    rows = [
        line
        for line in catalog.splitlines()
        if any(
            line.startswith(f"| {item.criterion_id} |") for item in evaluation.criteria
        )
    ]

    assert len(evaluation.criteria) == evaluation.expected_criteria == 153
    assert len(rows) == 153
    assert [row.removeprefix("| ").split(" | ", maxsplit=1)[0] for row in rows] == [
        item.criterion_id for item in evaluation.criteria
    ]
    assert "`receipt_hash_missing`" in catalog
    assert "hash missing" in catalog


def test_path_or_receipt_tampering_fails_the_gate(tmp_path: Path) -> None:
    manifest, repository_root, evidence_root, source = _valid_fixture(tmp_path)
    source.write_text("AUTHORITY = 'changed'\n", encoding="utf-8")

    evaluation = evaluate_manifest(
        manifest,
        repository_root=repository_root,
        evidence_root=evidence_root,
    )

    assert evaluation.complete is False
    assert "evidence_path_hash_mismatch" in {
        finding.code for finding in evaluation.findings
    }
    assert "receipt_paths_mismatch" in {finding.code for finding in evaluation.findings}


def test_e5_requires_an_external_site_or_organization_attestation(
    tmp_path: Path,
) -> None:
    manifest_path, repository_root, evidence_root, _source = _valid_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["criteria"][0]["evidence"]["minimum_level"] = "E5"
    _write_json(manifest_path, manifest)

    evaluation = evaluate_manifest(
        manifest_path,
        repository_root=repository_root,
        evidence_root=evidence_root,
    )

    codes = {finding.code for finding in evaluation.findings}
    assert "receipt_level_insufficient" in codes
    assert "external_attestation_required" in codes


def test_repository_pytest_receipt_cannot_self_promote_to_e5(tmp_path: Path) -> None:
    manifest_path, repository_root, evidence_root, _source = _valid_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt_path = evidence_root / "receipts" / "X-01.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    attestation_path = evidence_root / "attestations" / "site.json"
    _write_json(attestation_path, {"site": "research-validation-lab"})
    manifest["criteria"][0]["evidence"]["minimum_level"] = "E5"
    receipt["evidence_level"] = "E5"
    receipt["kind"] = "external_attestation"
    receipt["external_attestation"] = {
        "schema_version": 1,
        "scope": "site_or_organization",
        "issuer": "independent-validation-team",
        "site_id": "research-validation-lab",
        "issued_at": "2026-07-17T00:00:02Z",
        "path": "attestations/site.json",
        "sha256": sha256_path(attestation_path),
    }
    _write_json(receipt_path, receipt)
    manifest["criteria"][0]["evidence"]["receipts"][0]["sha256"] = sha256_path(
        receipt_path
    )
    _write_json(manifest_path, manifest)

    evaluation = evaluate_manifest(
        manifest_path,
        repository_root=repository_root,
        evidence_root=evidence_root,
    )

    assert evaluation.complete is False
    assert evaluation.criteria[0].evidence_level == "E0"
    assert "repository_command_e5_forbidden" in {
        finding.code for finding in evaluation.findings
    }


def test_non_repository_external_attestation_can_supply_e5(tmp_path: Path) -> None:
    manifest_path, repository_root, evidence_root, _source = _valid_fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt_path = evidence_root / "receipts" / "X-01.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    attestation_path = evidence_root / "attestations" / "site.json"
    _write_json(attestation_path, {"site": "independent-recovery-site"})
    external_argv = ["external-site-verifier", "attestation-X-01"]
    manifest["criteria"][0]["evidence"]["minimum_level"] = "E5"
    manifest["criteria"][0]["evidence"]["commands"][0]["argv"] = external_argv
    receipt["argv"] = external_argv
    receipt["evidence_level"] = "E5"
    receipt["kind"] = "external_attestation"
    receipt["external_attestation"] = {
        "schema_version": 1,
        "scope": "site_or_organization",
        "issuer": "independent-recovery-owner",
        "site_id": "independent-recovery-site",
        "issued_at": "2026-07-17T00:00:02Z",
        "path": "attestations/site.json",
        "sha256": sha256_path(attestation_path),
    }
    _write_json(receipt_path, receipt)
    manifest["criteria"][0]["evidence"]["receipts"][0]["sha256"] = sha256_path(
        receipt_path
    )
    _write_json(manifest_path, manifest)

    evaluation = evaluate_manifest(
        manifest_path,
        repository_root=repository_root,
        evidence_root=evidence_root,
    )

    assert evaluation.findings == ()
    assert evaluation.complete is True
    assert evaluation.criteria[0].evidence_level == "E5"


def test_generated_status_document_is_exactly_reproducible() -> None:
    evaluation = evaluate_manifest(DEFAULT_MANIFEST)

    assert DEFAULT_REPORT.read_text(encoding="utf-8") == render_report(evaluation)
    assert "Status: **INCOMPLETE**" in DEFAULT_REPORT.read_text(encoding="utf-8")
    assert (
        "Generated by tools/platform_completeness.py; do not edit manually."
        in DEFAULT_REPORT.read_text(encoding="utf-8")
    )
    assert main(["--check-report", "--quiet"]) == 1


def test_manifest_rejects_duplicate_json_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = tmp_path / "duplicate.json"
    manifest_path.write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8"
    )

    assert main(["--manifest", str(manifest_path), "--quiet"]) == 2
    assert "duplicate JSON key" in capsys.readouterr().err
