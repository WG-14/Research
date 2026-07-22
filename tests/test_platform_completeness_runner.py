from __future__ import annotations

import hashlib
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


def test_runner_targets_only_full_criteria_and_pass_blockers(tmp_path: Path) -> None:
    manifest_path, repository = _runner_fixture(tmp_path, criterion_count=1)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    full = manifest["criteria"][0]
    full["id"] = "S1-C01"
    full["current_assessment"] = {"status": "FULL"}
    partial = {
        "id": "S1-C02",
        "current_assessment": {"status": "PARTIAL"},
    }
    pass_blocker = {
        "id": "B-01",
        "current_status": "PASS",
        "evidence": json.loads(json.dumps(full["evidence"])),
    }
    pass_blocker["evidence"]["commands"][0]["id"] = "B-01-verification"
    pass_blocker["evidence"]["receipts"][0].update(
        {
            "command_id": "B-01-verification",
            "path": "receipts/B-01.json",
        }
    )
    manifest["criteria"] = [full, partial]
    manifest["blockers"] = [
        pass_blocker,
        {"id": "B-02", "current_status": "FAIL"},
    ]
    manifest["schema_version"] = 2
    manifest["canonical_source"] = {
        "sha256": "a" * 64,
        "blocker_count": 19,
    }

    commands = platform_completeness._prepare_evidence_commands(
        manifest=manifest,
        repository_root=repository,
        evidence_root=tmp_path / "evidence",
    )

    assert [(item.subject, item.command_id) for item in commands] == [
        ("S1-C01", "X-01-verification"),
        ("B-01", "B-01-verification"),
    ]
    resolved = platform_completeness._resolved_manifest_payload(
        manifest=manifest,
        receipt_hashes={
            ("S1-C01", "X-01-verification"): "d" * 64,
            ("B-01", "B-01-verification"): "e" * 64,
        },
    )
    assert resolved["criteria"][0]["evidence"]["receipts"][0]["sha256"] == ("d" * 64)
    assert "evidence" not in resolved["criteria"][1]
    assert resolved["blockers"][0]["evidence"]["receipts"][0]["sha256"] == ("e" * 64)
    assert "evidence" not in resolved["blockers"][1]


def test_repository_provenance_binds_tracked_deletion_with_explicit_sentinel(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "research@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Research Test"],
        cwd=repository,
        check=True,
    )
    tracked = repository / "tracked.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgSign=false", "commit", "-q", "-m", "fixture"],
        cwd=repository,
        check=True,
    )
    tracked.unlink()

    provenance = platform_completeness._repository_provenance(repository)

    expected = hashlib.sha256()
    expected.update(b"tracked.txt\0")
    expected.update(platform_completeness._REPOSITORY_TRACKED_DELETION_SENTINEL)
    expected.update(b"\0")
    assert provenance.dirty_diff_sha256 == expected.hexdigest()


def test_repository_provenance_does_not_treat_untracked_missing_path_as_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_git_bytes(_repository: Path, *args: str) -> bytes:
        if args == ("rev-parse", "HEAD"):
            return b"b" * 40 + b"\n"
        if args == (
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ):
            return b"vanished-untracked.txt\0"
        if args == ("ls-files", "--deleted", "-z"):
            return b""
        raise AssertionError(args)

    monkeypatch.setattr(platform_completeness, "_git_bytes", fake_git_bytes)

    with pytest.raises(EvidenceRunError, match="repository evidence path is unsafe"):
        platform_completeness._repository_provenance(tmp_path)


