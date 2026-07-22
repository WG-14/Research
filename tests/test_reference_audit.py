from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import tools.reference_audit as reference_audit
from tools.reference_audit import (
    DEFAULT_MATRIX,
    DuplicateKeyError,
    evaluate_matrix,
    load_matrix,
    main,
    _verdict,
)
from tools.render_reference_audit_report import REPORT_PATH, RESULT_PATH
from tools.reference_audit_surface import AUDIT_SURFACE_SCHEMA_VERSION, audit_surface
from tools.update_reference_audit import build_matrix


def _write_matrix(path: Path, payload: dict[str, object]) -> Path:
    isolated_path = path.parent / "audit-root" / "docs" / path.name
    isolated_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return isolated_path


def _assert_nonempty_text(value: object) -> None:
    assert isinstance(value, str)
    assert value.strip() == value
    assert value


def _assert_text_list(value: object) -> None:
    assert isinstance(value, list)
    assert value
    for item in value:
        _assert_nonempty_text(item)


def _assert_machine_result_schema(result: object) -> None:
    assert isinstance(result, dict)
    assert set(result) == {
        "verdict",
        "is_complete_against_reference",
        "overall_score",
        "raw_weighted_score",
        "score_cap",
        "repository",
        "fatal_gates",
        "domain_scores",
        "criteria",
        "final_questions",
        "top_gaps",
        "unverified_external_dependencies",
        "retained_evidence",
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

    repository = result["repository"]
    assert isinstance(repository, dict)
    assert set(repository) == {
        "root",
        "commit",
        "branch",
        "dirty",
        "assessment_surface",
        "primary_languages",
        "entrypoints",
        "test_commands",
    }
    _assert_nonempty_text(repository["root"])
    assert Path(repository["root"]).is_absolute()
    _assert_nonempty_text(repository["commit"])
    _assert_nonempty_text(repository["branch"])
    assert isinstance(repository["dirty"], bool)
    assessment_surface = repository["assessment_surface"]
    assert isinstance(assessment_surface, dict)
    assert set(assessment_surface) == {
        "schema_version",
        "file_count",
        "sha256",
        "exclusions",
    }
    assert assessment_surface["schema_version"] == AUDIT_SURFACE_SCHEMA_VERSION
    assert isinstance(assessment_surface["file_count"], int)
    assert assessment_surface["file_count"] > 0
    assert len(assessment_surface["sha256"]) == 64
    _assert_text_list(assessment_surface["exclusions"])
    assert {
        "directory:.git",
        "directory:.venv",
        "directory_suffix:*.egg-info",
        "file:docs/investment-research-platform-audit.json",
        "file:docs/investment-research-platform-audit-report.md",
        "file:docs/investment-research-platform-audit-result.json",
        "file_suffix:*.pyc",
    }.issubset(assessment_surface["exclusions"])
    for key in ("primary_languages", "entrypoints", "test_commands"):
        _assert_text_list(repository[key])

    fatal_gates = result["fatal_gates"]
    assert isinstance(fatal_gates, list)
    assert len(fatal_gates) == 12
    assert {gate["id"] for gate in fatal_gates} == {
        f"FG-{number:02d}" for number in range(1, 13)
    }
    for gate in fatal_gates:
        assert isinstance(gate, dict)
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
        _assert_text_list(gate["evidence"])
        _assert_nonempty_text(gate["verification_method"])
        _assert_nonempty_text(gate["impact"])
        assert isinstance(gate["mitigation_possible"], bool)
        _assert_nonempty_text(gate["required_remediation"])

    expected_domains = {
        "scope_boundary": 5,
        "data": 15,
        "reproducibility": 15,
        "research_lifecycle": 10,
        "backtesting_simulation": 15,
        "validation": 15,
        "review_governance": 10,
        "artifacts_knowledge": 10,
        "security_observability": 5,
        "architecture_usability": 5,
    }
    domain_scores = result["domain_scores"]
    assert isinstance(domain_scores, dict)
    assert set(domain_scores) == set(expected_domains)
    for domain, maximum in expected_domains.items():
        score = domain_scores[domain]
        assert isinstance(score, dict)
        assert set(score) == {"max", "score"}
        assert score["max"] == maximum
        assert isinstance(score["score"], (int, float))
        assert not isinstance(score["score"], bool)
        assert 0 <= score["score"] <= maximum

    criteria = result["criteria"]
    assert isinstance(criteria, list)
    assert len(criteria) == 184
    assert len({criterion["id"] for criterion in criteria}) == 184
    allowed_statuses = {
        "VERIFIED",
        "IMPLEMENTED_NOT_VERIFIED",
        "PARTIAL",
        "DOCUMENTATION_ONLY",
        "PLACEHOLDER",
        "MISSING",
        "OUT_OF_SCOPE_VIOLATION",
        "UNVERIFIED_EXTERNAL",
    }
    evidence_fields = {
        "path",
        "path_sha256",
        "symbol_or_lines",
        "test",
        "test_sha256",
        "command",
        "result",
    }
    for criterion in criteria:
        assert isinstance(criterion, dict)
        assert set(criterion) == {
            "id",
            "importance",
            "maturity",
            "status",
            "evidence",
            "gap",
            "required_remediation",
        }
        _assert_nonempty_text(criterion["id"])
        assert criterion["importance"] in {"CRITICAL", "MAJOR", "SUPPORTING"}
        assert criterion["maturity"] in {f"M{rank}" for rank in range(6)}
        assert criterion["status"] in allowed_statuses
        _assert_nonempty_text(criterion["gap"])
        _assert_nonempty_text(criterion["required_remediation"])
        evidence = criterion["evidence"]
        assert isinstance(evidence, list)
        assert evidence
        for item in evidence:
            assert isinstance(item, dict)
            assert set(item) == evidence_fields
            for key in evidence_fields:
                _assert_nonempty_text(item[key])
            for key in ("path_sha256", "test_sha256"):
                assert len(item[key]) == 64
                assert set(item[key]) <= set("0123456789abcdef")

    questions = result["final_questions"]
    assert isinstance(questions, list)
    assert [question["number"] for question in questions] == list(range(1, 16))
    for question in questions:
        assert isinstance(question, dict)
        assert set(question) == {"number", "answer", "evidence", "explanation"}
        assert question["answer"] in {"YES", "PARTIAL", "NO", "UNVERIFIED"}
        _assert_text_list(question["evidence"])
        _assert_nonempty_text(question["explanation"])

    top_gaps = result["top_gaps"]
    assert isinstance(top_gaps, list)
    assert top_gaps
    for gap in top_gaps:
        assert isinstance(gap, dict)
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
        for key in (
            "criterion_ids",
            "required_implementation",
            "required_tests",
            "definition_of_done",
        ):
            _assert_text_list(gap[key])
        for key in ("title", "why_it_matters"):
            _assert_nonempty_text(gap[key])

    retained_evidence = result["retained_evidence"]
    assert isinstance(retained_evidence, dict)
    assert set(retained_evidence) == {"index", "artifacts"}
    retained_index = retained_evidence["index"]
    assert isinstance(retained_index, dict)
    assert set(retained_index) == {
        "path",
        "byte_sha256",
        "run_root",
        "test_status",
    }
    for key in ("path", "run_root"):
        _assert_nonempty_text(retained_index[key])
        assert Path(retained_index[key]).is_absolute()
    assert retained_index["test_status"] == "PASS"
    assert retained_index["byte_sha256"].startswith("sha256:")
    assert len(retained_index["byte_sha256"]) == 71

    retained_artifacts = retained_evidence["artifacts"]
    assert isinstance(retained_artifacts, list)
    assert retained_artifacts
    for artifact in retained_artifacts:
        assert isinstance(artifact, dict)
        assert set(artifact) == {"label", "path", "byte_sha256", "binding"}
        for key in ("label", "path", "byte_sha256", "binding"):
            _assert_nonempty_text(artifact[key])
        assert Path(artifact["path"]).is_absolute()
        assert artifact["byte_sha256"].startswith("sha256:")
        assert len(artifact["byte_sha256"]) == 71

    for key in ("unverified_external_dependencies", "commands_executed"):
        _assert_text_list(result[key])
    tests_failed = result["tests_failed"]
    assert isinstance(tests_failed, list)
    for failure in tests_failed:
        assert isinstance(failure, dict)
        assert set(failure) == {"command", "failure", "resolution"}
        for value in failure.values():
            _assert_nonempty_text(value)
    _assert_nonempty_text(result["final_reasoning"])


def test_canonical_reference_inventory_and_assessment_are_exact() -> None:
    evaluation = evaluate_matrix(DEFAULT_MATRIX)

    assert evaluation.findings == ()
    assert evaluation.score == pytest.approx(71.9947988391)
    assert evaluation.raw_score == pytest.approx(71.9947988391)
    assert evaluation.score_cap == 84
    assert evaluation.fatal_failures == ("FG-06",)
    assert evaluation.fatal_unverified == ()
    assert evaluation.critical_m4_or_higher == 36
    assert evaluation.critical_count == 72
    assert not evaluation.complete
    assert evaluation.verdict == "NOT_AN_INVESTMENT_RESEARCH_PLATFORM"


def test_complete_verdict_requires_the_full_completion_predicate() -> None:
    verdict = _verdict(
        score=100,
        complete=False,
        fatal_failures=(),
        fatal_unverified=(),
        findings=("forged_matrix",),
        critical_m4_or_higher=72,
        critical_count=72,
    )

    assert verdict == "SUBSTANTIALLY_COMPLETE_BUT_INCOMPLETE"


@pytest.mark.parametrize("fatal_gate", ("FG-03", "FG-06"))
def test_future_information_or_reproducibility_fatal_overrides_score(
    fatal_gate: str,
) -> None:
    verdict = _verdict(
        score=100,
        complete=True,
        fatal_failures=(fatal_gate,),
        fatal_unverified=(),
        findings=(),
        critical_m4_or_higher=72,
        critical_count=72,
    )

    assert verdict == "NOT_AN_INVESTMENT_RESEARCH_PLATFORM"


def test_other_fatal_gate_blocks_near_complete_without_forcing_not_platform() -> None:
    verdict = _verdict(
        score=92,
        complete=False,
        fatal_failures=("FG-11",),
        fatal_unverified=(),
        findings=(),
        critical_m4_or_higher=72,
        critical_count=72,
    )

    assert verdict == "SUBSTANTIALLY_COMPLETE_BUT_INCOMPLETE"


def test_inconsistent_complete_flag_cannot_bypass_other_fatal_gate() -> None:
    verdict = _verdict(
        score=100,
        complete=True,
        fatal_failures=("FG-11",),
        fatal_unverified=(),
        findings=(),
        critical_m4_or_higher=72,
        critical_count=72,
    )

    assert verdict == "SUBSTANTIALLY_COMPLETE_BUT_INCOMPLETE"


def test_inconsistent_complete_flag_cannot_bypass_structural_findings() -> None:
    verdict = _verdict(
        score=100,
        complete=True,
        fatal_failures=(),
        fatal_unverified=(),
        findings=("forged_matrix",),
        critical_m4_or_higher=72,
        critical_count=72,
    )

    assert verdict == "SUBSTANTIALLY_COMPLETE_BUT_INCOMPLETE"


def test_generated_reference_matrix_is_checked_in_without_drift() -> None:
    matrix = load_matrix(DEFAULT_MATRIX)

    assert matrix == build_matrix()
    iteration = matrix["assessment"]["iteration"]
    assert 1 <= iteration <= 10
    assert all(
        [entry["iteration"] for entry in criterion["assessment_history"]]
        == list(range(1, iteration + 1))
        for criterion in matrix["criteria"]
    )


def test_canonical_matrix_must_match_the_generator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reference_audit, "build_matrix", lambda: {})

    evaluation = evaluate_matrix(DEFAULT_MATRIX)

    assert "canonical_matrix_generator_mismatch" in evaluation.findings
    assert not evaluation.complete


