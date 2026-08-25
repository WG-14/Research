from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

import tools.reference_audit as reference_audit
import tools.reference_audit_receipt as receipt_tool
from tools.reference_audit_authority import (
    AUDIT_SESSION_HISTORY_RELATIVE_ROOT,
    AUDIT_SESSION_ID,
    INSTRUCTION_RELATIVE_PATH,
    RUBRIC_RELATIVE_PATH,
    validate_audit_authority,
)
from tools.reference_audit import (
    DEFAULT_MATRIX,
    DuplicateKeyError,
    _payload,
    _verdict,
    evaluate_matrix,
    load_matrix,
    main,
)
from tools.reference_audit_receipt import (
    DEFAULT_RECEIPT,
    RUBRIC_SHA256,
    INSTRUCTION_SHA256,
    build_receipt_document,
    required_test_hashes_from_matrix,
    run_and_write_receipt,
    validate_receipt,
)
from tools.reference_audit_surface import (
    AUDIT_SURFACE_SCHEMA_VERSION,
    audit_surface,
)
from tools.render_reference_audit_report import (
    REPORT_PATH,
    RESULT_PATH,
    _machine_result,
)
from tools.update_reference_audit import (
    REPOSITORY_COMMIT_ROLE,
    _differences,
    build_matrix,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CONSERVATIVE_TEMPLATE: dict[str, Any] | None = None


def _write_matrix(path: Path, payload: dict[str, object]) -> Path:
    isolated_path = path.parent / "audit-root" / "docs" / path.name
    isolated_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return isolated_path


def _conservative_matrix(tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    global _CONSERVATIVE_TEMPLATE
    if _CONSERVATIVE_TEMPLATE is None:
        _CONSERVATIVE_TEMPLATE = build_matrix(
            receipt_path=Path("/tmp/reference-audit-intentionally-missing.json")
        )
    return copy.deepcopy(_CONSERVATIVE_TEMPLATE)


def _required_tests(matrix: dict[str, Any]) -> dict[str, str]:
    return required_test_hashes_from_matrix(matrix, root=PROJECT_ROOT)


def _receipt_document(
    matrix: dict[str, Any],
) -> tuple[dict[str, object], dict[str, object], dict[str, str]]:
    surface = audit_surface(PROJECT_ROOT)
    required = _required_tests(matrix)
    document = build_receipt_document(
        source_surface=surface,
        required_tests=required,
        tests_passed=len(required),
        duration_seconds=1.25,
        output_sha256="a" * 64,
        created_at="2026-07-29T00:00:00Z",
    )
    return document, surface, required


def _rehash_receipt(document: dict[str, object]) -> None:
    payload = json.dumps(
        document["payload"],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    document["content_sha256"] = hashlib.sha256(payload).hexdigest()


def _assert_nonempty_text(value: object) -> None:
    assert isinstance(value, str)
    assert value
    assert value.strip() == value


def _assert_machine_result_schema(result: object) -> None:
    assert isinstance(result, dict)
    assert set(result) == {
        "verdict",
        "is_complete_against_reference",
        "overall_score",
        "raw_weighted_score",
        "score_cap",
        "canonical_source",
        "audit_history",
        "repository",
        "execution_receipt",
        "fatal_gates",
        "domain_scores",
        "criteria",
        "final_questions",
        "top_gaps",
        "unverified_external_dependencies",
        "commands_executed",
        "tests_failed",
        "final_reasoning",
    }
    assert result["verdict"] in {
        "COMPLETE",
        "NEAR_COMPLETE",
        "SUBSTANTIALLY_COMPLETE_BUT_INCOMPLETE",
        "FUNCTIONAL_RESEARCH_PLATFORM",
        "RESEARCH_TOOLKIT",
        "PROTOTYPE",
        "NOT_AN_INVESTMENT_RESEARCH_PLATFORM",
    }
    assert isinstance(result["is_complete_against_reference"], bool)
    for key in ("overall_score", "raw_weighted_score", "score_cap"):
        assert isinstance(result[key], (int, float))
        assert not isinstance(result[key], bool)

    source = result["canonical_source"]
    assert isinstance(source, dict)
    assert source["sha256"] == RUBRIC_SHA256
    assert source["instruction_sha256"] == INSTRUCTION_SHA256
    assert source["criterion_count"] == 184
    assert source["fatal_gate_count"] == 12

    audit_history = result["audit_history"]
    assert audit_history["session_id"] == AUDIT_SESSION_ID
    iteration = audit_history["iteration_count"]
    assert 1 <= iteration <= 10
    assert audit_history["current_surface_iteration"] == iteration
    assert audit_history["retained_snapshot_iterations"] == list(range(1, iteration))
    assert AUDIT_SESSION_ID in audit_history["semantics"]
    assert [item["iteration"] for item in audit_history["phases"]] == list(
        range(1, iteration + 1)
    )
    assert all(
        item["history_kind"] == "retained_assessment_snapshot"
        for item in audit_history["phases"][:-1]
    )
    assert audit_history["phases"][-1]["history_kind"] == (
        "current_surface_reassessment"
    )

    repository = result["repository"]
    assert isinstance(repository, dict)
    assert set(repository) == {
        "root",
        "commit",
        "commit_role",
        "branch",
        "dirty",
        "assessment_surface",
        "primary_languages",
        "entrypoints",
        "test_commands",
    }
    assert Path(repository["root"]).is_absolute()
    assert repository["commit_role"] == REPOSITORY_COMMIT_ROLE
    assert len(repository["commit"]) == 40
    assert isinstance(repository["dirty"], bool)
    surface = repository["assessment_surface"]
    assert isinstance(surface, dict)
    assert surface["schema_version"] == AUDIT_SURFACE_SCHEMA_VERSION
    assert surface["file_count"] > 0
    assert len(surface["sha256"]) == 64
    assert (
        "file:docs/investment-research-platform-audit-execution-receipt.json"
        in surface["exclusions"]
    )

    receipt = result["execution_receipt"]
    assert isinstance(receipt, dict)
    assert set(receipt) == {
        "path",
        "status",
        "clean_local_run",
        "trusted",
        "trust_level",
        "content_sha256",
        "required_target_count",
        "tests_passed",
        "findings",
    }
    assert receipt["status"] in {
        "VALID_LOCAL_SELF_ATTESTED",
        "MISSING",
        "STALE",
        "INVALID",
    }
    assert isinstance(receipt["clean_local_run"], bool)
    assert receipt["trusted"] is False
    assert receipt["trust_level"] in {"LOCAL_SELF_ATTESTED", "NONE"}
    assert receipt["required_target_count"] > 0
    assert isinstance(receipt["findings"], list)

    gates = result["fatal_gates"]
    assert len(gates) == 12
    assert {gate["id"] for gate in gates} == {
        f"FG-{number:02d}" for number in range(1, 13)
    }
    for gate in gates:
        assert set(gate) == {
            "id",
            "status",
            "evidence",
            "verification_method",
            "impact",
            "mitigation_possible",
            "required_remediation",
        }
        assert gate["status"] in {"PASS", "FAIL", "UNVERIFIED"}

    assert set(result["domain_scores"]) == {
        "scope_boundary",
        "data",
        "reproducibility",
        "research_lifecycle",
        "backtesting_simulation",
        "validation",
        "review_governance",
        "artifacts_knowledge",
        "security_observability",
        "architecture_usability",
    }
    criteria = result["criteria"]
    assert len(criteria) == 184
    assert len({criterion["id"] for criterion in criteria}) == 184
    for criterion in criteria:
        assert set(criterion) == {
            "id",
            "importance",
            "maturity",
            "status",
            "evidence",
            "gap",
            "required_remediation",
        }
        assert criterion["importance"] in {"CRITICAL", "MAJOR", "SUPPORTING"}
        assert criterion["maturity"] in {f"M{rank}" for rank in range(6)}
        assert criterion["status"] in {
            "VERIFIED",
            "VERIFIED_LOCAL_SELF_ATTESTED",
            "IMPLEMENTED_NOT_VERIFIED",
            "PARTIAL",
            "DOCUMENTATION_ONLY",
            "PLACEHOLDER",
            "MISSING",
            "OUT_OF_SCOPE_VIOLATION",
            "UNVERIFIED_EXTERNAL",
        }
        assert criterion["evidence"]
        _assert_nonempty_text(criterion["gap"])
        _assert_nonempty_text(criterion["required_remediation"])

    questions = result["final_questions"]
    assert [question["number"] for question in questions] == list(range(1, 16))
    for question in questions:
        assert set(question) == {"number", "answer", "evidence", "explanation"}
        assert question["answer"] in {"YES", "PARTIAL", "NO", "UNVERIFIED"}
        assert question["evidence"]

    gaps = result["top_gaps"]
    assert 1 <= len(gaps) <= 20
    for gap in gaps:
        assert set(gap) == {
            "priority",
            "criterion_ids",
            "title",
            "why_it_matters",
            "required_implementation",
            "required_tests",
            "definition_of_done",
        }
        assert gap["priority"] in {"P0", "P1", "P2", "P3"}
        assert gap["criterion_ids"]
        assert gap["required_tests"]
    assert isinstance(result["commands_executed"], list)
    assert isinstance(result["tests_failed"], list)
    _assert_nonempty_text(result["final_reasoning"])


def test_current_a_j_source_is_the_single_hash_bound_authority() -> None:
    matrix = build_matrix(receipt_path=Path("/definitely/missing/receipt.json"))

    assert matrix["canonical_source"]["sha256"] == (
        "ce507e16b37a8915ba34f12907aac3145dd512859951d391781e5a390fb675a5"
    )
    assert matrix["canonical_source"]["instruction_sha256"] == (
        "a367c42b2d13824e8a5933b7bd6369eea35903dccc8a66027c7e450b3eef4564"
    )
    assert matrix["canonical_source"]["audit_session_id"] == AUDIT_SESSION_ID
    assert matrix["canonical_source"]["criterion_count"] == 184
    assert matrix["canonical_source"]["fatal_gate_count"] == 12
    assert matrix["assessment"]["repository_commit_role"] == REPOSITORY_COMMIT_ROLE


def test_raw_attachment_authority_is_present_and_tamper_evident(
    tmp_path: Path,
) -> None:
    authority = validate_audit_authority()

    assert authority.session_id == AUDIT_SESSION_ID
    assert authority.rubric_path == RUBRIC_RELATIVE_PATH.as_posix()
    assert authority.instruction_path == INSTRUCTION_RELATIVE_PATH.as_posix()

    isolated_root = tmp_path / "isolated-authority"
    for relative in (RUBRIC_RELATIVE_PATH, INSTRUCTION_RELATIVE_PATH):
        destination = isolated_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT_ROOT / relative, destination)
    assert validate_audit_authority(project_root=isolated_root) == authority

    instruction = isolated_root / INSTRUCTION_RELATIVE_PATH
    instruction.write_bytes(instruction.read_bytes() + b"tamper")
    with pytest.raises(
        ValueError,
        match="audit_instruction_authority_hash_mismatch",
    ):
        validate_audit_authority(project_root=isolated_root)


def test_missing_receipt_caps_claims_and_fatal_gates_fail_closed(
    tmp_path: Path,
) -> None:
    matrix = _conservative_matrix(tmp_path)

    assert matrix["assessment"]["execution_receipt"]["status"] == "MISSING"
    assert all(row["status"] != "VERIFIED" for row in matrix["criteria"])
    assert all(int(row["maturity"][1:]) <= 3 for row in matrix["criteria"])
    assert all(
        entry["status"] != "VERIFIED"
        for row in matrix["criteria"]
        for entry in row["assessment_history"]
    )
    statuses = {gate["id"]: gate["status"] for gate in matrix["fatal_gates"]}
    assert all(status == "UNVERIFIED" for status in statuses.values())


def test_canonical_matrix_is_structurally_valid_and_never_complete() -> None:
    evaluation = evaluate_matrix(DEFAULT_MATRIX)

    assert evaluation.findings == ()
    assert evaluation.score <= evaluation.score_cap
    assert evaluation.fatal_failures == ()
    assert not evaluation.complete
    assert evaluation.verdict != "COMPLETE"
    if not evaluation.execution_receipt_clean_local_run:
        assert evaluation.score_cap == 75
        assert not evaluation.critical_m4_or_higher
        assert len(evaluation.fatal_unverified) == 12


def test_generated_reference_matrix_is_checked_in_without_drift() -> None:
    matrix = load_matrix(DEFAULT_MATRIX)

    assert matrix == build_matrix()
    assert AUDIT_SESSION_ID in matrix["assessment"]["history_semantics"]
    iteration = matrix["assessment"]["iteration"]
    assert all(
        [entry["iteration"] for entry in criterion["assessment_history"]]
        == list(range(1, iteration + 1))
        for criterion in matrix["criteria"]
    )
    assert all(
        all(
            entry["history_kind"] == "retained_assessment_snapshot"
            and entry["retained_snapshot_sha256"]
            for entry in criterion["assessment_history"][:-1]
        )
        and criterion["assessment_history"][-1]["history_kind"]
        == "current_surface_reassessment"
        for criterion in matrix["criteria"]
    )


def test_retained_history_entry_is_bound_to_full_snapshot_bytes(
    tmp_path: Path,
) -> None:
    snapshot = load_matrix(DEFAULT_MATRIX)
    iteration = snapshot["assessment"]["iteration"]
    surface = snapshot["assessment"]["assessment_surface"]["sha256"]
    history_root = tmp_path / AUDIT_SESSION_HISTORY_RELATIVE_ROOT
    history_root.mkdir(parents=True)
    target = history_root / f"iteration-{iteration:03d}-{surface}.json"
    target.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    content_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
    criterion = snapshot["criteria"][0]
    entry = {
        **criterion["assessment_history"][-1],
        "history_kind": "retained_assessment_snapshot",
        "evidence_scope": (
            "retained_snapshot:"
            f"{AUDIT_SESSION_HISTORY_RELATIVE_ROOT.as_posix()}/{target.name};"
            f"assessment_surface:{surface}"
        ),
        "retained_snapshot_sha256": content_sha256,
    }

    assert (
        reference_audit._retained_history_snapshot_findings(
            matrix_root=tmp_path,
            criterion_id=criterion["id"],
            entry=entry,
            cache={},
        )
        == []
    )

    tampered_claim = {**entry, "retained_snapshot_sha256": "0" * 64}
    assert reference_audit._retained_history_snapshot_findings(
        matrix_root=tmp_path,
        criterion_id=criterion["id"],
        entry=tampered_claim,
        cache={},
    ) == [
        f"{criterion['id']}:assessment_history_{iteration}_"
        "retained_snapshot_hash_mismatch"
    ]


def test_structure_mode_does_not_promote_incomplete_matrix() -> None:
    assert main(["--matrix", str(DEFAULT_MATRIX), "--validate-structure"]) == 0
    assert main(["--matrix", str(DEFAULT_MATRIX)]) == 1


def test_json_payload_exposes_receipt_state() -> None:
    evaluation = evaluate_matrix(DEFAULT_MATRIX)
    payload = _payload(evaluation)

    assert payload["execution_receipt"]["status"] == (
        evaluation.execution_receipt_status
    )
    assert payload["execution_receipt"]["clean_local_run"] is (
        evaluation.execution_receipt_clean_local_run
    )
    assert payload["execution_receipt"]["trusted"] is False
    assert payload["execution_receipt"]["trust_level"] == (
        evaluation.execution_receipt_trust_level
    )
    assert payload["execution_receipt"]["findings"] == list(
        evaluation.execution_receipt_findings
    )


def test_local_receipt_binds_source_without_authenticating_executor(
    tmp_path: Path,
) -> None:
    matrix = _conservative_matrix(tmp_path)
    document, surface, required = _receipt_document(matrix)
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    validation = validate_receipt(
        path,
        source_surface=surface,
        required_tests=required,
    )

    assert validation.status == "VALID_LOCAL_SELF_ATTESTED"
    assert validation.clean_local_run
    assert not validation.trusted
    assert validation.trust_level == "LOCAL_SELF_ATTESTED"
    assert validation.findings == ("execution_authenticity_unverified",)
    assert validation.content_sha256 == document["content_sha256"]
    assert validation.required_target_count == len(required)
    assert validation.tests_passed == len(required)
    for gate in matrix["fatal_gates"]:
        assert str(gate["verification_method"]).rsplit(" ", 1)[-1] in required


def test_synthetically_rehashed_receipt_remains_only_self_attested(
    tmp_path: Path,
) -> None:
    matrix = _conservative_matrix(tmp_path)
    _, surface, required = _receipt_document(matrix)
    document = build_receipt_document(
        source_surface=surface,
        required_tests=required,
        tests_passed=999_999,
        duration_seconds=0.000001,
        output_sha256="f" * 64,
        created_at="2026-07-29T00:00:00Z",
    )
    path = tmp_path / "synthetic-receipt.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    validation = validate_receipt(
        path,
        source_surface=surface,
        required_tests=required,
    )

    assert validation.status == "VALID_LOCAL_SELF_ATTESTED"
    assert validation.clean_local_run
    assert not validation.trusted
    assert "execution_authenticity_unverified" in validation.findings


def test_self_attested_receipt_never_claims_authenticated_verification(
    tmp_path: Path,
) -> None:
    conservative = _conservative_matrix(tmp_path)
    document, _, _ = _receipt_document(conservative)
    receipt_path = tmp_path / "local-receipt.json"
    receipt_path.write_text(json.dumps(document), encoding="utf-8")

    matrix = build_matrix(receipt_path=receipt_path)
    statuses = {row["status"] for row in matrix["criteria"]}

    assert "VERIFIED_LOCAL_SELF_ATTESTED" in statuses
    assert "VERIFIED" not in statuses
    assert matrix["assessment"]["execution_receipt"]["clean_local_run"] is True
    assert matrix["assessment"]["execution_receipt"]["trusted"] is False
    assert all(gate["status"] == "PASS" for gate in matrix["fatal_gates"])
    m3_results = [
        str(item["result"])
        for row in matrix["criteria"]
        if row["maturity"] == "M3"
        for item in row["objective_evidence"]
    ]
    assert m3_results
    assert all("PASS in the exact local self-attested" in item for item in m3_results)
    assert all("no current clean-PASS" not in item for item in m3_results)
    assert (
        "execution_authenticity_unverified"
        in (matrix["assessment"]["execution_receipt"]["findings"])
    )


def test_receipt_with_changed_source_or_test_hash_is_stale(tmp_path: Path) -> None:
    matrix = _conservative_matrix(tmp_path)
    document, surface, required = _receipt_document(matrix)
    path = tmp_path / "receipt.json"
    payload = document["payload"]
    assert isinstance(payload, dict)
    payload["source_surface"] = {**surface, "sha256": "0" * 64}
    _rehash_receipt(document)
    path.write_text(json.dumps(document), encoding="utf-8")

    source_validation = validate_receipt(
        path,
        source_surface=surface,
        required_tests=required,
    )
    assert source_validation.status == "STALE"
    assert "execution_receipt_source_surface_mismatch" in source_validation.findings

    valid_document, _, _ = _receipt_document(matrix)
    path.write_text(json.dumps(valid_document), encoding="utf-8")
    changed_required = dict(required)
    target = next(iter(changed_required))
    changed_required[target] = "0" * 64
    target_validation = validate_receipt(
        path,
        source_surface=surface,
        required_tests=changed_required,
    )
    assert target_validation.status == "STALE"
    assert "execution_receipt_test_targets_mismatch" in target_validation.findings


def test_receipt_content_hash_or_clean_pass_shape_cannot_be_forged(
    tmp_path: Path,
) -> None:
    matrix = _conservative_matrix(tmp_path)
    document, surface, required = _receipt_document(matrix)
    path = tmp_path / "receipt.json"
    document["content_sha256"] = "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")

    invalid_hash = validate_receipt(
        path,
        source_surface=surface,
        required_tests=required,
    )
    assert invalid_hash.status == "INVALID"
    assert "execution_receipt_content_hash_mismatch" in invalid_hash.findings

    document, _, _ = _receipt_document(matrix)
    payload = document["payload"]
    assert isinstance(payload, dict)
    pytest_result = payload["pytest"]
    assert isinstance(pytest_result, dict)
    pytest_result["skipped"] = 1
    _rehash_receipt(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    invalid_run = validate_receipt(
        path,
        source_surface=surface,
        required_tests=required,
    )
    assert invalid_run.status == "INVALID"
    assert "execution_receipt_pytest_not_clean_pass" in invalid_run.findings


def test_receipt_runner_writes_only_after_a_clean_exact_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    matrix = _conservative_matrix(tmp_path)
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    required = _required_tests(matrix)

    observed_environment: dict[str, str] = {}

    def clean_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        observed_environment.update(environment)
        junit_argument = next(
            argument for argument in command if argument.startswith("--junitxml=")
        )
        junit = Path(junit_argument.split("=", 1)[1])
        cases = "".join(
            f'<testcase name="test_{index}" />' for index in range(len(required))
        )
        junit.write_text(f"<testsuite>{cases}</testsuite>", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"passed", b"")

    monkeypatch.setattr(receipt_tool.subprocess, "run", clean_run)
    output = tmp_path / "receipt.json"

    validation = run_and_write_receipt(
        root=PROJECT_ROOT,
        matrix_path=matrix_path,
        output_path=output,
    )

    assert output.is_file()
    assert validation.clean_local_run
    assert not validation.trusted
    assert validation.tests_passed == len(required)
    isolated_root = Path(observed_environment["TMPDIR"])
    assert Path(observed_environment["RESEARCH_DATA_ROOT"]) == (
        isolated_root / "datasets"
    )
    assert Path(observed_environment["RESEARCH_ARTIFACT_ROOT"]) == (
        isolated_root / "artifacts"
    )
    assert Path(observed_environment["RESEARCH_REPORT_ROOT"]) == (
        isolated_root / "reports"
    )
    assert Path(observed_environment["RESEARCH_CACHE_ROOT"]) == (
        isolated_root / "cache"
    )
    assert (
        Path(observed_environment["RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH"])
        == isolated_root / "identity" / "experiment-identities.jsonl"
    )
    assert Path(observed_environment["XDG_STATE_HOME"]) == isolated_root / "xdg-state"
    assert not isolated_root.exists()


def test_receipt_python_executable_preserves_virtualenv_launcher(
    tmp_path: Path,
) -> None:
    target = tmp_path / "system-python"
    target.write_text("placeholder", encoding="utf-8")
    launcher = tmp_path / "venv" / "bin" / "python"
    launcher.parent.mkdir(parents=True)
    try:
        launcher.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    assert receipt_tool._absolute_executable(str(launcher)) == str(launcher)
    assert receipt_tool._absolute_executable(str(launcher)) != str(launcher.resolve())


def test_receipt_normalizes_only_python_aliases_in_the_same_virtualenv(
    tmp_path: Path,
) -> None:
    target = tmp_path / "host" / "python3.12"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o755)
    environment = tmp_path / "venv"
    launcher_directory = environment / ("Scripts" if os.name == "nt" else "bin")
    launcher_directory.mkdir(parents=True)
    canonical_name = "python.exe" if os.name == "nt" else "python"
    alias_names = (
        (
            "python.exe",
            f"python{sys.version_info.major}.exe",
            f"python{sys.version_info.major}.{sys.version_info.minor}.exe",
        )
        if os.name == "nt"
        else (
            "python",
            f"python{sys.version_info.major}",
            f"python{sys.version_info.major}.{sys.version_info.minor}",
        )
    )
    for name in alias_names:
        try:
            (launcher_directory / name).symlink_to(target)
        except OSError as error:
            pytest.skip(f"symlink creation unavailable: {error}")
    canonical = launcher_directory / canonical_name

    normalized = {
        receipt_tool._normalized_python_executable(
            str(launcher_directory / name),
            prefix=str(environment),
            base_prefix=str(tmp_path / "host-prefix"),
        )
        for name in alias_names
    }

    assert normalized == {str(canonical)}
    assert str(canonical) != str(canonical.resolve())

    other_environment_launcher = (
        tmp_path / "other-venv" / launcher_directory.name / alias_names[-1]
    )
    other_environment_launcher.parent.mkdir(parents=True)
    other_environment_launcher.symlink_to(target)
    assert receipt_tool._normalized_python_executable(
        str(other_environment_launcher),
        prefix=str(environment),
        base_prefix=str(tmp_path / "host-prefix"),
    ) == str(other_environment_launcher)


