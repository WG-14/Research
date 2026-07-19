from __future__ import annotations

import json

from tools.platform_completeness import DEFAULT_MANIFEST, evaluate_manifest


RUBRIC_SHA256 = "13ab8fbd3c37a3095ca9fd2c69818c4cb7d5f85fdf96f9f27fedb626ba17d635"
INSTRUCTION_SHA256 = (
    "25ddd87c30dce17b5c22c24096b5d8642375dc58570f8fa2dcbb67ce34a19396"
)


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
    assert "criterion_not_full" in codes
    assert any(finding.subject.startswith("B-") for finding in evaluation.findings)