@pytest.mark.parametrize("relative", ("../outside.txt", "dangling-link.txt"))
def test_repository_provenance_rejects_unsafe_path_even_if_reported_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
) -> None:
    if relative == "dangling-link.txt":
        (tmp_path / relative).symlink_to(tmp_path / "missing-target.txt")

    encoded = relative.encode("utf-8") + b"\0"

    def fake_git_bytes(_repository: Path, *args: str) -> bytes:
        if args == ("rev-parse", "HEAD"):
            return b"b" * 40 + b"\n"
        if args == (
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ):
            return encoded
        if args == ("ls-files", "--deleted", "-z"):
            return encoded
        raise AssertionError(args)

    monkeypatch.setattr(platform_completeness, "_git_bytes", fake_git_bytes)

    with pytest.raises(EvidenceRunError, match="repository evidence path is unsafe"):
        platform_completeness._repository_provenance(tmp_path)


def test_runner_groups_commands_redacts_secrets_and_emits_valid_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, repository = _runner_fixture(tmp_path)
    evidence_root = tmp_path / "evidence"
    original_manifest = manifest_path.read_bytes()
    calls: list[tuple[list[str], dict[str, Any]]] = []
    secret = "runner-secret-value"
    monkeypatch.setenv("RUNNER_TEST_TOKEN", secret)
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
        assert (
            kwargs["env"]["DJANGO_SETTINGS_MODULE"]
            == "market_research_web.settings_test"
        )
        assert "INTERNAL_WEB_SECRET_KEY" not in kwargs["env"]
        assert "RUNNER_TEST_TOKEN" not in kwargs["env"]
        assert set(kwargs["env"]) <= (
            set(platform_completeness._SAFE_INHERITED_ENVIRONMENT_KEYS)
            | set(platform_completeness._DETERMINISTIC_ENVIRONMENT)
            | {"PYTHONPYCACHEPREFIX", "TEMP", "TMP", "TMPDIR"}
        )
        assert not Path(kwargs["env"]["TMPDIR"]).is_relative_to(evidence_root)
        assert Path(kwargs["env"]["TMPDIR"]).is_dir()
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
    assert all(receipt["schema_version"] == 2 for receipt in receipts)
    assert all(
        set(receipt)
        == set(platform_completeness._REPOSITORY_VERIFICATION_RECEIPT_FIELDS)
        for receipt in receipts
    )
    assert all(
        "RUNNER_TEST_TOKEN" in receipt["secret_environment_keys_removed"]
        for receipt in receipts
    )
    assert receipts[0]["command_group_id"] == receipts[1]["command_group_id"]
    stdout = (evidence_root / receipts[0]["stdout_path"]).read_text()
    stderr = (evidence_root / receipts[0]["stderr_path"]).read_text()
    assert secret not in stdout + stderr
    assert "<redacted>" in stdout + stderr
    ledger = json.loads(result.ledger_json.read_text(encoding="utf-8"))
    assert ledger["execution_policy"]["shell"] is False
    assert ledger["execution_policy"]["unique_command_count"] == 1
    assert "INTERNAL_WEB_SECRET_KEY" not in ledger["execution_policy"]["environment"]
    assert "RUNNER_TEST_TOKEN" not in ledger["execution_policy"]["environment"]
    assert (
        "RUNNER_TEST_TOKEN"
        in ledger["execution_policy"]["secret_environment_keys_removed"]
    )
    assert (
        ledger["execution_policy"]["environment"]["DJANGO_SETTINGS_MODULE"]
        == "market_research_web.settings_test"
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


def test_runner_receipt_is_invalidated_by_current_checkout_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, repository = _runner_fixture(tmp_path)
    evidence_root = tmp_path / "evidence"
    current = [_stable_provenance(repository)]
    provenance_calls = 0

    def changing_provenance(_repository: Path) -> RepositoryProvenance:
        nonlocal provenance_calls
        provenance_calls += 1
        return current[0]

    def passed(
        command: list[str], **_kwargs: Any
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, stdout=b"2 passed\n", stderr=b"")

    monkeypatch.setattr(
        platform_completeness, "_repository_provenance", changing_provenance
    )
    monkeypatch.setattr(platform_completeness.subprocess, "run", passed)
    result = run_manifest_evidence(
        manifest_path,
        repository_root=repository,
        evidence_root=evidence_root,
        timeout_seconds=1.0,
    )

    assert result.complete is True
    assert provenance_calls == 3
    current[0] = RepositoryProvenance(
        commit=current[0].commit,
        dirty_diff_sha256="d" * 64,
    )
    stale = evaluate_manifest(
        result.resolved_manifest,
        repository_root=repository,
        evidence_root=evidence_root,
    )

    assert provenance_calls == 4
    assert stale.complete is False
    assert "repository_receipt_dirty_diff_mismatch" in {
        finding.code for finding in stale.findings
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("remove_stdout_path", "repository_receipt_fields_mismatch"),
        ("wrong_group", "repository_receipt_group_mismatch"),
        ("ineligible", "repository_receipt_ineligible"),
        ("disqualified", "repository_receipt_disqualified"),
        ("downgrade_kind", "repository_verification_receipt_required"),
    ],
)
def test_repository_verification_receipt_schema_fails_closed_on_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_code: str,
) -> None:
    manifest_path, repository = _runner_fixture(tmp_path, criterion_count=1)
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
    receipt_path = evidence_root / "receipts" / "X-01.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if mutation == "remove_stdout_path":
        receipt.pop("stdout_path")
    elif mutation == "wrong_group":
        receipt["command_group_id"] = "0" * 24
    elif mutation == "ineligible":
        receipt["evidence_eligible"] = False
    elif mutation == "downgrade_kind":
        receipt["kind"] = "test"
        receipt["schema_version"] = 1
    else:
        receipt["disqualifying_outcomes"] = ["pytest_skipped"]
    _write_json(receipt_path, receipt)
    resolved = json.loads(result.resolved_manifest.read_text(encoding="utf-8"))
    resolved["criteria"][0]["evidence"]["receipts"][0]["sha256"] = sha256_path(
        receipt_path
    )
    _write_json(result.resolved_manifest, resolved)

    evaluation = evaluate_manifest(
        result.resolved_manifest,
        repository_root=repository,
        evidence_root=evidence_root,
    )

    assert evaluation.complete is False
    assert expected_code in {finding.code for finding in evaluation.findings}


