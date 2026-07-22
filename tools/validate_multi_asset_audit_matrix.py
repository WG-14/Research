#!/usr/bin/env python3
"""Validate the frozen A--N multi-asset research audit matrix.

This validator is deliberately fail-closed.  It verifies the canonical document
copies, source hashes, exact rubric section text, identifier sets, assessment
schema, dependency references, repository inspection targets, and recomputed
baseline score.  It does not implement or promote any product capability.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, NoReturn, TypedDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = PROJECT_ROOT / "docs/multi-asset-investment-research-audit-matrix.json"
RUBRIC_COPY = "docs/multi-asset-investment-research-audit-rubric.md"
INSTRUCTION_COPY = "docs/multi-asset-investment-research-audit-instructions.md"
RUBRIC_ATTACHMENT_SHA256 = (
    "db0ef81e43bb09e47d7be2ed30cd819d05ec018bc8e488a4a2c83966faae9f3b"
)
RUBRIC_NORMALIZED_SHA256 = (
    "b68838c53d0e14bc67bd21e56ee662b2221c183796590bccfde2bc8cec531bff"
)
INSTRUCTION_ATTACHMENT_SHA256 = (
    "26871e2de2deb4a86b8bee87bdbb30b731eb19e82e61ee0a64bbf0c2cebfc8de"
)
INSTRUCTION_COPY_SHA256 = (
    "2e6d7b9719ab685af60743240278d1fcba82409fc51396673dedb4ea56a328bc"
)
EXPECTED_AREA_COUNTS = {
    "A": 5,
    "B": 9,
    "C": 13,
    "D": 11,
    "E": 16,
    "F": 25,
    "G": 6,
    "H": 7,
    "I": 7,
    "J": 8,
    "K": 8,
    "L": 6,
    "M": 10,
    "N": 9,
}
EXPECTED_WEIGHTS = {
    "A": 6,
    "B": 6,
    "C": 12,
    "D": 8,
    "E": 12,
    "F": 16,
    "G": 6,
    "H": 6,
    "I": 5,
    "J": 6,
    "K": 5,
    "L": 4,
    "M": 4,
    "N": 4,
}
EXPECTED_STATUS_SCORES = {
    "ABSENT": 0,
    "NOMINAL": 1,
    "PARTIAL": 2,
    "SUBSTANTIAL": 3,
    "COMPLETE": 4,
    "NOT_APPLICABLE": None,
    "UNKNOWN": None,
}
EXPECTED_EVIDENCE_LEVELS = {f"E{number}" for number in range(7)}
EXPECTED_TOP_LEVEL_FIELDS = {
    "schema_version",
    "matrix_id",
    "title",
    "canonical_source",
    "counts",
    "scoring_policy",
    "initial_assessment_summary",
    "criteria",
    "critical_fail_gates",
    "end_to_end_scenarios",
}
REQUIRED_CRITERION_FIELDS = {
    "id",
    "area",
    "area_name",
    "area_weight",
    "title",
    "source",
    "exact_meaning",
    "ideal_state",
    "inspection_targets",
    "objective_evidence",
    "status_scale",
    "dependencies",
    "verification",
    "completion_condition",
    "conservative_initial_assessment",
}
REQUIRED_GATE_FIELDS = {
    "id",
    "title",
    "source",
    "exact_meaning",
    "trigger_policy",
    "inspection_targets",
    "objective_evidence",
    "dependencies",
    "verification",
    "completion_condition",
    "conservative_initial_assessment",
}
REQUIRED_SCENARIO_FIELDS = {
    "id",
    "title",
    "source",
    "exact_meaning",
    "ideal_state",
    "inspection_targets",
    "objective_evidence",
    "status_scale",
    "dependencies",
    "verification",
    "completion_condition",
    "conservative_initial_assessment",
}
LEGACY_DOCS_THAT_MUST_REMAIN = {
    "docs/investment-research-platform-audit-rubric.md",
    "docs/investment-research-platform-audit-instructions.md",
    "docs/research-platform-evaluation-matrix.json",
    "docs/research-platform-full-scope-evaluation-matrix.json",
}


class MatrixValidationError(ValueError):
    """The audit matrix or its frozen source documents are inconsistent."""


class RubricSection(TypedDict):
    groups: tuple[str, ...]
    heading: str
    start_line: int
    end_line: int
    body: str


def _fail(message: str) -> NoReturn:
    raise MatrixValidationError(message)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail(f"duplicate_json_key:{key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    _fail(f"nonfinite_json_constant:{value}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail(f"matrix_load_failed:{exc}")
    if not isinstance(value, dict):
        _fail("matrix_root_must_be_object")
    return value


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _fail(f"nonempty_trimmed_text_required:{field}")
    return value


def _text_list(value: object, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        _fail(f"text_list_required:{field}")
    for index, item in enumerate(value):
        _text(item, f"{field}[{index}]")
    return value


def _expected_ids() -> list[str]:
    return [
        f"{area}-{number:02d}"
        for area, count in EXPECTED_AREA_COUNTS.items()
        for number in range(1, count + 1)
    ]


def _parse_sections(path: Path, pattern: str) -> list[RubricSection]:
    lines = path.read_text(encoding="utf-8").splitlines()
    result: list[RubricSection] = []
    for index, line in enumerate(lines):
        match = re.fullmatch(pattern, line)
        if match is None:
            continue
        body: list[str] = []
        end = index + 1
        while end < len(lines):
            if re.fullmatch(pattern, lines[end]) or re.match(r"^# (?!#)", lines[end]):
                break
            body.append(lines[end])
            end += 1
        while body and (not body[-1].strip() or body[-1].strip() == "---"):
            body.pop()
        while body and not body[0].strip():
            body.pop(0)
        result.append(
            {
                "groups": match.groups(),
                "heading": line,
                "start_line": index + 1,
                "end_line": end,
                "body": "\n".join(body),
            }
        )
    return result


def _validate_frozen_sources(root: Path, matrix: dict[str, Any]) -> dict[str, str]:
    canonical = matrix.get("canonical_source")
    if not isinstance(canonical, dict):
        _fail("canonical_source_must_be_object")
    if canonical.get("policy") != (
        "이 파일은 이 작업의 현재 단일 평가 권위이며, "
        "기존 A–J/431 문서는 별도의 역사적 증거로 보존하되 "
        "현재 판정 권위로 사용하지 않는다."
    ):
        _fail("current_single_authority_policy_changed")
    rubric_meta = canonical.get("rubric")
    instruction_meta = canonical.get("instructions")
    if not isinstance(rubric_meta, dict) or not isinstance(instruction_meta, dict):
        _fail("canonical_source_entries_must_be_objects")

    rubric_path = root / RUBRIC_COPY
    instruction_path = root / INSTRUCTION_COPY
    try:
        rubric_bytes = rubric_path.read_bytes()
        instruction_bytes = instruction_path.read_bytes()
    except OSError as exc:
        _fail(f"frozen_source_missing:{exc}")
    if b"\r" in rubric_bytes or b"\r" in instruction_bytes:
        _fail("frozen_sources_must_use_lf_only")
    rubric_hash = _sha256_bytes(rubric_bytes)
    instruction_hash = _sha256_bytes(instruction_bytes)
    if rubric_hash != RUBRIC_NORMALIZED_SHA256:
        _fail(f"rubric_copy_hash_mismatch:{rubric_hash}")
    if instruction_hash != INSTRUCTION_COPY_SHA256:
        _fail(f"instruction_copy_hash_mismatch:{instruction_hash}")
    if not instruction_bytes.endswith(b"\n") or instruction_bytes.endswith(b"\n\n"):
        _fail("instruction_patch_copy_must_have_one_terminal_lf")
    if _sha256_bytes(instruction_bytes[:-1]) != INSTRUCTION_ATTACHMENT_SHA256:
        _fail("instruction_copy_content_differs_beyond_patch_terminal_lf")

    expected_meta = {
        "rubric_attachment": (
            rubric_meta.get("attachment_original_sha256"),
            RUBRIC_ATTACHMENT_SHA256,
        ),
        "rubric_normalized": (
            rubric_meta.get("crlf_normalized_sha256"),
            RUBRIC_NORMALIZED_SHA256,
        ),
        "rubric_copy": (rubric_meta.get("repository_copy_sha256"), rubric_hash),
        "instruction_attachment": (
            instruction_meta.get("attachment_original_sha256"),
            INSTRUCTION_ATTACHMENT_SHA256,
        ),
        "instruction_normalized": (
            instruction_meta.get("crlf_normalized_sha256"),
            INSTRUCTION_ATTACHMENT_SHA256,
        ),
        "instruction_copy": (
            instruction_meta.get("repository_copy_sha256"),
            instruction_hash,
        ),
        "instruction_content": (
            instruction_meta.get("repository_text_without_patch_terminal_lf_sha256"),
            INSTRUCTION_ATTACHMENT_SHA256,
        ),
    }
    for name, (actual, expected) in expected_meta.items():
        if actual != expected:
            _fail(f"canonical_hash_metadata_mismatch:{name}")
    if (
        rubric_meta.get("repository_copy") != RUBRIC_COPY
        or instruction_meta.get("repository_copy") != INSTRUCTION_COPY
    ):
        _fail("canonical_source_path_changed")
    for legacy in LEGACY_DOCS_THAT_MUST_REMAIN:
        if not (root / legacy).is_file():
            _fail(f"legacy_reference_was_removed:{legacy}")
    return {"rubric_sha256": rubric_hash, "instructions_sha256": instruction_hash}


def _validate_source_binding(
    row: dict[str, Any],
    expected: RubricSection,
    expected_id: str,
    expected_title: str,
) -> None:
    if row.get("id") != expected_id:
        _fail(f"unexpected_row_id:{row.get('id')}:{expected_id}")
    if row.get("title") != expected_title:
        _fail(f"title_differs_from_rubric:{expected_id}")
    if row.get("exact_meaning") != expected["body"]:
        _fail(f"exact_meaning_differs_from_rubric:{expected_id}")
    source = row.get("source")
    if not isinstance(source, dict):
        _fail(f"source_must_be_object:{expected_id}")
    expected_source = {
        "document": RUBRIC_COPY,
        "heading": expected["heading"],
        "start_line": expected["start_line"],
        "end_line": expected["end_line"],
    }
    if source != expected_source:
        _fail(f"source_location_differs_from_rubric:{expected_id}")


def _validate_paths(root: Path, values: object, field: str) -> list[str]:
    paths = _text_list(values, field)
    for value in paths:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            _fail(f"unsafe_repository_target:{field}:{value}")
        if not (root / path).exists():
            _fail(f"repository_target_missing:{field}:{value}")
    return paths


def _validate_common_record(
    root: Path,
    row: dict[str, Any],
    required_fields: set[str],
    all_criterion_ids: set[str],
) -> None:
    if set(row) != required_fields:
        _fail(f"record_fields_mismatch:{row.get('id')}")
    row_id = _text(row.get("id"), "id")
    _text(row.get("title"), f"{row_id}.title")
    _text(row.get("exact_meaning"), f"{row_id}.exact_meaning")
    _text(row.get("completion_condition"), f"{row_id}.completion_condition")
    _validate_paths(root, row.get("inspection_targets"), f"{row_id}.inspection_targets")
    _text_list(row.get("objective_evidence"), f"{row_id}.objective_evidence")
    dependencies = _text_list(
        row.get("dependencies"), f"{row_id}.dependencies", allow_empty=True
    )
    if row_id in dependencies:
        _fail(f"self_dependency:{row_id}")
    unknown = set(dependencies) - all_criterion_ids
    if unknown:
        _fail(f"unknown_dependencies:{row_id}:{sorted(unknown)}")
    verification = row.get("verification")
    if not isinstance(verification, dict):
        _fail(f"verification_must_be_object:{row_id}")
    _text(verification.get("method"), f"{row_id}.verification.method")
    focused = _validate_paths(
        root, verification.get("focused_tests"), f"{row_id}.verification.focused_tests"
    )
    if any(not path.startswith("tests/") for path in focused):
        _fail(f"focused_test_outside_tests:{row_id}")
    assessment = row.get("conservative_initial_assessment")
    if not isinstance(assessment, dict):
        _fail(f"assessment_must_be_object:{row_id}")
    if assessment.get("assessed_at") != "2026-07-22":
        _fail(f"assessment_date_changed:{row_id}")
    if assessment.get("evidence_level") not in EXPECTED_EVIDENCE_LEVELS:
        _fail(f"invalid_evidence_level:{row_id}")
    _text(assessment.get("finding"), f"{row_id}.assessment.finding")
    _text(assessment.get("assessment_scope"), f"{row_id}.assessment.scope")


def _validate_criteria(
    root: Path, matrix: dict[str, Any], rubric_path: Path
) -> list[dict[str, Any]]:
    rows = matrix.get("criteria")
    if not isinstance(rows, list):
        _fail("criteria_must_be_list")
    expected_ids = _expected_ids()
    if [
        row.get("id") if isinstance(row, dict) else None for row in rows
    ] != expected_ids:
        _fail("criterion_ids_or_order_mismatch")
    expected_sections = _parse_sections(rubric_path, r"## ([A-N])-([0-9]{2})\.\s*(.+)")
    if len(expected_sections) != 140 or len(rows) != 140:
        _fail("criterion_count_must_be_140")
    all_ids = set(expected_ids)
    for row, expected in zip(rows, expected_sections, strict=True):
        if not isinstance(row, dict):
            _fail("criterion_row_must_be_object")
        area, digits, title = expected["groups"]
        row_id = f"{area}-{digits}"
        _validate_source_binding(row, expected, row_id, title)
        _validate_common_record(root, row, REQUIRED_CRITERION_FIELDS, all_ids)
        if row.get("area") != area or row.get("area_weight") != EXPECTED_WEIGHTS[area]:
            _fail(f"criterion_area_metadata_mismatch:{row_id}")
        _text(row.get("area_name"), f"{row_id}.area_name")
        _text(row.get("ideal_state"), f"{row_id}.ideal_state")
        if row.get("status_scale") != "#/scoring_policy/status_scale":
            _fail(f"criterion_status_scale_reference_changed:{row_id}")
        verification = row["verification"]
        _text(verification.get("command"), f"{row_id}.verification.command")
        _text(
            verification.get("required_evidence_level"),
            f"{row_id}.verification.required_evidence_level",
        )
        for test_path in verification["focused_tests"]:
            if test_path not in verification["command"]:
                _fail(f"focused_test_missing_from_command:{row_id}:{test_path}")
        assessment = row["conservative_initial_assessment"]
        status = assessment.get("status")
        if status not in EXPECTED_STATUS_SCORES:
            _fail(f"invalid_initial_status:{row_id}")
        if assessment.get("score") != EXPECTED_STATUS_SCORES[status]:
            _fail(f"status_score_mismatch:{row_id}")
        _validate_paths(
            root, assessment.get("code_evidence"), f"{row_id}.assessment.code_evidence"
        ) if assessment.get("code_evidence") else _text_list(
            assessment.get("code_evidence"),
            f"{row_id}.assessment.code_evidence",
            allow_empty=True,
        )
        _validate_paths(
            root, assessment.get("test_evidence"), f"{row_id}.assessment.test_evidence"
        ) if assessment.get("test_evidence") else _text_list(
            assessment.get("test_evidence"),
            f"{row_id}.assessment.test_evidence",
            allow_empty=True,
        )
        _text(assessment.get("remaining_gap"), f"{row_id}.assessment.remaining_gap")
        if status == "COMPLETE" and int(str(assessment["evidence_level"])[1:]) < 4:
            _fail(f"complete_without_E4:{row_id}")
        if (
            "E5 또는 E6" in verification["required_evidence_level"]
            and status == "COMPLETE"
            and int(str(assessment["evidence_level"])[1:]) < 5
        ):
            _fail(f"core_complete_without_E5:{row_id}")
    return rows


def _validate_gates_and_scenarios(
    root: Path,
    matrix: dict[str, Any],
    rubric_path: Path,
    criterion_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gates = matrix.get("critical_fail_gates")
    scenarios = matrix.get("end_to_end_scenarios")
    if not isinstance(gates, list) or not isinstance(scenarios, list):
        _fail("gates_and_scenarios_must_be_lists")
    expected_gates = _parse_sections(rubric_path, r"## (CF-[0-9]{2})\.\s*(.+)")
    expected_scenarios = _parse_sections(rubric_path, r"## (T-[0-9]{2})\.\s*(.+)")
    if len(gates) != 8 or len(expected_gates) != 8:
        _fail("critical_gate_count_must_be_8")
    if len(scenarios) != 5 or len(expected_scenarios) != 5:
        _fail("scenario_count_must_be_5")
    if [row.get("id") for row in gates if isinstance(row, dict)] != [
        f"CF-{number:02d}" for number in range(1, 9)
    ]:
        _fail("critical_gate_ids_or_order_mismatch")
    if [row.get("id") for row in scenarios if isinstance(row, dict)] != [
        f"T-{number:02d}" for number in range(1, 6)
    ]:
        _fail("scenario_ids_or_order_mismatch")
    for row, expected in zip(gates, expected_gates, strict=True):
        if not isinstance(row, dict):
            _fail("critical_gate_row_must_be_object")
        gate_id, title = expected["groups"]
        _validate_source_binding(row, expected, gate_id, title)
        _validate_common_record(root, row, REQUIRED_GATE_FIELDS, criterion_ids)
        _text(row.get("trigger_policy"), f"{gate_id}.trigger_policy")
        status = row["conservative_initial_assessment"].get("status")
        if status not in {"PASS", "TRIGGERED", "UNKNOWN"}:
            _fail(f"invalid_gate_status:{gate_id}")
    for row, expected in zip(scenarios, expected_scenarios, strict=True):
        if not isinstance(row, dict):
            _fail("scenario_row_must_be_object")
        scenario_id, title = expected["groups"]
        _validate_source_binding(row, expected, scenario_id, title)
        _validate_common_record(root, row, REQUIRED_SCENARIO_FIELDS, criterion_ids)
        _text(row.get("ideal_state"), f"{scenario_id}.ideal_state")
        if row.get("status_scale") != "#/scoring_policy/status_scale":
            _fail(f"scenario_status_scale_reference_changed:{scenario_id}")
        assessment = row["conservative_initial_assessment"]
        status = assessment.get("status")
        if (
            status not in EXPECTED_STATUS_SCORES
            or assessment.get("score") != EXPECTED_STATUS_SCORES[status]
        ):
            _fail(f"scenario_status_score_mismatch:{scenario_id}")
        if row["verification"].get("required_evidence_level") != "E5 또는 E6":
            _fail(f"scenario_must_require_E5_or_E6:{scenario_id}")
    return gates, scenarios


def _validate_policy_and_summary(
    matrix: dict[str, Any],
    criteria: list[dict[str, Any]],
    gates: list[dict[str, Any]],
) -> dict[str, Any]:
    if matrix.get("schema_version") != 1:
        _fail("schema_version_must_be_1")
    if set(matrix) != EXPECTED_TOP_LEVEL_FIELDS:
        _fail("top_level_fields_mismatch")
    counts = matrix.get("counts")
    if counts != {
        "areas": 14,
        "atomic_criteria": 140,
        "critical_fail_gates": 8,
        "end_to_end_scenarios": 5,
    }:
        _fail("declared_counts_mismatch")
    policy = matrix.get("scoring_policy")
    if not isinstance(policy, dict):
        _fail("scoring_policy_must_be_object")
    scale = policy.get("status_scale")
    if not isinstance(scale, list) or [
        item.get("status") for item in scale if isinstance(item, dict)
    ] != list(EXPECTED_STATUS_SCORES):
        _fail("status_scale_order_or_values_mismatch")
    if {item["status"]: item.get("score") for item in scale} != EXPECTED_STATUS_SCORES:
        _fail("status_scale_scores_mismatch")
    evidence_scale = policy.get("evidence_scale")
    if (
        not isinstance(evidence_scale, dict)
        or set(evidence_scale) != EXPECTED_EVIDENCE_LEVELS
    ):
        _fail("evidence_scale_mismatch")
    weights = policy.get("area_weights")
    if not isinstance(weights, dict) or set(weights) != set(EXPECTED_WEIGHTS):
        _fail("area_weights_mismatch")
    if {
        area: item.get("weight") for area, item in weights.items()
    } != EXPECTED_WEIGHTS or sum(EXPECTED_WEIGHTS.values()) != 100:
        _fail("area_weight_values_mismatch")
    if policy.get("unknown_is_passing") is not False:
        _fail("unknown_must_not_pass")

    summary = matrix.get("initial_assessment_summary")
    if not isinstance(summary, dict):
        _fail("initial_assessment_summary_must_be_object")
    status_counts = Counter(
        row["conservative_initial_assessment"]["status"] for row in criteria
    )
    if summary.get("status_counts") != dict(sorted(status_counts.items())):
        _fail("initial_status_counts_mismatch")
    area_scores: dict[str, dict[str, Any]] = {}
    for area, count in EXPECTED_AREA_COUNTS.items():
        rows = [row for row in criteria if row["area"] == area]
        if len(rows) != count:
            _fail(f"area_criterion_count_mismatch:{area}")
        earned = sum(row["conservative_initial_assessment"]["score"] for row in rows)
        possible = count * 4
        area_scores[area] = {
            "name": weights[area]["name"],
            "weight": EXPECTED_WEIGHTS[area],
            "criterion_count": count,
            "earned_atomic_points": earned,
            "possible_atomic_points": possible,
            "score_rate": round(earned / possible, 6),
            "weighted_points": round(earned / possible * EXPECTED_WEIGHTS[area], 6),
        }
    if summary.get("area_scores") != area_scores:
        _fail("initial_area_scores_mismatch")
    weighted = round(sum(item["weighted_points"] for item in area_scores.values()), 6)
    if summary.get("weighted_score_out_of_100") != weighted:
        _fail("initial_weighted_score_mismatch")
    triggered = [
        row["id"]
        for row in gates
        if row["conservative_initial_assessment"]["status"] == "TRIGGERED"
    ]
    if summary.get("triggered_critical_fail_gates") != triggered:
        _fail("triggered_gate_summary_mismatch")
    verdict = summary.get("verdict")
    if triggered and verdict != "CRITICAL FAIL — 완전 충족 아님":
        _fail("triggered_gate_must_force_critical_fail")
    return {
        "status_counts": dict(sorted(status_counts.items())),
        "weighted_score_out_of_100": weighted,
        "triggered_critical_fail_gates": triggered,
        "verdict": verdict,
    }


def validate_matrix(
    root: Path = PROJECT_ROOT, matrix_path: Path | None = None
) -> dict[str, Any]:
    root = root.resolve()
    path = (
        matrix_path or root / "docs/multi-asset-investment-research-audit-matrix.json"
    ).resolve()
    matrix = _load_json(path)
    hashes = _validate_frozen_sources(root, matrix)
    rubric_path = root / RUBRIC_COPY
    criteria = _validate_criteria(root, matrix, rubric_path)
    gates, scenarios = _validate_gates_and_scenarios(
        root, matrix, rubric_path, set(_expected_ids())
    )
    summary = _validate_policy_and_summary(matrix, criteria, gates)
    return {
        "valid": True,
        "matrix": str(path),
        "counts": {
            "areas": 14,
            "atomic_criteria": len(criteria),
            "critical_fail_gates": len(gates),
            "end_to_end_scenarios": len(scenarios),
        },
        "source_hashes": hashes,
        "initial_assessment": summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--matrix", type=Path)
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable validation result"
    )
    args = parser.parse_args(argv)
    try:
        result = validate_matrix(args.root, args.matrix)
    except MatrixValidationError as exc:
        failure = {"valid": False, "error": str(exc)}
        if args.json:
            print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        else:
            print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        counts = result["counts"]
        print(
            "VALID: "
            f"{counts['atomic_criteria']} criteria, "
            f"{counts['critical_fail_gates']} CF gates, "
            f"{counts['end_to_end_scenarios']} scenarios; "
            f"initial verdict={result['initial_assessment']['verdict']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