def test_receipt_python_alias_normalization_keeps_other_arguments_exact(
    tmp_path: Path,
) -> None:
    matrix = _conservative_matrix(tmp_path)
    document, surface, required = _receipt_document(matrix)
    payload = document["payload"]
    assert isinstance(payload, dict)
    pytest_result = payload["pytest"]
    assert isinstance(pytest_result, dict)
    command = pytest_result["command"]
    assert isinstance(command, list)
    command[3] = "-qq"
    _rehash_receipt(document)
    path = tmp_path / "changed-command-receipt.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    validation = validate_receipt(
        path,
        source_surface=surface,
        required_tests=required,
    )

    assert validation.status == "STALE"
    assert "execution_receipt_pytest_command_mismatch" in validation.findings


def test_receipt_uses_short_posix_temp_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.name != "posix" or not Path("/tmp").is_dir():
        pytest.skip("short POSIX temporary namespace is unavailable")
    long_temp = tmp_path / ("long-receipt-parent-" * 8)
    long_temp.mkdir()
    monkeypatch.setenv("TMPDIR", str(long_temp))

    assert receipt_tool._receipt_temp_parent() == "/tmp"


def test_receipt_runner_does_not_publish_a_failed_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    matrix = _conservative_matrix(tmp_path)
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    def failed_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 1, b"", b"failed")

    monkeypatch.setattr(receipt_tool.subprocess, "run", failed_run)
    output = tmp_path / "receipt.json"

    with pytest.raises(RuntimeError, match="execution_receipt_pytest_failed"):
        run_and_write_receipt(
            root=PROJECT_ROOT,
            matrix_path=matrix_path,
            output_path=output,
        )
    assert not output.exists()


