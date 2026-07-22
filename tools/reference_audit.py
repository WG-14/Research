#!/usr/bin/env python3
"""Fail-closed evaluator for the user-supplied A--J research-platform rubric.

The repository also retains older product-scope matrices as historical evidence.
This evaluator is intentionally bound to the July 2026 completeness rubric named
in ``REFERENCE_RUBRIC_SHA256``.  It never promotes documentation, a declared
score, or a missing external attestation into implementation evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

try:
    from tools.reference_audit_surface import (
        AUDIT_SURFACE_SCHEMA_VERSION,
        audit_surface,
    )
    from tools.update_reference_audit import build_matrix
except ModuleNotFoundError:  # direct ``python tools/...`` execution
    from reference_audit_surface import (  # type: ignore[import-not-found,no-redef]
        AUDIT_SURFACE_SCHEMA_VERSION,
        audit_surface,
    )
    from update_reference_audit import build_matrix  # type: ignore[import-not-found,no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = PROJECT_ROOT / "docs" / "investment-research-platform-audit.json"
REFERENCE_RUBRIC_SHA256 = (
    "f7ec62425039c335c22ce39ff94de0b3c113ec162620b8ff10bef9902f3c14ae"
)
REFERENCE_INSTRUCTION_SHA256 = (
    "26871e2de2deb4a86b8bee87bdbb30b731eb19e82e61ee0a64bbf0c2cebfc8de"
)
RUBRIC_COPY_SHA256 = "28cd21646427b5205423eb0deb6df05aed752321e1be455b5ce77fe72eba8787"
INSTRUCTION_COPY_SHA256 = (
    "2e6d7b9719ab685af60743240278d1fcba82409fc51396673dedb4ea56a328bc"
)
EXPECTED_CRITERIA = 184
EXPECTED_FATAL_GATES = 12
DOMAIN_POINTS = {
    "A": 5.0,
    "B": 15.0,
    "C": 15.0,
    "D": 10.0,
    "E": 15.0,
    "F": 15.0,
    "G": 10.0,
    "H": 10.0,
    "I": 5.0,
    "J": 5.0,
}
IMPORTANCE_WEIGHTS = {"C": 3, "M": 2, "S": 1}
MATURITY_MULTIPLIERS = {
    "M0": 0.0,
    "M1": 0.10,
    "M2": 0.40,
    "M3": 0.65,
    "M4": 0.85,
    "M5": 1.0,
}
ALLOWED_STATUSES = {
    "VERIFIED",
    "IMPLEMENTED_NOT_VERIFIED",
    "PARTIAL",
    "DOCUMENTATION_ONLY",
    "PLACEHOLDER",
    "MISSING",
    "OUT_OF_SCOPE_VIOLATION",
    "UNVERIFIED_EXTERNAL",
}
_CRITERION_ID = re.compile(r"^[A-J]-[0-9]{2}$")
_FATAL_GATE_ID = re.compile(r"^FG-[0-9]{2}$")
_REQUIRED_CRITERION_FIELDS = {
    "id",
    "domain",
    "importance",
    "title",
    "exact_meaning",
    "rubric_text",
    "ideal_state",
    "inspection_targets",
    "objective_evidence",
    "status_scale",
    "dependencies",
    "verification_method",
    "completion_condition",
    "maturity",
    "status",
    "gap",
    "required_remediation",
    "assessment_history",
}
_EXPECTED_MATRIX_FIELDS = {
    "schema_version",
    "canonical_source",
    "scoring",
    "assessment",
    "fatal_gates",
    "criteria",
}
_EXPECTED_ASSESSMENT_FIELDS = {
    "iteration",
    "assessed_at",
    "repository_commit",
    "repository_branch",
    "worktree_was_clean",
    "diagnosis",
    "score_cap",
    "score_cap_reason",
    "assessment_surface",
}
_REQUIRED_HISTORY_FIELDS = {
    "iteration",
    "assessed_at",
    "commit",
    "phase",
    "maturity",
    "status",
    "diagnosis",
}
_ALLOWED_HISTORY_FIELDS = _REQUIRED_HISTORY_FIELDS | {"worktree_patch"}
_REQUIRED_FATAL_GATE_FIELDS = {
    "id",
    "title",
    "status",
    "evidence",
    "verification_method",
    "mitigation_possible",
    "impact",
    "required_remediation",
}
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_COMMAND_PREFIX = (
    "PYTHONHASHSEED=0",
    "OMP_NUM_THREADS=1",
    "OPENBLAS_NUM_THREADS=1",
    "MKL_NUM_THREADS=1",
    "NUMEXPR_NUM_THREADS=1",
    "BLIS_NUM_THREADS=1",
    "VECLIB_MAXIMUM_THREADS=1",
    "DJANGO_SETTINGS_MODULE=market_research_web.settings_test",
    "PYTHONPATH=src:apps/internal_web/src:services/research_operations/src",
    "uv",
    "run",
    "--no-sync",
    "pytest",
    "-q",
)


class DuplicateKeyError(ValueError):
    """Raised when JSON could otherwise be interpreted ambiguously."""


@dataclass(frozen=True, slots=True)
class AuditEvaluation:
    score: float
    raw_score: float
    score_cap: float
    domain_scores: dict[str, float]
    fatal_failures: tuple[str, ...]
    fatal_unverified: tuple[str, ...]
    critical_m4_or_higher: int
    critical_count: int
    maturity_counts: dict[str, int]
    status_counts: dict[str, int]
    findings: tuple[str, ...]
    complete: bool
    verdict: str


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"nonfinite_json_constant:{value}")


def load_matrix(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_nonfinite_json_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("audit_matrix_root_must_be_object")
    return value


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _nonempty_text_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_nonempty_text(item) for item in value)
    )


def _is_iso_date(value: object) -> bool:
    if not isinstance(value, str) or value != value.strip():
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _pytest_target(command: object) -> str | None:
    if not isinstance(command, str):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) != len(_EVIDENCE_COMMAND_PREFIX) + 1:
        return None
    if tuple(tokens[:-1]) != _EVIDENCE_COMMAND_PREFIX:
        return None
    return tokens[-1]


def _owned_path(root: Path, value: object) -> Path | None:
    if not _nonempty_text(value):
        return None
    relative = Path(str(value))
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _evidence_is_structured(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    required = {
        "path",
        "path_sha256",
        "symbol_or_lines",
        "test",
        "test_sha256",
        "command",
        "result",
    }
    return all(
        isinstance(item, dict)
        and set(item) == required
        and _nonempty_text(item["path"])
        and re.fullmatch(r"[0-9a-f]{64}", str(item["path_sha256"])) is not None
        and _nonempty_text(item["symbol_or_lines"])
        and _nonempty_text(item["test"])
        and re.fullmatch(r"[0-9a-f]{64}", str(item["test_sha256"])) is not None
        and _nonempty_text(item["command"])
        and _nonempty_text(item["result"])
        for item in value
    )


def _maturity_rank(value: str) -> int:
    return int(value.removeprefix("M"))


def _status_matches_maturity(*, status: str, maturity: str) -> bool:
    rank = _maturity_rank(maturity)
    if status == "VERIFIED":
        return rank >= 4
    if status == "IMPLEMENTED_NOT_VERIFIED":
        return rank == 3
    if status == "PARTIAL":
        return rank == 2
    if status in {"DOCUMENTATION_ONLY", "PLACEHOLDER"}:
        return rank == 1
    if status in {"MISSING", "OUT_OF_SCOPE_VIOLATION"}:
        return rank == 0
    if status == "UNVERIFIED_EXTERNAL":
        return rank <= 2
    return False


def _git_provenance(root: Path) -> tuple[Path, str, str, bool]:
    """Return the exact Git identity and worktree state used by the audit."""

    def run(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    top_level = Path(run("rev-parse", "--show-toplevel")).resolve()
    head = run("rev-parse", "--verify", "HEAD")
    branch = run("branch", "--show-current")
    dirty = bool(run("status", "--porcelain=v1", "--untracked-files=all"))
    return top_level, head, branch, dirty


def evaluate_matrix(path: Path = DEFAULT_MATRIX) -> AuditEvaluation:
    matrix = load_matrix(path)
    findings: list[str] = []
    if set(matrix) != _EXPECTED_MATRIX_FIELDS:
        findings.append("matrix_fields_invalid")
    if matrix.get("schema_version") != 1:
        findings.append("matrix_schema_version_invalid")
    canonical_generator_match = False
    if path.resolve() == DEFAULT_MATRIX.resolve():
        try:
            canonical_generator_match = matrix == build_matrix()
        except (OSError, UnicodeError, ValueError):
            findings.append("canonical_matrix_generator_evaluation_failed")
        if not canonical_generator_match:
            findings.append("canonical_matrix_generator_mismatch")
    source = matrix.get("canonical_source")
    if not isinstance(source, dict):
        findings.append("canonical_source_missing")
        source = {}
    expected_source = {
        "title": "Codex용 투자 연구 전용 플랫폼 레포지토리 완전성 감사 프롬프트",
        "sha256": REFERENCE_RUBRIC_SHA256,
        "instruction_sha256": REFERENCE_INSTRUCTION_SHA256,
        "criterion_count": EXPECTED_CRITERIA,
        "fatal_gate_count": EXPECTED_FATAL_GATES,
        "domain_count": len(DOMAIN_POINTS),
        "repository_copy": {
            "rubric_path": "docs/investment-research-platform-audit-rubric.md",
            "rubric_normalized_sha256": RUBRIC_COPY_SHA256,
            "instruction_path": "docs/investment-research-platform-audit-instructions.md",
            "instruction_normalized_sha256": INSTRUCTION_COPY_SHA256,
        },
    }
    if set(source) != set(expected_source):
        findings.append("canonical_source_fields_invalid")
    for key, expected in expected_source.items():
        if source.get(key) != expected:
            findings.append(f"canonical_source_{key}_mismatch")

    expected_scoring = {
        "maturity_multipliers": MATURITY_MULTIPLIERS,
        "importance_weights": IMPORTANCE_WEIGHTS,
        "domain_points": DOMAIN_POINTS,
        "completion_policy": "score>=95, no failed/unverified fatal gate, all Critical M4+, every criterion VERIFIED; evidence is never inferred from narrative score",
    }
    scoring = matrix.get("scoring")
    if not isinstance(scoring, dict):
        findings.append("scoring_missing")
    elif scoring != expected_scoring:
        findings.append("scoring_contract_mismatch")

    matrix_root = path.resolve().parent.parent
    for label, relative, expected_hash in (
        (
            "rubric",
            "docs/investment-research-platform-audit-rubric.md",
            RUBRIC_COPY_SHA256,
        ),
        (
            "instruction",
            "docs/investment-research-platform-audit-instructions.md",
            INSTRUCTION_COPY_SHA256,
        ),
    ):
        source_path = matrix_root / relative
        try:
            observed_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except OSError:
            findings.append(f"canonical_source_{label}_copy_missing")
        else:
            if observed_hash != expected_hash:
                findings.append(f"canonical_source_{label}_copy_hash_mismatch")

    criteria = matrix.get("criteria")
    if not isinstance(criteria, list):
        findings.append("criteria_missing")
        criteria = []
    gates = matrix.get("fatal_gates")
    if not isinstance(gates, list):
        findings.append("fatal_gates_missing")
        gates = []
    assessment = matrix.get("assessment")
    if not isinstance(assessment, dict):
        findings.append("assessment_missing")
        assessment = {}
    elif set(assessment) != _EXPECTED_ASSESSMENT_FIELDS:
        findings.append("assessment_fields_invalid")
    assessment_iteration = assessment.get("iteration")
    if (
        not isinstance(assessment_iteration, int)
        or isinstance(assessment_iteration, bool)
        or assessment_iteration < 1
        or assessment_iteration > 10
    ):
        findings.append("assessment_iteration_invalid")
        assessment_iteration = 0
    if not _is_iso_date(assessment.get("assessed_at")):
        findings.append("assessment_date_invalid")
    if (
        not isinstance(assessment.get("repository_commit"), str)
        or _SHA1.fullmatch(str(assessment.get("repository_commit"))) is None
    ):
        findings.append("assessment_repository_commit_invalid")
    if not _nonempty_text(assessment.get("repository_branch")):
        findings.append("assessment_repository_branch_invalid")
    if not isinstance(assessment.get("worktree_was_clean"), bool):
        findings.append("assessment_worktree_state_invalid")
    if not _nonempty_text(assessment.get("diagnosis")):
        findings.append("assessment_diagnosis_invalid")
    score_cap = assessment.get("score_cap")
    if not isinstance(score_cap, (int, float)) or isinstance(score_cap, bool):
        findings.append("assessment_score_cap_invalid")
        score_cap = 0.0
    elif float(score_cap) not in {25.0, 55.0, 75.0, 84.0, 100.0}:
        findings.append("assessment_score_cap_invalid")
        score_cap = 0.0
    if not _nonempty_text(assessment.get("score_cap_reason")):
        findings.append("assessment_score_cap_reason_invalid")
    declared_surface = assessment.get("assessment_surface")
    if not isinstance(declared_surface, dict) or set(declared_surface) != {
        "schema_version",
        "file_count",
        "sha256",
        "exclusions",
    }:
        findings.append("assessment_surface_schema_invalid")
    else:
        if declared_surface.get("schema_version") != AUDIT_SURFACE_SCHEMA_VERSION:
            findings.append("assessment_surface_schema_version_invalid")
        file_count = declared_surface.get("file_count")
        if (
            not isinstance(file_count, int)
            or isinstance(file_count, bool)
            or file_count < 1
        ):
            findings.append("assessment_surface_file_count_invalid")
        if (
            not isinstance(declared_surface.get("sha256"), str)
            or _SHA256.fullmatch(str(declared_surface.get("sha256"))) is None
        ):
            findings.append("assessment_surface_digest_invalid")
        if not _nonempty_text_list(declared_surface.get("exclusions")):
            findings.append("assessment_surface_exclusions_invalid")
    expected_surface = audit_surface(matrix_root)
    if declared_surface != expected_surface:
        findings.append("assessment_surface_hash_mismatch")
    if path.resolve() == DEFAULT_MATRIX.resolve():
        try:
            git_root, head, branch, dirty = _git_provenance(matrix_root)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            findings.append("assessment_git_provenance_unavailable")
        else:
            if git_root != matrix_root.resolve():
                findings.append("assessment_git_root_mismatch")
            if assessment.get("repository_commit") != head:
                findings.append("assessment_repository_commit_mismatch")
            if not branch:
                findings.append("assessment_repository_detached_head")
            if assessment.get("repository_branch") != branch:
                findings.append("assessment_repository_branch_mismatch")
            if assessment.get("worktree_was_clean") != (not dirty):
                findings.append("assessment_worktree_state_mismatch")

    criterion_ids = [item.get("id") for item in criteria if isinstance(item, dict)]
    if len(criteria) != EXPECTED_CRITERIA:
        findings.append(f"criterion_count:{len(criteria)}")
    if len(set(criterion_ids)) != len(criterion_ids):
        findings.append("criterion_ids_duplicate")
    expected_ids = {
        f"{domain}-{number:02d}"
        for domain, count in {
            "A": 8,
            "B": 22,
            "C": 20,
            "D": 17,
            "E": 26,
            "F": 25,
            "G": 16,
            "H": 21,
            "I": 14,
            "J": 15,
        }.items()
        for number in range(1, count + 1)
    }
    if set(criterion_ids) != expected_ids:
        findings.append("criterion_id_set_mismatch")

    numerators = {domain: 0.0 for domain in DOMAIN_POINTS}
    denominators = {domain: 0 for domain in DOMAIN_POINTS}
    maturity_counts = {key: 0 for key in MATURITY_MULTIPLIERS}
    status_counts = {key: 0 for key in sorted(ALLOWED_STATUSES)}
    critical_count = 0
    critical_m4_or_higher = 0
    history_phase_sequence: tuple[str, ...] | None = None
    for index, raw in enumerate(criteria):
        if not isinstance(raw, dict):
            findings.append(f"criterion_{index}_not_object")
            continue
        criterion_id = str(raw.get("id") or f"index-{index}")
        missing = sorted(_REQUIRED_CRITERION_FIELDS - set(raw))
        if missing:
            findings.append(f"{criterion_id}:fields_missing:{','.join(missing)}")
        if _CRITERION_ID.fullmatch(criterion_id) is None:
            findings.append(f"{criterion_id}:id_invalid")
        domain = raw.get("domain")
        if domain != criterion_id[:1] or domain not in DOMAIN_POINTS:
            findings.append(f"{criterion_id}:domain_invalid")
            continue
        importance = raw.get("importance")
        maturity = raw.get("maturity")
        status = raw.get("status")
        if importance not in IMPORTANCE_WEIGHTS:
            findings.append(f"{criterion_id}:importance_invalid")
            continue
        if maturity not in MATURITY_MULTIPLIERS:
            findings.append(f"{criterion_id}:maturity_invalid")
            continue
        if status not in ALLOWED_STATUSES:
            findings.append(f"{criterion_id}:status_invalid")
        elif not _status_matches_maturity(status=str(status), maturity=str(maturity)):
            findings.append(f"{criterion_id}:status_maturity_incoherent")
        if not all(
            _nonempty_text(raw.get(field))
            for field in (
                "title",
                "exact_meaning",
                "rubric_text",
                "ideal_state",
                "verification_method",
                "completion_condition",
                "gap",
                "required_remediation",
            )
        ):
            findings.append(f"{criterion_id}:narrative_fields_invalid")
        rubric_text = raw.get("rubric_text")
        rubric_heading = f"## {criterion_id} [{importance}] {raw.get('title')}"
        if not isinstance(rubric_text, str) or not (
            rubric_text == rubric_heading
            or rubric_text.startswith(f"{rubric_heading}\n")
        ):
            findings.append(f"{criterion_id}:rubric_text_binding_invalid")
        if not _nonempty_text_list(raw.get("inspection_targets")):
            findings.append(f"{criterion_id}:inspection_targets_invalid")
        if not _nonempty_text_list(raw.get("dependencies")):
            findings.append(f"{criterion_id}:dependencies_invalid")
        if not isinstance(raw.get("status_scale"), dict):
            findings.append(f"{criterion_id}:status_scale_invalid")
        history = raw.get("assessment_history")
        if not isinstance(history, list):
            findings.append(f"{criterion_id}:assessment_history_invalid")
        else:
            if len(history) != assessment_iteration:
                findings.append(f"{criterion_id}:assessment_history_length_invalid")
            observed_iterations: list[int] = []
            observed_phases: list[str] = []
            observed_dates: list[date] = []
            for history_index, entry in enumerate(history, start=1):
                if not isinstance(entry, dict):
                    findings.append(
                        f"{criterion_id}:assessment_history_{history_index}_not_object"
                    )
                    continue
                if not _REQUIRED_HISTORY_FIELDS.issubset(entry) or not set(
                    entry
                ).issubset(_ALLOWED_HISTORY_FIELDS):
                    findings.append(
                        f"{criterion_id}:assessment_history_{history_index}_fields_invalid"
                    )
                iteration = entry.get("iteration")
                if (
                    not isinstance(iteration, int)
                    or isinstance(iteration, bool)
                    or iteration < 1
                ):
                    findings.append(
                        f"{criterion_id}:assessment_history_{history_index}_iteration_invalid"
                    )
                else:
                    observed_iterations.append(iteration)
                history_date = entry.get("assessed_at")
                if not _is_iso_date(history_date):
                    findings.append(
                        f"{criterion_id}:assessment_history_{history_index}_date_invalid"
                    )
                else:
                    assert isinstance(history_date, str)
                    observed_dates.append(date.fromisoformat(history_date))
                if (
                    not isinstance(entry.get("commit"), str)
                    or _SHA1.fullmatch(str(entry.get("commit"))) is None
                ):
                    findings.append(
                        f"{criterion_id}:assessment_history_{history_index}_commit_invalid"
                    )
                phase = entry.get("phase")
                if not _nonempty_text(phase):
                    findings.append(
                        f"{criterion_id}:assessment_history_{history_index}_phase_invalid"
                    )
                else:
                    observed_phases.append(str(phase))
                history_maturity = entry.get("maturity")
                history_status = entry.get("status")
                if history_maturity not in MATURITY_MULTIPLIERS:
                    findings.append(
                        f"{criterion_id}:assessment_history_{history_index}_maturity_invalid"
                    )
                if history_status not in ALLOWED_STATUSES:
                    findings.append(
                        f"{criterion_id}:assessment_history_{history_index}_status_invalid"
                    )
                elif (
                    history_maturity in MATURITY_MULTIPLIERS
                    and not _status_matches_maturity(
                        status=str(history_status), maturity=str(history_maturity)
                    )
                ):
                    findings.append(
                        f"{criterion_id}:assessment_history_{history_index}_status_maturity_incoherent"
                    )
                if not _nonempty_text(entry.get("diagnosis")):
                    findings.append(
                        f"{criterion_id}:assessment_history_{history_index}_diagnosis_invalid"
                    )
                if "worktree_patch" in entry and not _nonempty_text(
                    entry.get("worktree_patch")
                ):
                    findings.append(
                        f"{criterion_id}:assessment_history_{history_index}_worktree_patch_invalid"
                    )
            if observed_iterations != list(range(1, assessment_iteration + 1)):
                findings.append(
                    f"{criterion_id}:assessment_history_iteration_sequence_invalid"
                )
            if len(observed_phases) != len(set(observed_phases)):
                findings.append(f"{criterion_id}:assessment_history_phase_duplicate")
            if observed_dates != sorted(observed_dates):
                findings.append(f"{criterion_id}:assessment_history_date_order_invalid")
            phase_sequence = tuple(observed_phases)
            if history_phase_sequence is None:
                history_phase_sequence = phase_sequence
            elif phase_sequence != history_phase_sequence:
                findings.append(
                    f"{criterion_id}:assessment_history_phase_sequence_mismatch"
                )
            if history and isinstance(history[-1], dict):
                final_history = history[-1]
                expected_final = {
                    "iteration": assessment_iteration,
                    "assessed_at": assessment.get("assessed_at"),
                    "commit": assessment.get("repository_commit"),
                    "maturity": maturity,
                    "status": status,
                    "diagnosis": raw.get("gap"),
                }
                for field, expected in expected_final.items():
                    if final_history.get(field) != expected:
                        findings.append(
                            f"{criterion_id}:assessment_history_final_{field}_mismatch"
                        )
                if assessment.get("worktree_was_clean") is False and not _nonempty_text(
                    final_history.get("worktree_patch")
                ):
                    findings.append(
                        f"{criterion_id}:assessment_history_final_worktree_patch_missing"
                    )
        evidence = raw.get("objective_evidence")
        if not _evidence_is_structured(evidence):
            findings.append(f"{criterion_id}:objective_evidence_invalid")
        else:
            assert isinstance(evidence, list)
            for item in evidence:
                evidence_path = _owned_path(matrix_root, item["path"])
                if evidence_path is None:
                    findings.append(f"{criterion_id}:evidence_path_outside_root")
                elif not evidence_path.is_file():
                    findings.append(
                        f"{criterion_id}:evidence_path_missing:{item['path']}"
                    )
                elif hashlib.sha256(evidence_path.read_bytes()).hexdigest() != item.get(
                    "path_sha256"
                ):
                    findings.append(f"{criterion_id}:evidence_path_hash_mismatch")
                test_path = _owned_path(matrix_root, item["test"])
                if test_path is None:
                    findings.append(f"{criterion_id}:evidence_test_outside_root")
                elif not test_path.is_file():
                    findings.append(
                        f"{criterion_id}:evidence_test_missing:{item['test']}"
                    )
                elif hashlib.sha256(test_path.read_bytes()).hexdigest() != item.get(
                    "test_sha256"
                ):
                    findings.append(f"{criterion_id}:evidence_test_hash_mismatch")
                if _pytest_target(item["command"]) != item["test"]:
                    findings.append(f"{criterion_id}:evidence_command_binding_mismatch")
        weight = IMPORTANCE_WEIGHTS[str(importance)]
        numerators[domain] += weight * MATURITY_MULTIPLIERS[str(maturity)]
        denominators[domain] += weight
        maturity_counts[str(maturity)] += 1
        if status in status_counts:
            status_counts[str(status)] += 1
        if importance == "C":
            critical_count += 1
            if _maturity_rank(str(maturity)) >= 4:
                critical_m4_or_higher += 1

    gate_ids: list[str] = []
    fatal_failures: list[str] = []
    fatal_unverified: list[str] = []
    for index, raw in enumerate(gates):
        if not isinstance(raw, dict):
            findings.append(f"fatal_gate_{index}_not_object")
            continue
        gate_id = str(raw.get("id") or f"index-{index}")
        gate_ids.append(gate_id)
        if set(raw) != _REQUIRED_FATAL_GATE_FIELDS:
            findings.append(f"{gate_id}:fields_invalid")
        if _FATAL_GATE_ID.fullmatch(gate_id) is None:
            findings.append(f"{gate_id}:id_invalid")
        if raw.get("status") not in {"PASS", "FAIL", "UNVERIFIED"}:
            findings.append(f"{gate_id}:status_invalid")
        for field in (
            "title",
            "evidence",
            "verification_method",
            "impact",
            "required_remediation",
        ):
            if not _nonempty_text(raw.get(field)):
                findings.append(f"{gate_id}:{field}_invalid")
        if not isinstance(raw.get("mitigation_possible"), bool):
            findings.append(f"{gate_id}:mitigation_possible_invalid")
        gate_test = _pytest_target(raw.get("verification_method"))
        if gate_test is None:
            findings.append(f"{gate_id}:verification_method_binding_invalid")
        else:
            gate_test_path = _owned_path(matrix_root, gate_test)
            if gate_test_path is None:
                findings.append(f"{gate_id}:verification_test_outside_root")
            elif not gate_test_path.is_file():
                findings.append(f"{gate_id}:verification_test_missing:{gate_test}")
        if raw.get("status") == "FAIL":
            fatal_failures.append(gate_id)
        elif raw.get("status") == "UNVERIFIED":
            fatal_unverified.append(gate_id)
    if len(gates) != EXPECTED_FATAL_GATES or len(set(gate_ids)) != len(gate_ids):
        findings.append("fatal_gate_count_or_uniqueness_invalid")
    if set(gate_ids) != {f"FG-{number:02d}" for number in range(1, 13)}:
        findings.append("fatal_gate_id_set_mismatch")
    if "FG-06" in fatal_failures:
        if float(score_cap) > 84.0:
            findings.append("fg06_score_cap_invalid")
        if "FG-06" not in str(assessment.get("score_cap_reason", "")):
            findings.append("fg06_score_cap_reason_mismatch")
        if maturity_counts["M5"]:
            findings.append("fg06_m5_award_invalid")

    domain_scores = {
        domain: (
            DOMAIN_POINTS[domain] * numerators[domain] / denominators[domain]
            if denominators[domain]
            else 0.0
        )
        for domain in DOMAIN_POINTS
    }
    raw_score = sum(domain_scores.values())
    score = min(raw_score, float(score_cap))
    all_verified = bool(criteria) and all(
        isinstance(item, dict) and item.get("status") == "VERIFIED" for item in criteria
    )
    complete = (
        canonical_generator_match
        and not findings
        and score >= 95.0
        and not fatal_failures
        and not fatal_unverified
        and critical_m4_or_higher == critical_count
        and all_verified
    )
    verdict = _verdict(
        score=score,
        complete=complete,
        fatal_failures=tuple(fatal_failures),
        fatal_unverified=tuple(fatal_unverified),
        findings=tuple(findings),
        critical_m4_or_higher=critical_m4_or_higher,
        critical_count=critical_count,
    )
    return AuditEvaluation(
        score=score,
        raw_score=raw_score,
        score_cap=float(score_cap),
        domain_scores=domain_scores,
        fatal_failures=tuple(fatal_failures),
        fatal_unverified=tuple(fatal_unverified),
        critical_m4_or_higher=critical_m4_or_higher,
        critical_count=critical_count,
        maturity_counts={key: value for key, value in maturity_counts.items() if value},
        status_counts={key: value for key, value in status_counts.items() if value},
        findings=tuple(sorted(set(findings))),
        complete=complete,
        verdict=verdict,
    )


def _verdict(
    *,
    score: float,
    complete: bool,
    fatal_failures: tuple[str, ...],
    fatal_unverified: tuple[str, ...],
    findings: tuple[str, ...],
    critical_m4_or_higher: int,
    critical_count: int,
) -> str:
    """Apply fatal precedence and the rubric's score bands conservatively."""

    # The rubric's strongest classification rule is narrower than its general
    # fatal-gate cap: confirmed future-information leakage (FG-03) or inability
    # to reproduce results (FG-06) makes the repository *not* an investment
    # research platform regardless of its weighted score.  Other fatal gates
    # still block COMPLETE/NEAR_COMPLETE through the predicates below.
    if {"FG-03", "FG-06"}.intersection(fatal_failures):
        return "NOT_AN_INVESTMENT_RESEARCH_PLATFORM"
    critical_coverage = (
        critical_m4_or_higher / critical_count if critical_count else 0.0
    )
    if (
        complete
        and not fatal_failures
        and not fatal_unverified
        and not findings
        and critical_coverage == 1.0
    ):
        return "COMPLETE"
    if (
        90 <= score < 95
        and not fatal_failures
        and not fatal_unverified
        and not findings
        and critical_coverage >= 0.9
    ):
        return "NEAR_COMPLETE"
    if score >= 80:
        return "SUBSTANTIALLY_COMPLETE_BUT_INCOMPLETE"
    if score >= 70:
        return "FUNCTIONAL_RESEARCH_PLATFORM"
    if score >= 50:
        return "RESEARCH_TOOLKIT"
    if score >= 30:
        return "PROTOTYPE"
    return "NOT_AN_INVESTMENT_RESEARCH_PLATFORM"


