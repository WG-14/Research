from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tools import platform_completeness
from tools.platform_completeness import (
    EvidenceRunError,
    RepositoryProvenance,
    criterion_ids_sha256,
    evaluate_manifest,
    run_manifest_evidence,
    sha256_path,
    validate_evidence_argv,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8"
    )


def _runner_fixture(
    tmp_path: Path,
    *,
    criterion_count: int = 2,
    argv: list[str] | None = None,
    minimum_level: str = "E4",
) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    test_path = repository / "tests" / "test_authority.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("AUTHORITY = 'single'\n", encoding="utf-8")
    pytest_path = repository / ".venv" / "bin" / "pytest"
    pytest_path.parent.mkdir(parents=True)
    pytest_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pytest_path.chmod(0o755)
    platform_path = repository / "scripts" / "platform"
    platform_path.parent.mkdir(parents=True)
    platform_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    platform_path.chmod(0o755)

    command_argv = argv or [
        ".venv/bin/pytest",
        "-q",
        "tests/test_authority.py",
    ]
    ids = [f"X-{number:02d}" for number in range(1, criterion_count + 1)]
    criteria = []
    for criterion_id in ids:
        command_id = f"{criterion_id}-verification"
        criteria.append(
            {
                "id": criterion_id,
                "rubric_title": f"Fixture {criterion_id}",
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
                    "minimum_level": minimum_level,
                    "paths": [
                        {
                            "path": "tests/test_authority.py",
                            "sha256": sha256_path(test_path),
                        }
                    ],
                    "commands": [{"id": command_id, "argv": command_argv}],
                    "receipts": [
                        {
                            "command_id": command_id,
                            "path": f"receipts/{criterion_id}.json",
                            "sha256": None,
                        }
                    ],
                },
            }
        )
    manifest = {
        "schema_version": 1,
        "completion_policy": {
            "criterion_count": criterion_count,
            "required_score": 5,
            "required_capability_status": "supported",
            "allow_not_applicable": False,
            "blocker_ids": [],
        },
        "rubric": {
            "source_sha256": "a" * 64,
            "criterion_ids_sha256": criterion_ids_sha256(ids),
        },
        "areas": [
            {
                "id": "X",
                "name": "Fixture",
                "weight": 100,
                "criterion_ids": ids,
            }
        ],
        "blockers": [],
        "criteria": criteria,
    }
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path, repository


def _stable_provenance(_repository: Path) -> RepositoryProvenance:
    return RepositoryProvenance(commit="b" * 40, dirty_diff_sha256="c" * 64)


