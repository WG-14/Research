from __future__ import annotations

import json

import pytest

from tools.platform_completeness import DEFAULT_MANIFEST, evaluate_manifest


RUBRIC_SHA256 = "5a457d1ba9c3b2f9afc74d1118c971d4e32089e26288a1c97ef322ba0756b8d5"


def test_canonical_research_only_matrix_has_all_215_judged_criteria() -> None:
    matrix = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    criteria = matrix["criteria"]
    blockers = matrix["blockers"]
    area_ids = [area["id"] for area in matrix["areas"]]
    criterion_ids = [criterion["id"] for criterion in criteria]

    assert matrix["canonical_source"] == {
        "title": "투자 연구 전용 플랫폼 레포 완성도 평가 기준 — 연구 한정판",
        "sha256": RUBRIC_SHA256,
        "instruction_sha256": (
            "25ddd87c30dce17b5c22c24096b5d8642375dc58570f8fa2dcbb67ce34a19396"
        ),
        "criterion_count": 215,
        "blocker_count": 11,
        "area_count": 16,
    }
    assert len(area_ids) == len(set(area_ids)) == 16
    assert sum(area["weight"] for area in matrix["areas"]) == 100
    assert len(criterion_ids) == len(set(criterion_ids)) == 215
    assert len(blockers) == 11
    assert all(criterion["current_status"] != "UNASSESSED" for criterion in criteria)
    assert all(len(criterion["assessment_history"]) == 10 for criterion in criteria)
    assert all(len(blocker["assessment_history"]) == 10 for blocker in blockers)
    assert all(
        set(criterion["evidence"]) == {
            "minimum_level",
            "paths",
            "commands",
            "receipts",
        }
        for criterion in criteria
    )


def test_completion_gate_fails_closed_on_remaining_partial_rows_and_receipts() -> None:
    matrix = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    evaluation = evaluate_manifest(DEFAULT_MANIFEST)
    codes = {(finding.subject, finding.code) for finding in evaluation.findings}

    assert evaluation.complete is False
    assert evaluation.expected_criteria == 215
    assert evaluation.verified_criteria == 0
    assert evaluation.declared_score == pytest.approx(
        matrix["assessment_summary"]["strict_weighted_score"]
    )
    assert matrix["assessment_summary"]["current_iteration"] == 10
    assert matrix["assessment_summary"]["blockers_passed"] == 11
    assert all(blocker["current_status"] == "PASS" for blocker in matrix["blockers"])
    assert not any(code == "blocker_not_cleared" for _subject, code in codes)
    assert ("UX-05", "criterion_not_full") in codes
    assert ("RSC-08", "declared_evidence_level_insufficient") in codes
    assert ("RSC-01", "receipt_hash_missing") in codes