def test_verified_and_pass_claims_without_receipt_are_rejected(
    tmp_path: Path,
) -> None:
    payload = _conservative_matrix(tmp_path)
    criterion = payload["criteria"][0]
    criterion["maturity"] = "M4"
    criterion["status"] = "VERIFIED"
    final_history = criterion["assessment_history"][-1]
    final_history["maturity"] = "M4"
    final_history["status"] = "VERIFIED"
    payload["fatal_gates"][0]["status"] = "PASS"
    final_iteration = payload["assessment"]["iteration"]

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "A-01:verified_without_valid_execution_receipt" in evaluation.findings
    assert (
        f"A-01:assessment_history_{final_iteration}_verified_without_valid_execution_receipt"
        in evaluation.findings
    )
    assert "FG-01:pass_without_valid_execution_receipt" in evaluation.findings
    assert "FG-01" in evaluation.fatal_unverified
    assert not evaluation.complete


def test_local_m4_claim_without_clean_local_receipt_is_rejected(
    tmp_path: Path,
) -> None:
    payload = _conservative_matrix(tmp_path)
    criterion = payload["criteria"][0]
    criterion["maturity"] = "M4"
    criterion["status"] = "VERIFIED_LOCAL_SELF_ATTESTED"
    final_history = criterion["assessment_history"][-1]
    final_history["maturity"] = "M4"
    final_history["status"] = "VERIFIED_LOCAL_SELF_ATTESTED"
    final_iteration = payload["assessment"]["iteration"]

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert (
        "A-01:local_verified_without_clean_local_execution_receipt"
        in evaluation.findings
    )
    assert (
        f"A-01:assessment_history_{final_iteration}_local_verified_without_clean_local_execution_receipt"
        in evaluation.findings
    )