def test_runner_groups_commands_redacts_secrets_and_emits_valid_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, repository = _runner_fixture(tmp_path)
    evidence_root = tmp_path / "evidence"
    original_manifest = manifest_path.read_bytes()
    calls: list[tuple[list[str], dict[str, Any]]] = []
    secret = "runner-secret-value"
    monkeypatch.setenv("INTERNAL_WEB_SECRET_KEY", secret)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--deselect=tests/test_authority.py")
    monkeypatch.setattr(
        platform_completeness, "_repository_provenance", _stable_provenance
    )

    def fake_run(
        command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        assert kwargs["cwd"] == str(repository)
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 12.0
        assert "PYTEST_ADDOPTS" not in kwargs["env"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"2 passed with {secret}\n".encode(),
            stderr=f"diagnostic {secret}\n".encode(),
        )

    monkeypatch.setattr(platform_completeness.subprocess, "run", fake_run)

    result = run_manifest_evidence(
        manifest_path,
        repository_root=repository,
        evidence_root=evidence_root,
        timeout_seconds=12.0,
    )

    assert result.complete is True
    assert result.command_count == 2
    assert result.execution_count == len(calls) == 1
    assert manifest_path.read_bytes() == original_manifest
    resolved = json.loads(result.resolved_manifest.read_text(encoding="utf-8"))
    receipt_hashes = [
        criterion["evidence"]["receipts"][0]["sha256"]
        for criterion in resolved["criteria"]
    ]
    assert all(isinstance(value, str) and len(value) == 64 for value in receipt_hashes)
    receipts = [
        json.loads((evidence_root / f"receipts/X-0{number}.json").read_text())
        for number in (1, 2)
    ]
    assert receipts[0]["command_group_id"] == receipts[1]["command_group_id"]
    stdout = (evidence_root / receipts[0]["stdout_path"]).read_text()
    stderr = (evidence_root / receipts[0]["stderr_path"]).read_text()
    assert secret not in stdout + stderr
    assert "<redacted>" in stdout + stderr
    ledger = json.loads(result.ledger_json.read_text(encoding="utf-8"))
    assert ledger["execution_policy"]["shell"] is False
    assert ledger["execution_policy"]["unique_command_count"] == 1
    assert (
        ledger["execution_policy"]["environment"]["INTERNAL_WEB_SECRET_KEY"]
        == "<redacted>"
    )
    assert result.ledger_markdown.is_file()
    assert evaluate_manifest(
        result.resolved_manifest,
        repository_root=repository,
        evidence_root=evidence_root,
    ).complete

    (evidence_root / receipts[0]["stdout_path"]).write_text("tampered\n")
    tampered = evaluate_manifest(
        result.resolved_manifest,
        repository_root=repository,
        evidence_root=evidence_root,
    )
    assert "receipt_stdout_hash_mismatch" in {
        finding.code for finding in tampered.findings
    }


@pytest.mark.parametrize(
    "argv",
    [
        ("/bin/sh", "-c", "true"),
        (".venv/bin/pytest", "--deselect", "tests/test_authority.py"),
        (".venv/bin/pytest", "-q", "../../etc/passwd.py"),
        ("scripts/platform", "build"),
    ],
)
def test_runner_rejects_non_allowlisted_argv_before_execution(
    tmp_path: Path,
    argv: tuple[str, ...],
) -> None:
    _manifest_path, repository = _runner_fixture(tmp_path)

    with pytest.raises(EvidenceRunError):
        validate_evidence_argv(argv, repository_root=repository)


def test_runner_accepts_only_exact_known_platform_verification_subcommand(
    tmp_path: Path,
) -> None:
    _manifest_path, repository = _runner_fixture(tmp_path)

    validate_evidence_argv(("scripts/platform", "test-all"), repository_root=repository)
    with pytest.raises(EvidenceRunError):
        validate_evidence_argv(
            ("scripts/platform", "test-all", "unexpected"),
            repository_root=repository,
        )


def test_platform_exposes_exact_verify_complete_entrypoint() -> None:
    script = (PROJECT_ROOT / "scripts" / "platform").read_text(encoding="utf-8")
    documentation = (
        PROJECT_ROOT / "docs" / "platform-completeness-evidence-runner.md"
    ).read_text(encoding="utf-8")

    assert "verify-complete)" in script
    assert 'python tools/platform_completeness.py "$@"' in script
    assert "scripts/platform verify-complete" in documentation


def test_runner_rejects_repository_internal_or_stale_evidence_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, repository = _runner_fixture(tmp_path, criterion_count=1)

    def forbidden_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("subprocess must not run when preflight rejects evidence")

    monkeypatch.setattr(platform_completeness.subprocess, "run", forbidden_run)
    with pytest.raises(EvidenceRunError, match="repository-external"):
        run_manifest_evidence(
            manifest_path,
            repository_root=repository,
            evidence_root=repository / "evidence",
        )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["criteria"][0]["evidence"]["paths"][0]["sha256"] = "f" * 64
    _write_json(manifest_path, manifest)
    with pytest.raises(EvidenceRunError, match="hash is stale"):
        run_manifest_evidence(
            manifest_path,
            repository_root=repository,
            evidence_root=tmp_path / "external-evidence",
        )

    manifest["criteria"][0]["evidence"]["paths"][0]["sha256"] = sha256_path(
        repository / "tests" / "test_authority.py"
    )
    manifest["criteria"][0]["evidence"]["receipts"][0]["sha256"] = "a" * 64
    _write_json(manifest_path, manifest)
    with pytest.raises(EvidenceRunError, match="must remain null"):
        run_manifest_evidence(
            manifest_path,
            repository_root=repository,
            evidence_root=tmp_path / "external-evidence",
        )


def test_runner_timeout_is_receipted_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, repository = _runner_fixture(tmp_path, criterion_count=1)
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(
        platform_completeness, "_repository_provenance", _stable_provenance
    )

    def time_out(command: list[str], **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(command, timeout=1.0, output=b"partial")

    monkeypatch.setattr(platform_completeness.subprocess, "run", time_out)
    result = run_manifest_evidence(
        manifest_path,
        repository_root=repository,
        evidence_root=evidence_root,
        timeout_seconds=1.0,
    )

    assert result.complete is False
    receipt = json.loads((evidence_root / "receipts/X-01.json").read_text())
    assert receipt["exit_code"] == 124
    assert receipt["timed_out"] is True
    assert "receipt_exit_nonzero" in {
        finding.code for finding in result.evaluation.findings
    }
    assert os.stat(result.ledger_json).st_mode & 0o777 == 0o600


def test_runner_caps_repository_receipts_at_e4_for_an_e5_criterion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, repository = _runner_fixture(
        tmp_path,
        criterion_count=1,
        minimum_level="E5",
    )
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(
        platform_completeness, "_repository_provenance", _stable_provenance
    )

    def passed(
        command: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, stdout=b"1 passed\n", stderr=b"")

    monkeypatch.setattr(platform_completeness.subprocess, "run", passed)
    result = run_manifest_evidence(
        manifest_path,
        repository_root=repository,
        evidence_root=evidence_root,
        timeout_seconds=1.0,
    )

    receipt = json.loads((evidence_root / "receipts/X-01.json").read_text())
    ledger = json.loads(result.ledger_json.read_text())
    assert receipt["evidence_level"] == "E4"
    assert receipt["kind"] == "repository_verification"
    assert ledger["execution_policy"]["maximum_issued_evidence_level"] == "E4"
    assert result.evaluation.criteria[0].evidence_level == "E0"
    assert {finding.code for finding in result.evaluation.findings} >= {
        "receipt_level_insufficient",
        "external_attestation_required",
    }


@pytest.mark.parametrize(
    ("summary", "expected_outcome"),
    [
        (b"1 passed, 1 skipped in 0.01s\n", "pytest_skipped"),
        (b"1 passed, 1 xfailed in 0.01s\n", "pytest_xfailed"),
        (b"1 passed, 1 xpassed in 0.01s\n", "pytest_xpassed"),
        (b"1 passed, 1 deselected in 0.01s\n", "pytest_deselected"),
        (b"no tests ran in 0.01s\n", "pytest_no_tests"),
        (b"collected 0 items\n", "pytest_no_tests"),
    ],
)
def test_runner_rejects_nonexecuted_pytest_outcomes_even_with_exit_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary: bytes,
    expected_outcome: str,
) -> None:
    manifest_path, repository = _runner_fixture(tmp_path, criterion_count=1)
    evidence_root = tmp_path / "evidence"
    monkeypatch.setattr(
        platform_completeness, "_repository_provenance", _stable_provenance
    )

    def reported(
        command: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, stdout=summary, stderr=b"")

    monkeypatch.setattr(platform_completeness.subprocess, "run", reported)
    result = run_manifest_evidence(
        manifest_path,
        repository_root=repository,
        evidence_root=evidence_root,
        timeout_seconds=1.0,
    )

    receipt = json.loads((evidence_root / "receipts/X-01.json").read_text())
    assert receipt["exit_code"] == 0
    assert receipt["evidence_level"] == "E0"
    assert receipt["evidence_eligible"] is False
    assert expected_outcome in receipt["disqualifying_outcomes"]
    assert result.complete is False
    assert "test_output_disqualified" in {
        finding.code for finding in result.runner_findings
    }
    assert "receipt_level_insufficient" in {
        finding.code for finding in result.evaluation.findings
    }