def test_canonical_git_identity_and_worktree_state_are_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mismatched_provenance(root: Path) -> tuple[Path, str, str, bool]:
        return root.parent, "0" * 40, "", False

    monkeypatch.setattr(reference_audit, "_git_provenance", mismatched_provenance)

    evaluation = evaluate_matrix(DEFAULT_MATRIX)

    assert "assessment_git_root_mismatch" in evaluation.findings
    assert "assessment_repository_commit_mismatch" in evaluation.findings
    assert "assessment_repository_detached_head" in evaluation.findings
    assert "assessment_repository_branch_mismatch" in evaluation.findings
    assert "assessment_worktree_state_mismatch" in evaluation.findings
    assert not evaluation.complete


def test_canonical_human_and_machine_reports_cover_the_required_surface() -> None:
    report = REPORT_PATH.read_text(encoding="utf-8")
    result = json.loads(RESULT_PATH.read_text(encoding="utf-8"))

    for section in range(1, 17):
        assert f"## 13.{section} " in report
    for required_text in (
        "| 수행한 반복 횟수 |",
        "| 완전 충족(VERIFIED) 판정 기준 수 |",
        "| 구현된 내용 | 실행 명령·검증 증거 |",
        "| 관련 평가 기준 | 수정·유지 방안 |",
        "### 주요 변경 사항",
        "NOT RETAINED",
    ):
        assert required_text in report

    _assert_machine_result_schema(result)
    evaluation = evaluate_matrix(DEFAULT_MATRIX)
    assert result["verdict"] == evaluation.verdict
    assert result["is_complete_against_reference"] is evaluation.complete
    assert result["overall_score"] == round(evaluation.score, 4)
    assert result["raw_weighted_score"] == round(evaluation.raw_score, 4)
    assert result["score_cap"] == evaluation.score_cap

    marker = "## 기계 판독 가능한 JSON 결과\n\n```json\n"
    assert report.count(marker) == 1
    embedded_json, suffix = report.split(marker, maxsplit=1)[1].split(
        "\n```\n", maxsplit=1
    )
    assert not suffix.strip()
    embedded_result = json.loads(embedded_json)
    _assert_machine_result_schema(embedded_result)
    assert embedded_result == result


