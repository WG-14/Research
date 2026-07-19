from __future__ import annotations

import json
from pathlib import Path

from tools.platform_completeness import evaluate_manifest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MATRIX = PROJECT_ROOT / "docs" / "research-platform-full-scope-evaluation-matrix.json"
RUBRIC_SHA256 = "13ab8fbd3c37a3095ca9fd2c69818c4cb7d5f85fdf96f9f27fedb626ba17d635"


def test_full_scope_matrix_contains_every_explicit_and_normative_criterion() -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    source = matrix["canonical_source"]
    ids = [row["id"] for row in matrix["criteria"]]

    assert source["sha256"] == RUBRIC_SHA256
    assert source["explicit_criterion_count"] == 268
    assert source["supplemental_normative_criterion_count"] == 163
    assert source["criterion_count"] == len(ids) == len(set(ids)) == 431
    assert source["blocker_count"] == 19
    assert [row["id"] for row in matrix["blockers"]] == [
        f"B-{number:02d}" for number in range(1, 20)
    ]
    assert all(
        {
            "exact_meaning",
            "ideal_state",
            "inspection_targets",
            "objective_evidence",
            "status_scale",
            "dependencies",
            "verification_method",
            "completion_condition",
            "baseline_assessment",
            "current_assessment",
            "assessment_history",
        }
        <= set(row)
        for row in matrix["criteria"]
    )
    assert matrix["assessment"]["iteration"] >= 2
    assert all(
        row["assessment_history"][-1] == row["current_assessment"]
        and len(row["assessment_history"]) == matrix["assessment"]["iteration"]
        for row in matrix["criteria"]
    )


def test_full_scope_completion_gate_reports_the_current_audit_as_incomplete() -> None:
    evaluation = evaluate_manifest(MATRIX)
    codes = {(finding.subject, finding.code) for finding in evaluation.findings}

    assert evaluation.expected_criteria == 431
    assert evaluation.verified_criteria == 0
    assert evaluation.complete is False
    assert ("B-04", "blocker_not_cleared") in codes
    assert ("B-19", "blocker_not_cleared") not in codes
    assert ("B-12", "blocker_not_cleared") not in codes
    assert ("B-13", "blocker_not_cleared") not in codes
    assert ("S4-F01", "criterion_not_full") in codes
    assert ("S4-O01", "criterion_not_full") in codes


def test_full_scope_completion_gate_fails_closed_on_stale_assessment(tmp_path: Path) -> None:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    matrix["assessment"]["iteration"] = 1
    matrix["criteria"][0].pop("current_assessment")
    candidate = tmp_path / "stale-matrix.json"
    candidate.write_text(json.dumps(matrix), encoding="utf-8")

    evaluation = evaluate_manifest(candidate)
    codes = {(finding.subject, finding.code) for finding in evaluation.findings}

    assert ("manifest", "assessment_stale") in codes
    assert ("S1-C01", "current_assessment_missing") in codes