def test_evidence_writer_rejects_parent_and_leaf_symlinks(tmp_path: Path) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (evidence_root / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(EvidenceRunError, match="evidence parent is unsafe"):
        platform_completeness._atomic_write_evidence(
            evidence_root / "logs" / "command.log",
            b"safe\n",
            evidence_root=evidence_root,
        )
    assert list(outside.iterdir()) == []

    (evidence_root / "logs").unlink()
    receipts = evidence_root / "receipts"
    receipts.mkdir(mode=0o700)
    outside_file = outside / "protected.txt"
    outside_file.write_bytes(b"protected\n")
    (receipts / "X-01.json").symlink_to(outside_file)
    with pytest.raises(EvidenceRunError, match="existing evidence file is unsafe"):
        platform_completeness._atomic_write_evidence(
            receipts / "X-01.json",
            b"replacement\n",
            evidence_root=evidence_root,
        )
    assert outside_file.read_bytes() == b"protected\n"


def test_evidence_writer_create_or_verify_is_atomic_and_immutable(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    target = evidence_root / "receipts" / "X-01.json"

    platform_completeness._atomic_write_evidence(
        target, b"receipt\n", evidence_root=evidence_root
    )
    platform_completeness._atomic_write_evidence(
        target, b"receipt\n", evidence_root=evidence_root
    )

    assert target.read_bytes() == b"receipt\n"
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(EvidenceRunError, match="content differs"):
        platform_completeness._atomic_write_evidence(
            target, b"changed\n", evidence_root=evidence_root
        )
    assert target.read_bytes() == b"receipt\n"


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
    documentation = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "verify-complete)" in script
    assert "python tools/update_reference_audit.py --check" in script
    assert "python tools/render_reference_audit_report.py --check" in script
    assert 'python tools/reference_audit.py "$@"' in script
    assert "verify-product-scope)" in script
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