def test_assessment_surface_covers_owned_root_files_but_not_virtualenv(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("owned\n", encoding="utf-8")
    root_policy = tmp_path / ".env.example"
    root_policy.write_text("POLICY=one\n", encoding="utf-8")
    before = audit_surface(tmp_path)

    virtualenv_file = tmp_path / "apps" / "internal_web" / ".venv" / "cache.py"
    virtualenv_file.parent.mkdir(parents=True)
    virtualenv_file.write_text("ephemeral\n", encoding="utf-8")
    assert audit_surface(tmp_path) == before

    root_policy.write_text("POLICY=two\n", encoding="utf-8")
    after_policy = audit_surface(tmp_path)
    assert after_policy["sha256"] != before["sha256"]

    unknown_root = tmp_path / "Dockerfile.audit-test"
    unknown_root.write_text("FROM scratch\n", encoding="utf-8")
    after_unknown_root = audit_surface(tmp_path)
    assert after_unknown_root["sha256"] != after_policy["sha256"]

    unknown_domain = tmp_path / "previously_unknown_domain" / "live_trading.py"
    unknown_domain.parent.mkdir()
    unknown_domain.write_text("forbidden = True\n", encoding="utf-8")
    after_unknown_domain = audit_surface(tmp_path)
    assert after_unknown_domain["sha256"] != after_unknown_root["sha256"]


def test_assessment_surface_hash_binds_file_mode() -> None:
    with tempfile.TemporaryDirectory(prefix="audit-surface-mode-", dir="/tmp") as root:
        source = Path(root) / "scripts" / "platform"
        source.parent.mkdir()
        source.write_text("#!/bin/sh\n", encoding="utf-8")
        source.chmod(0o644)
        before = audit_surface(Path(root))

        source.chmod(0o755)

        assert audit_surface(Path(root))["sha256"] != before["sha256"]


def test_structure_mode_does_not_promote_incomplete_matrix() -> None:
    assert main(["--matrix", str(DEFAULT_MATRIX), "--validate-structure"]) == 0
    assert main(["--matrix", str(DEFAULT_MATRIX)]) == 1


def test_unknown_criterion_and_missing_evidence_fail_closed(tmp_path: Path) -> None:
    payload = build_matrix()
    criterion = payload["criteria"][0]
    assert isinstance(criterion, dict)
    criterion["id"] = "A-99"
    evidence = criterion["objective_evidence"]
    assert isinstance(evidence, list)
    assert isinstance(evidence[0], dict)
    evidence[0]["path"] = "does/not/exist.py"

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "criterion_id_set_mismatch" in evaluation.findings
    assert any("evidence_path_missing" in item for item in evaluation.findings)
    assert not evaluation.complete


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version": 1, "schema_version": 2}\n', encoding="utf-8")

    with pytest.raises(DuplicateKeyError, match="duplicate_json_key"):
        load_matrix(path)


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_nonfinite_json_numbers_are_rejected(tmp_path: Path, constant: str) -> None:
    path = tmp_path / "nonfinite.json"
    path.write_text(f'{{"score": {constant}}}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="nonfinite_json_constant"):
        load_matrix(path)


def test_declared_score_cannot_override_computed_score(tmp_path: Path) -> None:
    payload = build_matrix()
    baseline = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))
    payload["declared_score"] = 100

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert evaluation.score == baseline.score
    assert "matrix_fields_invalid" in evaluation.findings
    assert not evaluation.complete