def test_declared_receipt_summary_cannot_override_validator(
    tmp_path: Path,
) -> None:
    payload = _conservative_matrix(tmp_path)
    payload["assessment"]["execution_receipt"]["status"] = "VALID_LOCAL_SELF_ATTESTED"

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "assessment_execution_receipt_mismatch" in evaluation.findings
    assert evaluation.execution_receipt_status == "MISSING"


def test_generation_commit_is_provenance_not_self_referential_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def newer_provenance(root: Path) -> tuple[Path, str, str, bool]:
        return root.resolve(), "f" * 40, "later-branch", True

    monkeypatch.setattr(reference_audit, "_git_provenance", newer_provenance)
    monkeypatch.setattr(
        reference_audit,
        "_git_commit_is_ancestor",
        lambda _root, _commit, _head: True,
    )

    evaluation = evaluate_matrix(DEFAULT_MATRIX)

    assert "assessment_generation_commit_not_ancestor" not in evaluation.findings
    assert not any(
        "branch_mismatch" in finding or "worktree_state_mismatch" in finding
        for finding in evaluation.findings
    )


def test_non_ancestor_generation_commit_or_wrong_root_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def wrong_provenance(root: Path) -> tuple[Path, str, str, bool]:
        return root.parent, "f" * 40, "branch", False

    monkeypatch.setattr(reference_audit, "_git_provenance", wrong_provenance)
    monkeypatch.setattr(
        reference_audit,
        "_git_commit_is_ancestor",
        lambda _root, _commit, _head: False,
    )

    evaluation = evaluate_matrix(DEFAULT_MATRIX)

    assert "assessment_git_root_mismatch" in evaluation.findings
    assert "assessment_generation_commit_not_ancestor" in evaluation.findings


