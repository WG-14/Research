from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.platform_completeness import (
    criterion_ids_sha256,
    evaluate_manifest,
    main,
    render_report,
    sha256_path,
)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )


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


def test_manifest_rejects_duplicate_json_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = tmp_path / "duplicate.json"
    manifest_path.write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8"
    )

    assert main(["--manifest", str(manifest_path), "--quiet"]) == 2
    assert "duplicate JSON key" in capsys.readouterr().err