def test_fg06_caps_score_and_forbids_m5_awards(tmp_path: Path) -> None:
    payload = build_matrix()
    assessment = payload["assessment"]
    criterion = payload["criteria"][0]
    assert isinstance(assessment, dict)
    assert isinstance(criterion, dict)
    assessment["score_cap"] = 100
    criterion["maturity"] = "M5"
    history = criterion["assessment_history"]
    assert isinstance(history, list)
    final = history[-1]
    assert isinstance(final, dict)
    final["maturity"] = "M5"

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "fg06_score_cap_invalid" in evaluation.findings
    assert "fg06_m5_award_invalid" in evaluation.findings
    assert evaluation.verdict == "NOT_AN_INVESTMENT_RESEARCH_PLATFORM"
    assert not evaluation.complete


def test_evidence_hash_drift_is_rejected(tmp_path: Path) -> None:
    payload = build_matrix()
    criterion = payload["criteria"][0]
    assert isinstance(criterion, dict)
    evidence = criterion["objective_evidence"]
    assert isinstance(evidence, list)
    assert isinstance(evidence[0], dict)
    evidence[0]["path_sha256"] = "0" * 64

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert any("evidence_path" in item for item in evaluation.findings)
    assert not evaluation.complete


def test_evidence_paths_and_commands_cannot_escape_declared_binding(
    tmp_path: Path,
) -> None:
    payload = build_matrix()
    criterion = payload["criteria"][0]
    assert isinstance(criterion, dict)
    evidence = criterion["objective_evidence"]
    assert isinstance(evidence, list)
    item = evidence[0]
    assert isinstance(item, dict)
    item["path"] = "../../outside.py"
    item["command"] = str(item["command"]).replace(
        str(item["test"]), "tests/test_unrelated.py"
    )

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "A-01:evidence_path_outside_root" in evaluation.findings
    assert "A-01:evidence_command_binding_mismatch" in evaluation.findings
    assert not evaluation.complete