def test_canonical_human_and_machine_reports_are_current_and_honest() -> None:
    report = REPORT_PATH.read_text(encoding="utf-8")
    result = json.loads(RESULT_PATH.read_text(encoding="utf-8"))

    for section in range(1, 17):
        assert f"## 13.{section} " in report
    assert "441.41" not in report
    assert "383.12" not in report
    assert "retained_evidence" not in result
    _assert_machine_result_schema(result)

    evaluation = evaluate_matrix(DEFAULT_MATRIX)
    assert result["verdict"] == evaluation.verdict
    assert result["is_complete_against_reference"] is evaluation.complete
    assert result["overall_score"] == round(evaluation.score, 4)
    assert result["execution_receipt"]["status"] == (
        evaluation.execution_receipt_status
    )

    marker = "## 기계 판독 가능한 JSON 결과\n\n```json\n"
    assert report.count(marker) == 1
    embedded, suffix = report.split(marker, maxsplit=1)[1].split("\n```\n", maxsplit=1)
    assert not suffix.strip()
    assert json.loads(embedded) == result


def test_assessment_surface_excludes_only_declared_generated_outputs(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("owned\n", encoding="utf-8")
    before = audit_surface(tmp_path)
    receipt = (
        tmp_path / "docs" / "investment-research-platform-audit-execution-receipt.json"
    )
    receipt.parent.mkdir()
    receipt.write_text('{"generated":true}\n', encoding="utf-8")
    assert audit_surface(tmp_path) == before

    virtualenv_file = tmp_path / ".venv" / "cache.py"
    virtualenv_file.parent.mkdir()
    virtualenv_file.write_text("ephemeral\n", encoding="utf-8")
    assert audit_surface(tmp_path) == before

    unknown = tmp_path / "previously_unknown_domain" / "live_trading.py"
    unknown.parent.mkdir()
    unknown.write_text("forbidden = True\n", encoding="utf-8")
    assert audit_surface(tmp_path)["sha256"] != before["sha256"]


def test_assessment_surface_hash_binds_file_mode() -> None:
    with tempfile.TemporaryDirectory(prefix="audit-surface-mode-", dir="/tmp") as root:
        source = Path(root) / "scripts" / "platform"
        source.parent.mkdir()
        source.write_text("#!/bin/sh\n", encoding="utf-8")
        source.chmod(0o644)
        before = audit_surface(Path(root))
        source.chmod(0o755)
        assert audit_surface(Path(root))["sha256"] != before["sha256"]


def test_duplicate_and_nonfinite_json_are_rejected(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema_version": 1, "schema_version": 2}\n', encoding="utf-8"
    )
    with pytest.raises(DuplicateKeyError, match="duplicate_json_key"):
        load_matrix(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"score": NaN}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="nonfinite_json_constant"):
        load_matrix(nonfinite)


