from __future__ import annotations

import json

from tools.platform_completeness import (
    DEFAULT_MANIFEST,
    evaluate_manifest,
    render_report,
    sha256_path,
)


RUBRIC_SHA256 = "13ab8fbd3c37a3095ca9fd2c69818c4cb7d5f85fdf96f9f27fedb626ba17d635"
INSTRUCTION_SHA256 = "26871e2de2deb4a86b8bee87bdbb30b731eb19e82e61ee0a64bbf0c2cebfc8de"


def test_default_matrix_is_the_full_scope_431_criterion_rubric() -> None:
    matrix = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    criteria = matrix["criteria"]
    blockers = matrix["blockers"]
    criterion_ids = [criterion["id"] for criterion in criteria]

    assert matrix["canonical_source"] == {
        "title": (
            "투자 연구 전용 플랫폼 레포 완성도 평가 기준 — "
            "연구 한정 · 현물·선물·옵션 전 범위 평가판"
        ),
        "sha256": RUBRIC_SHA256,
        "instruction_sha256": INSTRUCTION_SHA256,
        "explicit_criterion_count": 268,
        "supplemental_normative_criterion_count": 163,
        "criterion_count": 431,
        "blocker_count": 19,
    }
    assert len(criterion_ids) == len(set(criterion_ids)) == 431
    assert [blocker["id"] for blocker in blockers] == [
        f"B-{number:02d}" for number in range(1, 20)
    ]
    assert {criterion["scope"] for criterion in criteria} == {
        "CORE",
        "SPOT",
        "FUTURES",
        "OPTIONS",
        "DERIVATIVES_PORTFOLIO",
        "DERIVATIVES_RISK",
    }


def test_default_completion_gate_fails_closed_without_full_e4_e5_evidence() -> None:
    evaluation = evaluate_manifest(DEFAULT_MANIFEST)
    codes = {finding.code for finding in evaluation.findings}

    assert evaluation.complete is False
    assert evaluation.expected_criteria == 431
    assert evaluation.verified_criteria == 0
    assert 0.0 <= evaluation.declared_score < 100.0
    assert evaluation.strict_score == 58.8
    report = render_report(evaluation)
    assert "Final strict weak-axis score: **58.80/100**" in report
    assert "Criterion-average declared score: **69.87/100**" in report
    assert "Strict declared score: **69.87/100**" not in report
    assert "Open blocking conditions: B-01, B-02" in report
    assert "B-19" in report
    assert "criterion_not_full" in codes
    assert any(finding.subject.startswith("B-") for finding in evaluation.findings)


def test_default_matrix_records_ten_honest_reassessments() -> None:
    matrix = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    criteria = matrix["criteria"]
    blockers = {blocker["id"]: blocker for blocker in matrix["blockers"]}

    assert matrix["assessment"]["iteration"] == 10
    assert matrix["assessment"]["score_distribution"] == {
        "2": 12,
        "3": 194,
        "4": 225,
    }
    assert matrix["assessment"]["status_distribution"] == {"PARTIAL": 431}
    assert matrix["assessment"]["criterion_average_weighted_score"] == (69.871885204524)
    assert matrix["assessment"]["strict_axis_stage_scores"] == {
        "1": 3,
        "2": 3,
        "3": 2,
        "4": 3,
        "5": 4,
        "6": 2,
        "7": 3,
    }
    assert matrix["assessment"]["strict_axis_score"] == 58.8
    raised_ids = {
        criterion["id"]
        for criterion in criteria
        if criterion["assessment_history"][4]["score"]
        != criterion["current_assessment"]["score"]
    }
    assert raised_ids == {
        "S4-C01",
        "S4-C02",
        "S4-C06",
        "S4-O01",
        "S4-O02",
        "S4-O03",
        "S4-O04",
        "S4-O05",
        "S4-O07",
        "S4-O11",
        "S4-O12",
        "S4-O14",
        "S4-OM01",
        "S4-OM02",
        "S4-OM03",
        "S4-OM04",
        "S4-OM05",
    }
    assert all(
        [row["iteration"] for row in criterion["assessment_history"]]
        == list(range(1, 11))
        for criterion in criteria
    )
    assert all(
        criterion["current_assessment"]["status"] == "PARTIAL" for criterion in criteria
    )
    assert blockers["B-04"]["current_status"] == "FAIL"
    assert blockers["B-04"]["current_evidence_level"] == "E4"
    assert {
        blocker_id
        for blocker_id, blocker in blockers.items()
        if blocker["current_status"] != "PASS"
    } == {"B-04"}
    pass_blockers = [
        blocker
        for blocker in blockers.values()
        if blocker["current_status"] == "PASS"
    ]
    assert len(pass_blockers) == 18
    assert all(
        blocker["evidence"]["minimum_level"] == "E4"
        for blocker in pass_blockers
    )
    repository_root = DEFAULT_MANIFEST.parents[1]
    assert all(
        sha256_path(repository_root / entry["path"]) == entry["sha256"]
        for blocker in pass_blockers
        for entry in blocker["evidence"]["paths"]
    )
    receipt_paths = [
        receipt["path"]
        for blocker in pass_blockers
        for receipt in blocker["evidence"]["receipts"]
    ]
    assert len(receipt_paths) == len(set(receipt_paths)) == 18
    assert len(
        {
            tuple(command["argv"])
            for blocker in pass_blockers
            for command in blocker["evidence"]["commands"]
        }
    ) == 1
    assert "tests/test_research_only_capability_guard.py" in blockers["B-01"][
        "current_assessment"
    ]["test_evidence"]
    assert "evidence" not in blockers["B-04"]
    assert [row["iteration"] for row in blockers["B-04"]["assessment_history"]] == list(
        range(1, 11)
    )