def _payload(evaluation: AuditEvaluation) -> dict[str, Any]:
    return {
        "verdict": evaluation.verdict,
        "is_complete_against_reference": evaluation.complete,
        "overall_score": round(evaluation.score, 4),
        "raw_weighted_score": round(evaluation.raw_score, 4),
        "score_cap": evaluation.score_cap,
        "domain_scores": {
            domain: {"max": DOMAIN_POINTS[domain], "score": round(score, 4)}
            for domain, score in evaluation.domain_scores.items()
        },
        "fatal_failures": list(evaluation.fatal_failures),
        "fatal_unverified": list(evaluation.fatal_unverified),
        "critical_m4_or_higher": evaluation.critical_m4_or_higher,
        "critical_count": evaluation.critical_count,
        "maturity_counts": evaluation.maturity_counts,
        "status_counts": evaluation.status_counts,
        "findings": list(evaluation.findings),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the canonical 184-criterion research-platform audit."
    )
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--validate-structure",
        action="store_true",
        help="exit successfully when the matrix is structurally valid even if incomplete",
    )
    args = parser.parse_args(argv)
    try:
        evaluation = evaluate_matrix(args.matrix)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"reference audit: INVALID: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(_payload(evaluation), ensure_ascii=False, indent=2))
    else:
        state = "COMPLETE" if evaluation.complete else "INCOMPLETE"
        print(
            "reference audit: "
            f"{state}; verdict={evaluation.verdict}; score={evaluation.score:.2f}/100; "
            f"critical_m4+={evaluation.critical_m4_or_higher}/"
            f"{evaluation.critical_count}; fatal_failures="
            f"{','.join(evaluation.fatal_failures) or 'none'}; "
            f"findings={len(evaluation.findings)}"
        )
        for finding in evaluation.findings:
            print(finding)
    if evaluation.findings:
        return 2
    if args.validate_structure:
        return 0
    return 0 if evaluation.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