def test_unknown_criterion_and_missing_evidence_fail_closed(
    tmp_path: Path,
) -> None:
    payload = _conservative_matrix(tmp_path)
    criterion = payload["criteria"][0]
    criterion["id"] = "A-99"
    criterion["objective_evidence"][0]["path"] = "does/not/exist.py"

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "criterion_id_set_mismatch" in evaluation.findings
    assert any("evidence_path_missing" in finding for finding in evaluation.findings)
    assert not evaluation.complete


def test_declared_score_cannot_override_computed_score(tmp_path: Path) -> None:
    payload = _conservative_matrix(tmp_path)
    baseline = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))
    payload["declared_score"] = 100

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert evaluation.score == baseline.score
    assert "matrix_fields_invalid" in evaluation.findings


def test_fg06_caps_score_and_forbids_m5_awards(tmp_path: Path) -> None:
    payload = _conservative_matrix(tmp_path)
    payload["assessment"]["score_cap"] = 100
    next(gate for gate in payload["fatal_gates"] if gate["id"] == "FG-06")["status"] = (
        "FAIL"
    )
    criterion = payload["criteria"][0]
    criterion["maturity"] = "M5"
    criterion["assessment_history"][-1]["maturity"] = "M5"

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "fg06_score_cap_invalid" in evaluation.findings
    assert "fg06_m5_award_invalid" in evaluation.findings
    assert evaluation.verdict == "NOT_AN_INVESTMENT_RESEARCH_PLATFORM"