def test_fatal_gate_verification_command_must_bind_an_owned_test(
    tmp_path: Path,
) -> None:
    payload = build_matrix()
    gate = payload["fatal_gates"][0]
    assert isinstance(gate, dict)
    gate["verification_method"] = "pytest tests/test_unrelated.py"

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "FG-01:verification_method_binding_invalid" in evaluation.findings
    assert not evaluation.complete


def test_assessment_history_requires_every_declared_iteration(tmp_path: Path) -> None:
    payload = build_matrix()
    criterion = payload["criteria"][0]
    assert isinstance(criterion, dict)
    history = criterion["assessment_history"]
    assert isinstance(history, list)
    history.pop()

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "A-01:assessment_history_length_invalid" in evaluation.findings
    assert "A-01:assessment_history_iteration_sequence_invalid" in evaluation.findings
    assert not evaluation.complete


def test_assessment_history_final_state_is_bound_to_criterion(tmp_path: Path) -> None:
    payload = build_matrix()
    criterion = payload["criteria"][0]
    assert isinstance(criterion, dict)
    history = criterion["assessment_history"]
    assert isinstance(history, list)
    final = history[-1]
    assert isinstance(final, dict)
    final["diagnosis"] = "forged final diagnosis"

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "A-01:assessment_history_final_diagnosis_mismatch" in evaluation.findings
    assert not evaluation.complete


def test_assessment_history_rejects_incomplete_or_divergent_phase(
    tmp_path: Path,
) -> None:
    payload = build_matrix()
    criteria = payload["criteria"]
    assert isinstance(criteria, list)
    first = criteria[0]
    second = criteria[1]
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    first_history = first["assessment_history"]
    second_history = second["assessment_history"]
    assert isinstance(first_history, list)
    assert isinstance(second_history, list)
    first_entry = first_history[0]
    second_entry = second_history[0]
    assert isinstance(first_entry, dict)
    assert isinstance(second_entry, dict)
    first_entry.pop("phase")
    second_entry["phase"] = "criterion_specific_forged_phase"

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert "A-01:assessment_history_1_fields_invalid" in evaluation.findings
    assert "A-01:assessment_history_1_phase_invalid" in evaluation.findings
    assert "A-02:assessment_history_phase_sequence_mismatch" in evaluation.findings
    assert not evaluation.complete


@pytest.mark.parametrize(
    ("maturity", "status"),
    (("M5", "UNVERIFIED_EXTERNAL"), ("M3", "VERIFIED")),
)
def test_status_cannot_overstate_maturity(
    tmp_path: Path, maturity: str, status: str
) -> None:
    payload = build_matrix()
    criterion = payload["criteria"][0]
    assert isinstance(criterion, dict)
    criterion["maturity"] = maturity
    criterion["status"] = status

    evaluation = evaluate_matrix(_write_matrix(tmp_path / "matrix.json", payload))

    assert any("status_maturity_incoherent" in item for item in evaluation.findings)
    assert not evaluation.complete
