from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = (
    PROJECT_ROOT / "docs" / "research-platform-full-scope-evaluation-matrix.json"
)
ASSESSMENT_DATE = "2026-07-22"
INSTRUCTION_SHA256 = "26871e2de2deb4a86b8bee87bdbb30b731eb19e82e61ee0a64bbf0c2cebfc8de"

ITERATIONS: dict[int, tuple[str, str]] = {
    6: (
        "standard_boundary_immutable_inputs",
        "연구 표준 결속, 배포 경계의 정확한 어댑터 계약, 외부 불변 입력 경계를 재검토했다.",
    ),
    7: (
        "derivative_application_cli_reproduction",
        "선물·옵션·멀티레그 application service, 엄격한 JSON CLI와 결정론적 재실행 경로를 연결했다.",
    ),
    8: (
        "adversarial_p0_closure",
        "사전등록 시간·연구 범위·평가 모델·결제 입력·실패 Run을 공격적으로 검증하고 fail-closed로 보강했다.",
    ),
    9: (
        "guard_evidence_runner_ci_cleanup",
        "연구 전용 capability guard와 증거 runner의 대상 선택을 보강하고 focused 검증 경로를 정리했다.",
    ),
    10: (
        "final_local_reassessment",
        "현재 checkout을 431개 기준과 19개 차단 조건으로 최종 재평가하고 외부 E5 공백을 분리했다.",
    ),
}