def test_evidence_paths_and_commands_cannot_escape_binding(
    tmp_path: Path,
) -> None:
    payload = _conservative_matrix(tmp_path)
    item = payload["criteria"][0]["objective_evidence"][0]
    item["path"] = "../../outside.py"
    item["command"] = str(item["command"]).replace(
        str(item["test"]), "tests/test_unrelated.py"
    )

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "A-01:evidence_path_outside_root" in evaluation.findings
    assert "A-01:evidence_command_binding_mismatch" in evaluation.findings


def test_assessment_history_requires_sequence_and_final_binding(
    tmp_path: Path,
) -> None:
    payload = _conservative_matrix(tmp_path)
    history = payload["criteria"][0]["assessment_history"]
    # A new authority session legitimately starts with one current-surface
    # entry.  Add a forged extra entry instead of assuming that a historical
    # entry is always available to remove.
    history.append(copy.deepcopy(history[-1]))
    history[-1]["iteration"] = int(history[-2]["iteration"]) + 1
    history[-1]["diagnosis"] = "forged diagnosis"

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "A-01:assessment_history_length_invalid" in evaluation.findings
    assert "A-01:assessment_history_iteration_sequence_invalid" in evaluation.findings
    assert "A-01:assessment_history_final_diagnosis_mismatch" in evaluation.findings


@pytest.mark.parametrize(
    ("maturity", "status"),
    (("M5", "UNVERIFIED_EXTERNAL"), ("M3", "VERIFIED")),
)
def test_status_cannot_overstate_maturity(
    tmp_path: Path, maturity: str, status: str
) -> None:
    payload = _conservative_matrix(tmp_path)
    criterion = payload["criteria"][0]
    criterion["maturity"] = maturity
    criterion["status"] = status

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert any("status_maturity_incoherent" in item for item in evaluation.findings)


def test_generator_diagnostics_name_the_first_stale_json_path() -> None:
    expected = {"assessment": {"surface": {"sha256": "a"}}}
    actual = {"assessment": {"surface": {"sha256": "b"}}}

    assert _differences(actual, expected) == [
        "$.assessment.surface.sha256:actual='b' expected='a'"
    ]


def test_complete_verdict_requires_the_full_completion_predicate() -> None:
    assert (
        _verdict(
            score=100,
            complete=False,
            fatal_failures=(),
            fatal_unverified=(),
            findings=("forged_matrix",),
            critical_m4_or_higher=72,
            critical_count=72,
        )
        == "SUBSTANTIALLY_COMPLETE_BUT_INCOMPLETE"
    )


@pytest.mark.parametrize("fatal_gate", ("FG-03", "FG-06"))
def test_future_information_or_reproducibility_fatal_overrides_score(
    fatal_gate: str,
) -> None:
    assert (
        _verdict(
            score=100,
            complete=True,
            fatal_failures=(fatal_gate,),
            fatal_unverified=(),
            findings=(),
            critical_m4_or_higher=72,
            critical_count=72,
        )
        == "NOT_AN_INVESTMENT_RESEARCH_PLATFORM"
    )


def test_default_receipt_path_is_excluded_from_audit_surface() -> None:
    exclusions = audit_surface(PROJECT_ROOT)["exclusions"]

    assert DEFAULT_RECEIPT.relative_to(PROJECT_ROOT).as_posix() in {
        value.removeprefix("file:") for value in exclusions if value.startswith("file:")
    }


def test_local_e4_receipt_inventory_excludes_conditional_postgresql_files(
    tmp_path: Path,
) -> None:
    matrix = _conservative_matrix(tmp_path)
    required = required_test_hashes_from_matrix(matrix, root=PROJECT_ROOT)

    assert "apps/internal_web/tests/test_audit_outbox.py" in required
    assert "apps/internal_web/tests/test_database_immutability_static.py" in required
    assert (
        "services/research_operations/tests/test_service_alert_worker_unit.py"
        in required
    )
    assert (
        "apps/internal_web/tests/test_database_immutability_postgresql.py"
        not in required
    )
    assert (
        "services/research_operations/tests/test_service_alert_postgresql.py"
        not in required
    )
    assert "apps/internal_web/tests/test_browser_e2e.py" not in required
    assert (
        "services/research_operations/tests/test_prior_release_upgrade.py"
        not in required
    )


def test_current_matrix_binds_pit_corporate_and_portable_evidence_without_overclaim(
    tmp_path: Path,
) -> None:
    conservative = _conservative_matrix(tmp_path)
    document, _, required = _receipt_document(conservative)
    receipt_path = tmp_path / "local-self-attested-receipt.json"
    receipt_path.write_text(json.dumps(document), encoding="utf-8")

    matrix = build_matrix(receipt_path=receipt_path)
    by_id = {row["id"]: row for row in matrix["criteria"]}
    assert {
        criterion_id: by_id[criterion_id]["maturity"]
        for criterion_id in (
            "B-06",
            "B-07",
            "B-08",
            "B-09",
            "C-19",
            "E-04",
            "E-06",
            "E-24",
            "G-02",
            "H-01",
            "H-02",
            "H-03",
            "H-04",
            "H-05",
            "H-06",
            "H-07",
            "H-08",
            "H-09",
            "H-10",
            "H-11",
            "H-12",
        )
    } == {
        "B-06": "M4",
        "B-07": "M4",
        "B-08": "M3",
        "B-09": "M3",
        "C-19": "M4",
        "E-04": "M4",
        "E-06": "M3",
        "E-24": "M4",
        "G-02": "M4",
        "H-01": "M3",
        "H-02": "M3",
        "H-03": "M4",
        "H-04": "M3",
        "H-05": "M3",
        "H-06": "M3",
        "H-07": "M3",
        "H-08": "M3",
        "H-09": "M3",
        "H-10": "M3",
        "H-11": "M4",
        "H-12": "M4",
    }
    assert "NOT_LOCALLY_OR_OMNISCIENTLY_PROVABLE" in by_id["B-06"]["gap"]
    assert "UNVERIFIED_NO_PROVIDER_SOURCE_ARTIFACT_CONTRACT" in by_id["B-08"]["gap"]
    assert "replacement price-series/InstrumentMaster" in by_id["E-06"]["gap"]
    assert "같은 host" in by_id["G-02"]["gap"]
    assert "OS filesystem/network namespace" in by_id["G-02"]["gap"]
    assert "`INCOMPLETE`" in by_id["H-01"]["gap"]
    assert "`INCOMPLETE`" in by_id["H-07"]["gap"]

    assert {
        "tests/test_point_in_time_universe_v2.py",
        "tests/test_corporate_action_accounting_v2.py",
        "tests/test_portable_research_package.py",
        "tests/test_portable_research_package_wheel_cold.py",
    }.issubset(required)
    assert "tests/test_point_in_time_universe_v2.py" in {
        item["test"] for item in by_id["B-06"]["objective_evidence"]
    }
    assert "tests/test_corporate_action_accounting_v2.py" in {
        item["test"] for item in by_id["E-24"]["objective_evidence"]
    }
    assert "tests/test_portable_research_package_wheel_cold.py" in {
        item["test"] for item in by_id["G-02"]["objective_evidence"]
    }

    gates = {gate["id"]: gate for gate in matrix["fatal_gates"]}
    assert gates["FG-04"]["verification_method"].endswith(
        "tests/test_point_in_time_universe_v2.py"
    )
    assert "전지적으로 증명한다고 주장하지 않는다" in gates["FG-04"]["evidence"]
    assert "독립 E5" in gates["FG-06"]["evidence"]


def test_generated_result_preserves_validation_incidents_and_external_pg_gap(
    tmp_path: Path,
) -> None:
    matrix = _conservative_matrix(tmp_path)
    matrix_path = _write_matrix(tmp_path / "matrix.json", matrix)
    evaluation = evaluate_matrix(matrix_path)
    result = _machine_result(matrix, evaluation)

    incidents = result["tests_failed"]
    assert {incident["id"] for incident in incidents} == {
        f"VI-{number:02d}" for number in range(1, 60)
    }
    assert any(incident["status"] == "UNVERIFIED_EXTERNAL" for incident in incidents)
    unverified = result["unverified_external_dependencies"]
    assert any(
        "test_database_immutability_postgresql.py" in item for item in unverified
    )
    assert any("test_service_alert_postgresql.py" in item for item in unverified)