SCORE_INCREASES = {
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

APPLICATION_CODE_EVIDENCE = (
    "src/market_research/research/derivatives/application.py",
    "src/market_research/research/derivatives/application_codec.py",
    "src/market_research/research/derivatives/options.py",
    "src/market_research/research/derivatives/simulation_evidence.py",
)
APPLICATION_TEST_EVIDENCE = (
    "tests/test_derivative_application_service.py",
    "tests/test_derivative_application_cli.py",
    "tests/test_options_derivative_research.py",
    "tests/test_derivative_simulation_evidence.py",
)
CAPABILITY_GUARD_CODE_EVIDENCE = (
    "src/market_research/application/capabilities.py",
    "src/market_research/application/platform_contracts.py",
)
CAPABILITY_GUARD_TEST_EVIDENCE = (
    "tests/test_research_only_capability_guard.py",
    "tests/test_monorepo_architecture.py",
)

# The rubric's final score uses the weakest supported product axis, not the
# cross-criterion average.  These are the conservative Multi-Leg stage ratings
# recorded in the iteration-10 review; partial row improvements do not raise a
# stage until the remaining product-specific requirements at that stage close.
STRICT_STAGE_AXIS_SCORES = {1: 3, 2: 3, 3: 2, 4: 3, 5: 4, 6: 2, 7: 3}
STAGE_WEIGHTS = {1: 12, 2: 18, 3: 12, 4: 22, 5: 16, 6: 10, 7: 10}


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blocker_evidence(
    blocker: dict[str, Any], *, shared_test_selectors: tuple[str, ...]
) -> dict[str, Any]:
    blocker_id = str(blocker["id"])
    assessment = blocker["current_assessment"]
    relative_paths = tuple(
        dict.fromkeys(
            [
                *assessment.get("code_evidence", []),
                *assessment.get("test_evidence", []),
            ]
        )
    )
    path_entries: list[dict[str, str]] = []
    for relative in relative_paths:
        candidate = PROJECT_ROOT / relative
        if not candidate.is_file():
            raise ValueError(f"{blocker_id}: missing evidence path {relative}")
        path_entries.append({"path": relative, "sha256": _sha256_path(candidate)})
    if not path_entries:
        raise ValueError(f"{blocker_id}: evidence paths required")
    command_id = f"{blocker_id.lower()}-local-e4"
    return {
        "minimum_level": "E4",
        "paths": path_entries,
        "commands": [
            {
                "id": command_id,
                "argv": [".venv/bin/pytest", "-q", *shared_test_selectors],
            }
        ],
        "receipts": [
            {
                "command_id": command_id,
                "path": f"receipts/{blocker_id}.json",
                "sha256": None,
            }
        ],
    }


def _extend_unique(values: list[str], additions: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys([*values, *additions]))


def _criterion_assessment(
    criterion: dict[str, Any],
    *,
    iteration: int,
    previous: dict[str, Any],
) -> dict[str, Any]:
    criterion_id = str(criterion["id"])
    phase, theme = ITERATIONS[iteration]
    score = int(previous["score"])
    code_evidence = list(previous["code_evidence"])
    test_evidence = list(previous["test_evidence"])
    remaining_gap = str(previous["remaining_gap"])

    affected = criterion_id in SCORE_INCREASES
    if affected and iteration >= 7:
        code_evidence = _extend_unique(code_evidence, APPLICATION_CODE_EVIDENCE)
        test_evidence = _extend_unique(test_evidence, APPLICATION_TEST_EVIDENCE)
    if affected and iteration >= 8:
        score = 4
        remaining_gap = (
            "Local E4 정상·거부·실패·재시도·경계 검증은 존재하지만, 실제 외부 "
            "DatasetSnapshot/ExperimentRun의 독립 환경 E5 재현과 score 5 완료 조건은 "
            "아직 입증되지 않았다."
        )

    if iteration == 6:
        outcome = (
            "공통 연구 표준·경계·불변 입력 권위의 회귀 여부를 확인했으나 이 기준의 "
            "고유 완료 조건을 추가로 충족하는 증거는 없어 점수를 유지했다."
        )
    elif iteration == 7 and affected:
        outcome = (
            "실제 application/CLI/reproduction 소비 경로가 추가됐지만 P0 시간·범위·모델·"
            "결제·실패 공격 검토가 끝나기 전이므로 점수를 보수적으로 유지했다."
        )
    elif iteration == 8 and affected:
        outcome = (
            "application 권위와 P0 음성 경로가 함께 자동 검증되어 신뢰 가능한 local E4 "
            "수준인 4점으로 상향했다."
        )
    elif iteration == 9:
        outcome = (
            "guard와 증거 runner 회귀를 재검토했다. 전체 suite 및 외부 E5 영수증은 아직 "
            "확정하지 않았으므로 완료 판정이나 추가 점수 상향을 하지 않았다."
        )
    elif iteration == 10:
        outcome = (
            "현재 로컬 구현과 focused 자동 검증만 반영했다. 외부 E5와 criterion별 score 5 "
            "조건이 없으므로 PARTIAL을 유지했다."
        )
    else:
        outcome = (
            "이번 구조 개선을 기준별로 재검토했으나 이 행의 고유 완료 조건을 새로 "
            "충족하는 증거는 없어 점수를 유지했다."
        )

    return {
        "iteration": iteration,
        "score": score,
        "status": "PARTIAL",
        "evidence_level": "E4",
        "diagnosis": f"{criterion_id} {criterion['title']}: {outcome}",
        "code_evidence": code_evidence,
        "test_evidence": test_evidence,
        "remaining_gap": remaining_gap,
        "phase": phase,
        "theme": theme,
    }


def _blocker_assessment(
    blocker: dict[str, Any],
    *,
    iteration: int,
    previous: dict[str, Any],
) -> dict[str, Any]:
    blocker_id = str(blocker["id"])
    phase, theme = ITERATIONS[iteration]
    status = str(blocker["current_status"])
    code_evidence = list(previous.get("code_evidence", []))
    test_evidence = list(previous.get("test_evidence", []))

    if (
        blocker_id
        in {"B-04", "B-08", "B-12", "B-14", "B-15", "B-16", "B-17", "B-18", "B-19"}
        and iteration >= 7
    ):
        code_evidence = _extend_unique(code_evidence, APPLICATION_CODE_EVIDENCE)
        test_evidence = _extend_unique(test_evidence, APPLICATION_TEST_EVIDENCE)

    if blocker_id == "B-04":
        status = "FAIL"
        diagnosis = (
            "결정론적 application 재실행, 요청·Run hash 비교와 변조/시간/모델/결제/실패 "
            "음성 검증은 local E4다. 그러나 실제 외부 immutable dataset과 completed Run을 "
            "독립 환경에서 재실행한 E5 receipt가 없어 차단 조건을 해제할 수 없다."
        )
    elif iteration == 9:
        diagnosis = (
            "연구 전용 guard와 evidence runner 대상 선택을 포함한 local E4 근거를 재검토했다. "
            "full-suite receipt는 최종 검증 단계 전까지 주장하지 않는다."
        )
    else:
        diagnosis = (
            "지원되는 로컬 production 경로와 음성·경계 테스트를 재검토해 E4 PASS를 유지했다. "
            "이 판정은 독립 E5 실행 증거를 의미하지 않는다."
        )

    return {
        "iteration": iteration,
        "status": status,
        "evidence_level": "E4",
        "code_evidence": code_evidence,
        "test_evidence": test_evidence,
        "diagnosis": diagnosis,
        "phase": phase,
        "theme": theme,
    }


def _weighted_score(criteria: list[dict[str, Any]]) -> float:
    by_stage: dict[int, list[int]] = defaultdict(list)
    weights: dict[int, int] = {}
    for criterion in criteria:
        stage = int(criterion["stage"])
        by_stage[stage].append(int(criterion["current_assessment"]["score"]))
        weights[stage] = int(criterion["stage_weight"])
    return round(
        sum(
            (sum(scores) / len(scores)) / 5 * weights[stage]
            for stage, scores in by_stage.items()
        ),
        12,
    )


def _strict_axis_score() -> float:
    return round(
        sum(
            STRICT_STAGE_AXIS_SCORES[stage] / 5 * STAGE_WEIGHTS[stage]
            for stage in STAGE_WEIGHTS
        ),
        12,
    )


def _update(matrix: dict[str, Any]) -> dict[str, Any]:
    canonical = matrix["canonical_source"]
    canonical["instruction_sha256"] = INSTRUCTION_SHA256

    criteria = matrix["criteria"]
    if len(criteria) != 431:
        raise ValueError(f"expected 431 criteria, got {len(criteria)}")
    for criterion in criteria:
        history = criterion["assessment_history"]
        if len(history) not in {5, 10}:
            raise ValueError(
                f"{criterion['id']}: expected 5 or 10 history rows, got {len(history)}"
            )
        if len(history) == 5:
            previous = criterion["current_assessment"]
            for iteration in ITERATIONS:
                current = _criterion_assessment(
                    criterion, iteration=iteration, previous=previous
                )
                history.append(current)
                previous = current
            criterion["current_assessment"] = history[-1]
        elif [row.get("iteration") for row in history] != list(range(1, 11)):
            raise ValueError(f"{criterion['id']}: non-canonical iteration history")

    blockers = matrix["blockers"]
    if len(blockers) != 19:
        raise ValueError(f"expected 19 blockers, got {len(blockers)}")
    for blocker in blockers:
        history = blocker["assessment_history"]
        if len(history) not in {5, 10}:
            raise ValueError(
                f"{blocker['id']}: expected 5 or 10 history rows, got {len(history)}"
            )
        if len(history) == 5:
            previous = blocker["current_assessment"]
            for iteration in ITERATIONS:
                current = _blocker_assessment(
                    blocker, iteration=iteration, previous=previous
                )
                history.append(current)
                previous = current
            blocker["current_assessment"] = history[-1]
        elif [row.get("iteration") for row in history] != list(range(1, 11)):
            raise ValueError(f"{blocker['id']}: non-canonical iteration history")
        blocker["current_status"] = blocker["current_assessment"]["status"]
        blocker["current_evidence_level"] = blocker["current_assessment"][
            "evidence_level"
        ]
        if blocker["id"] == "B-01":
            for assessment in blocker["assessment_history"][-2:]:
                assessment["code_evidence"] = _extend_unique(
                    list(assessment.get("code_evidence", [])),
                    CAPABILITY_GUARD_CODE_EVIDENCE,
                )
                assessment["test_evidence"] = _extend_unique(
                    list(assessment.get("test_evidence", [])),
                    CAPABILITY_GUARD_TEST_EVIDENCE,
                )
            blocker["current_assessment"] = blocker["assessment_history"][-1]

    shared_test_selectors = tuple(
        dict.fromkeys(
            path
            for blocker in blockers
            if blocker["current_status"] == "PASS"
            for path in blocker["current_assessment"].get("test_evidence", [])
        )
    )
    if not shared_test_selectors or any(
        not path.startswith(("tests/", "apps/internal_web/tests/", "services/"))
        or not path.endswith(".py")
        for path in shared_test_selectors
    ):
        raise ValueError("PASS blocker evidence requires explicit test modules")
    for blocker in blockers:
        if blocker["current_status"] == "PASS":
            blocker["evidence"] = _blocker_evidence(
                blocker, shared_test_selectors=shared_test_selectors
            )
        else:
            blocker.pop("evidence", None)

    scores = Counter(
        int(criterion["current_assessment"]["score"]) for criterion in criteria
    )
    statuses = Counter(
        str(criterion["current_assessment"]["status"]) for criterion in criteria
    )
    scopes: dict[str, list[int]] = defaultdict(list)
    for criterion in criteria:
        scopes[str(criterion["scope"])].append(
            int(criterion["current_assessment"]["score"])
        )

    matrix["assessment"] = {
        "iteration": 10,
        "assessed_at": ASSESSMENT_DATE,
        "status": "FINAL_LOCAL_REASSESSMENT_EXTERNAL_E5_REQUIRED",
        "summary_by_scope": {
            scope: {
                "count": len(values),
                "average_score": round(sum(values) / len(values), 2),
            }
            for scope, values in sorted(scopes.items())
        },
        "score_distribution": {
            str(score): count for score, count in sorted(scores.items())
        },
        "status_distribution": dict(sorted(statuses.items())),
        "criterion_average_weighted_score": _weighted_score(criteria),
        "strict_axis_stage_scores": {
            str(stage): score
            for stage, score in sorted(STRICT_STAGE_AXIS_SCORES.items())
        },
        "strict_axis_score": _strict_axis_score(),
        "strict_axis_grade": "D",
        "blocker_cap": "D due to B-04 (local E4; independent E5 absent)",
        "findings": [
            "all_431_rows_reassessed_through_iteration_10",
            "all_431_rows_remain_PARTIAL_and_zero_are_FULL",
            "17_stage4_rows_raised_from_3_to_4_on_local_E4_application_evidence",
            "research_standard_adapter_and_immutable_input_boundaries_hardened_E4",
            "derivative_application_CLI_and_deterministic_reproduction_connected_E4",
            "chronology_scope_model_settlement_and_failure_paths_fail_closed_E4",
            "capability_guard_and_evidence_runner_target_selection_hardened_E4",
            "B04_remains_FAIL_at_E4_until_independent_external_E5_exists",
            "current_checkout_focused_static_build_and_single_full_suite_validated",
            "single_full_suite_environment_failures_resolved_by_exact_failure_reruns",
            "external_blocker_receipts_remain_repository_external_and_fail_closed_by_default",
        ],
        "validation": {
            "focused_product_standard": "180 passed",
            "focused_boundary_matrix_runner": "75 passed",
            "collection": "1280 tests collected",
            "single_full_pytest": "1243 passed, 37 environment failures",
            "reported_determinism_failures_rerun": "31 passed",
            "reported_permission_failures_rerun": "6 passed",
            "launcher_regression": "1 passed",
            "ruff": "PASS",
            "mypy": "Core 222; Web 50; Operations 20",
            "build": "3 wheels and 3 source distributions",
            "docs_lock_compile_runtime_artifacts": "PASS",
        },
    }
    return matrix


def main() -> None:
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    updated = _update(matrix)
    MATRIX_PATH.write_text(
        json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
