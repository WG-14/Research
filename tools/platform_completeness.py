#!/usr/bin/env python3
"""Evaluate or safely execute the platform-completeness criterion manifest.

This gate deliberately separates a score assertion from completion evidence.
A criterion earns completion credit only when it has score 5, is supported, and
has hash-bound implementation/test paths plus successful command receipts.  The
default mode only evaluates receipts.  The opt-in evidence runner accepts a
small, fixed command grammar and never executes arbitrary manifest commands.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = (
    PROJECT_ROOT / "docs" / "research-platform-full-scope-evaluation-matrix.json"
)
DEFAULT_REPORT = (
    PROJECT_ROOT / "docs" / "research-platform-completeness-status.generated.md"
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_ID_RE = re.compile(r"^[A-Z]+-[0-9]{2}$")
_FULL_SCOPE_ID_RE = re.compile(r"^S[1-7]-[A-Z]+[0-9]{2}$")
_FULL_SCOPE_RUBRIC_SHA256 = (
    "13ab8fbd3c37a3095ca9fd2c69818c4cb7d5f85fdf96f9f27fedb626ba17d635"
)
_FULL_SCOPE_INSTRUCTION_SHA256 = (
    "25ddd87c30dce17b5c22c24096b5d8642375dc58570f8fa2dcbb67ce34a19396"
)
_FULL_SCOPE_CRITERION_COUNT = 431
_FULL_SCOPE_EXPLICIT_COUNT = 268
_FULL_SCOPE_SUPPLEMENTAL_COUNT = 163
_FULL_SCOPE_BLOCKER_COUNT = 19
_FULL_SCOPE_SCOPES = {
    "CORE",
    "SPOT",
    "FUTURES",
    "OPTIONS",
    "DERIVATIVES_PORTFOLIO",
    "DERIVATIVES_RISK",
}
_EVIDENCE_RANK = {"E0": 0, "E1": 1, "E2": 2, "E3": 3, "E4": 4, "E5": 5}
_FORBIDDEN_SELF_EVIDENCE = {
    "docs/research-platform-completeness-review.md",
    "docs/research-platform-evaluation-matrix.json",
    "docs/research-platform-full-scope-evaluation-matrix.json",
    "docs/research-platform-full-scope-review.md",
    "docs/research-platform-completeness-status.generated.md",
}
_FORBIDDEN_COMMAND_TOKENS = {
    "--continue-on-collection-errors",
    "--deselect",
    "--runxfail",
    "--sw",
    "--stepwise",
    "--stepwise-skip",
}
_ALLOWED_PYTEST_FLAGS = {
    "-q",
    "-s",
    "--strict-config",
    "--strict-markers",
}
_ALLOWED_TEST_ROOTS = (
    "tests/",
    "apps/internal_web/tests/",
    "services/research_operations/tests/",
)
_ALLOWED_PLATFORM_SUBCOMMANDS = {
    "audit",
    "compile",
    "docs-check",
    "lint",
    "test-all",
    "test-browser",
    "test-core",
    "test-integration",
    "test-operations",
    "test-web",
    "typecheck",
    "verify-deployment",
}
_DANGEROUS_ENVIRONMENT_KEYS = {
    "BASH_ENV",
    "CDPATH",
    "ENV",
    "PYTHONBREAKPOINT",
    "PYTHONINSPECT",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONWARNINGS",
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
}
_SECRET_ENVIRONMENT_MARKERS = (
    "ACCESS_KEY",
    "API_KEY",
    "AUTH",
    "CONNECTION_STRING",
    "COOKIE",
    "CREDENTIAL",
    "DATABASE_URL",
    "DSN",
    "PASSWORD",
    "PASSWD",
    "PRIVATE",
    "SECRET",
    "SESSION",
    "SIGNATURE",
    "TOKEN",
)
_DETERMINISTIC_ENVIRONMENT = {
    "BLIS_NUM_THREADS": "1",
    "DJANGO_SETTINGS_MODULE": "market_research_web.settings_test",
    "INTERNAL_WEB_SECRET_KEY": "test-only-not-for-production-0123456789abcdef",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}
_EXTERNAL_ATTESTATION_SCOPE = "site_or_organization"
_REPOSITORY_TRACKED_DELETION_SENTINEL = b"platform-completeness:tracked-file-deleted:v1"
_PYTEST_PASS_RE = re.compile(r"\b[1-9][0-9]*\s+passed\b", re.IGNORECASE)
_PYTEST_ZERO_RE = re.compile(
    r"\b(?:no tests ran|collected\s+0\s+items?)\b", re.IGNORECASE
)


class DuplicateKeyError(ValueError):
    """Raised for ambiguous JSON objects."""


@dataclass(frozen=True, slots=True, order=True)
class Finding:
    subject: str
    code: str
    detail: str

    def render(self) -> str:
        return f"{self.subject}: {self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class CriterionResult:
    criterion_id: str
    area_id: str
    score: int | None
    evidence_level: str
    complete: bool
    finding_codes: tuple[str, ...]
    required_evidence_level: str = "missing"
    evidence_paths: tuple[tuple[str, str | None], ...] = ()
    verification_commands: tuple[tuple[str, tuple[str, ...]], ...] = ()
    receipt_bindings: tuple[tuple[str, str, str | None], ...] = ()
    local_findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True)
class Evaluation:
    manifest_sha256: str
    rubric_sha256: str
    expected_criteria: int
    declared_score: float
    verified_criteria: int
    criteria: tuple[CriterionResult, ...]
    findings: tuple[Finding, ...]

    @property
    def complete(self) -> bool:
        return (
            not self.findings
            and self.verified_criteria == self.expected_criteria
            and self.declared_score == 100.0
        )


class EvidenceRunError(ValueError):
    """Raised before execution when an evidence run is unsafe or ambiguous."""


@dataclass(frozen=True, slots=True)
class RepositoryProvenance:
    commit: str
    dirty_diff_sha256: str


@dataclass(frozen=True, slots=True)
class EvidenceCommand:
    subject: str
    command_id: str
    argv: tuple[str, ...]
    minimum_level: str
    receipt_path: str
    path_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class CommandExecution:
    group_id: str
    argv: tuple[str, ...]
    started_at: str
    finished_at: str
    duration_seconds: float
    exit_code: int
    timed_out: bool
    evidence_eligible: bool
    disqualifying_outcomes: tuple[str, ...]
    stdout_path: str
    stdout_sha256: str
    stderr_path: str
    stderr_sha256: str


@dataclass(frozen=True, slots=True)
class EvidenceRun:
    evidence_root: Path
    resolved_manifest: Path
    ledger_json: Path
    ledger_markdown: Path
    command_count: int
    execution_count: int
    evaluation: Evaluation
    runner_findings: tuple[Finding, ...]

    @property
    def complete(self) -> bool:
        return self.evaluation.complete and not self.runner_findings


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def criterion_ids_sha256(values: list[str]) -> str:
    return sha256_bytes(("\n".join(sorted(values)) + "\n").encode("utf-8"))


def _safe_relative(root: Path, raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
        return None
    candidate = (root / raw).resolve(strict=False)
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _valid_timestamp(raw: object) -> bool:
    if not isinstance(raw, str) or not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _string_list(raw: object) -> list[str] | None:
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(item, str) and item for item in raw)
    ):
        return None
    return raw


def _criterion_report_bindings(
    raw: object,
) -> tuple[
    str,
    tuple[tuple[str, str | None], ...],
    tuple[tuple[str, tuple[str, ...]], ...],
    tuple[tuple[str, str, str | None], ...],
]:
    """Project manifest evidence into a stable, read-only report index."""

    if not isinstance(raw, dict):
        return "missing", (), (), ()

    minimum_level = raw.get("minimum_level")
    required_level = minimum_level if isinstance(minimum_level, str) else "missing"

    path_bindings: list[tuple[str, str | None]] = []
    paths = raw.get("paths")
    if isinstance(paths, list):
        for entry in paths:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                continue
            expected_hash = entry.get("sha256")
            path_bindings.append(
                (
                    entry["path"],
                    expected_hash if isinstance(expected_hash, str) else None,
                )
            )

    command_bindings: list[tuple[str, tuple[str, ...]]] = []
    commands = raw.get("commands")
    if isinstance(commands, list):
        for entry in commands:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            argv = _string_list(entry.get("argv"))
            if argv is not None:
                command_bindings.append((entry["id"], tuple(argv)))

    receipt_bindings: list[tuple[str, str, str | None]] = []
    receipts = raw.get("receipts")
    if isinstance(receipts, list):
        for entry in receipts:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("command_id"), str)
                or not isinstance(entry.get("path"), str)
            ):
                continue
            expected_hash = entry.get("sha256")
            receipt_bindings.append(
                (
                    entry["command_id"],
                    entry["path"],
                    expected_hash if isinstance(expected_hash, str) else None,
                )
            )

    return (
        required_level,
        tuple(path_bindings),
        tuple(command_bindings),
        tuple(receipt_bindings),
    )


def _receipt_log_findings(
    *,
    subject: str,
    command_id: str,
    receipt: dict[str, Any],
    evidence_root: Path,
) -> list[Finding]:
    """Validate optional runner log bindings without invalidating legacy receipts."""

    findings: list[Finding] = []
    for stream in ("stdout", "stderr"):
        path_field = f"{stream}_path"
        hash_field = f"{stream}_sha256"
        raw_path = receipt.get(path_field)
        expected_hash = receipt.get(hash_field)
        if raw_path is None:
            if stream == "stderr" and expected_hash is not None:
                findings.append(
                    Finding(subject, f"receipt_{path_field}_missing", command_id)
                )
            continue
        candidate = _safe_relative(evidence_root, raw_path)
        if candidate is None:
            findings.append(
                Finding(subject, f"receipt_{path_field}_unsafe", repr(raw_path))
            )
            continue
        if not isinstance(expected_hash, str) or not _HASH_RE.fullmatch(expected_hash):
            findings.append(
                Finding(subject, f"receipt_{hash_field}_invalid", command_id)
            )
            continue
        if not candidate.is_file():
            findings.append(
                Finding(subject, f"receipt_{stream}_missing", str(raw_path))
            )
            continue
        actual_hash = sha256_path(candidate)
        if actual_hash != expected_hash:
            findings.append(
                Finding(
                    subject,
                    f"receipt_{stream}_hash_mismatch",
                    f"{raw_path}: expected {expected_hash}, got {actual_hash}",
                )
            )
    return findings


def _is_repository_verification_command(argv: list[str]) -> bool:
    if not argv:
        return False
    if argv[0] == "scripts/platform":
        return True
    return any(Path(token).name in {"py.test", "pytest"} for token in argv)


def _external_e5_findings(
    *,
    subject: str,
    command_id: str,
    receipt: dict[str, Any],
    command_argv: list[str],
    evidence_root: Path | None,
) -> list[Finding]:
    findings: list[Finding] = []
    if _is_repository_verification_command(command_argv):
        findings.append(
            Finding(
                subject,
                "repository_command_e5_forbidden",
                f"{command_id}: repository verification is capped at E4",
            )
        )
    if receipt.get("kind") != "external_attestation":
        findings.append(
            Finding(
                subject,
                "external_attestation_required",
                f"{command_id}: E5 requires site/organization attestation",
            )
        )
    attestation = receipt.get("external_attestation")
    if not isinstance(attestation, dict):
        findings.append(Finding(subject, "external_attestation_invalid", command_id))
        return findings
    if attestation.get("schema_version") != 1:
        findings.append(
            Finding(subject, "external_attestation_schema_invalid", command_id)
        )
    if attestation.get("scope") != _EXTERNAL_ATTESTATION_SCOPE:
        findings.append(
            Finding(subject, "external_attestation_scope_invalid", command_id)
        )
    for field in ("issuer", "site_id"):
        value = attestation.get(field)
        if not isinstance(value, str) or not value.strip():
            findings.append(
                Finding(subject, f"external_attestation_{field}_invalid", command_id)
            )
    if not _valid_timestamp(attestation.get("issued_at")):
        findings.append(
            Finding(subject, "external_attestation_time_invalid", command_id)
        )
    raw_path = attestation.get("path")
    expected_hash = attestation.get("sha256")
    if evidence_root is None:
        findings.append(
            Finding(subject, "external_attestation_root_missing", command_id)
        )
        return findings
    candidate = _safe_relative(evidence_root, raw_path)
    if candidate is None:
        findings.append(
            Finding(subject, "external_attestation_path_unsafe", repr(raw_path))
        )
        return findings
    if not isinstance(expected_hash, str) or not _HASH_RE.fullmatch(expected_hash):
        findings.append(
            Finding(subject, "external_attestation_hash_invalid", command_id)
        )
        return findings
    if not candidate.is_file():
        findings.append(Finding(subject, "external_attestation_missing", str(raw_path)))
        return findings
    actual_hash = sha256_path(candidate)
    if actual_hash != expected_hash:
        findings.append(
            Finding(
                subject,
                "external_attestation_hash_mismatch",
                f"{raw_path}: expected {expected_hash}, got {actual_hash}",
            )
        )
    return findings


def _evidence_findings(
    *,
    subject: str,
    evidence: object,
    rubric_sha256: str,
    repository_root: Path,
    evidence_root: Path | None,
) -> tuple[list[Finding], str]:
    findings: list[Finding] = []
    if not isinstance(evidence, dict):
        return [
            Finding(subject, "evidence_invalid", "evidence must be an object")
        ], "E0"

    minimum_level = evidence.get("minimum_level")
    if (
        not isinstance(minimum_level, str)
        or minimum_level not in _EVIDENCE_RANK
        or minimum_level in {"E0", "E1", "E2", "E3"}
    ):
        findings.append(
            Finding(
                subject,
                "evidence_level_invalid",
                "minimum_level must be E4 or E5",
            )
        )
        minimum_level = "E4"

    path_hashes: dict[str, str] = {}
    paths = evidence.get("paths")
    if not isinstance(paths, list) or not paths:
        findings.append(
            Finding(subject, "evidence_paths_missing", "at least one path is required")
        )
    else:
        for index, entry in enumerate(paths):
            label = f"path[{index}]"
            if not isinstance(entry, dict):
                findings.append(Finding(subject, "evidence_path_invalid", label))
                continue
            raw_path = entry.get("path")
            expected_hash = entry.get("sha256")
            candidate = _safe_relative(repository_root, raw_path)
            if candidate is None:
                findings.append(
                    Finding(subject, "evidence_path_unsafe", f"{label}: {raw_path!r}")
                )
                continue
            relative = candidate.relative_to(repository_root.resolve()).as_posix()
            if relative in _FORBIDDEN_SELF_EVIDENCE:
                findings.append(
                    Finding(
                        subject,
                        "self_attestation_forbidden",
                        f"review output cannot evidence itself: {relative}",
                    )
                )
                continue
            if not candidate.is_file():
                findings.append(Finding(subject, "evidence_path_missing", relative))
                continue
            if not isinstance(expected_hash, str) or not _HASH_RE.fullmatch(
                expected_hash
            ):
                findings.append(
                    Finding(subject, "evidence_path_hash_missing", relative)
                )
                continue
            actual_hash = sha256_path(candidate)
            if actual_hash != expected_hash:
                findings.append(
                    Finding(
                        subject,
                        "evidence_path_hash_mismatch",
                        f"{relative}: expected {expected_hash}, got {actual_hash}",
                    )
                )
                continue
            if relative in path_hashes:
                findings.append(Finding(subject, "evidence_path_duplicate", relative))
            path_hashes[relative] = actual_hash

    commands = evidence.get("commands")
    command_map: dict[str, list[str]] = {}
    if not isinstance(commands, list) or not commands:
        findings.append(
            Finding(
                subject, "evidence_commands_missing", "at least one command is required"
            )
        )
    else:
        for index, entry in enumerate(commands):
            if not isinstance(entry, dict):
                findings.append(
                    Finding(subject, "evidence_command_invalid", f"command[{index}]")
                )
                continue
            command_id = entry.get("id")
            argv = _string_list(entry.get("argv"))
            if not isinstance(command_id, str) or not command_id:
                findings.append(
                    Finding(subject, "command_id_invalid", f"command[{index}]")
                )
                continue
            if command_id in command_map:
                findings.append(Finding(subject, "command_id_duplicate", command_id))
                continue
            if argv is None:
                findings.append(Finding(subject, "command_argv_invalid", command_id))
                continue
            if any(token in _FORBIDDEN_COMMAND_TOKENS for token in argv):
                findings.append(
                    Finding(subject, "command_weakens_gate", f"{command_id}: {argv}")
                )
            command_map[command_id] = argv

    receipts = evidence.get("receipts")
    receipt_command_ids: set[str] = set()
    verified_levels: list[str] = []
    if not isinstance(receipts, list) or not receipts:
        findings.append(
            Finding(
                subject, "evidence_receipts_missing", "at least one receipt is required"
            )
        )
    else:
        for index, entry in enumerate(receipts):
            if not isinstance(entry, dict):
                findings.append(
                    Finding(subject, "receipt_invalid", f"receipt[{index}]")
                )
                continue
            command_id = entry.get("command_id")
            raw_path = entry.get("path")
            expected_hash = entry.get("sha256")
            if not isinstance(command_id, str) or command_id not in command_map:
                findings.append(
                    Finding(subject, "receipt_command_unknown", repr(command_id))
                )
                continue
            if command_id in receipt_command_ids:
                findings.append(
                    Finding(subject, "receipt_command_duplicate", command_id)
                )
            receipt_command_ids.add(command_id)
            if not isinstance(expected_hash, str) or not _HASH_RE.fullmatch(
                expected_hash
            ):
                findings.append(Finding(subject, "receipt_hash_missing", str(raw_path)))
                continue
            if evidence_root is None:
                findings.append(
                    Finding(
                        subject,
                        "evidence_root_missing",
                        "set --evidence-root or MARKET_RESEARCH_COMPLETENESS_EVIDENCE_ROOT",
                    )
                )
                continue
            receipt_path = _safe_relative(evidence_root, raw_path)
            if receipt_path is None:
                findings.append(Finding(subject, "receipt_path_unsafe", repr(raw_path)))
                continue
            if not receipt_path.is_file():
                findings.append(Finding(subject, "receipt_missing", str(raw_path)))
                continue
            actual_hash = sha256_path(receipt_path)
            if actual_hash != expected_hash:
                findings.append(
                    Finding(
                        subject,
                        "receipt_hash_mismatch",
                        f"{raw_path}: expected {expected_hash}, got {actual_hash}",
                    )
                )
                continue
            try:
                receipt = load_json(receipt_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                findings.append(Finding(subject, "receipt_parse_failed", str(exc)))
                continue
            receipt_finding_start = len(findings)
            receipt_level = receipt.get("evidence_level")
            receipt_paths = receipt.get("path_hashes")
            candidate_level: str | None = None
            if receipt.get("schema_version") != 1:
                findings.append(Finding(subject, "receipt_schema_invalid", command_id))
            if receipt.get("criterion_id") != subject:
                findings.append(
                    Finding(subject, "receipt_criterion_mismatch", command_id)
                )
            if receipt.get("rubric_sha256") != rubric_sha256:
                findings.append(Finding(subject, "receipt_rubric_mismatch", command_id))
            if receipt.get("command_id") != command_id:
                findings.append(
                    Finding(subject, "receipt_command_mismatch", command_id)
                )
            if receipt.get("argv") != command_map[command_id]:
                findings.append(Finding(subject, "receipt_argv_mismatch", command_id))
            if receipt.get("exit_code") != 0:
                findings.append(Finding(subject, "receipt_exit_nonzero", command_id))
            if (
                not isinstance(receipt_level, str)
                or receipt_level not in _EVIDENCE_RANK
            ):
                findings.append(Finding(subject, "receipt_level_invalid", command_id))
            elif _EVIDENCE_RANK[receipt_level] < _EVIDENCE_RANK[minimum_level]:
                findings.append(
                    Finding(subject, "receipt_level_insufficient", command_id)
                )
            else:
                candidate_level = receipt_level
            if receipt_level == "E5":
                findings.extend(
                    _external_e5_findings(
                        subject=subject,
                        command_id=command_id,
                        receipt=receipt,
                        command_argv=command_map[command_id],
                        evidence_root=evidence_root,
                    )
                )
            elif minimum_level == "E5":
                findings.append(
                    Finding(
                        subject,
                        "external_attestation_required",
                        f"{command_id}: E5 requires site/organization attestation",
                    )
                )
            if receipt_paths != path_hashes:
                findings.append(Finding(subject, "receipt_paths_mismatch", command_id))
            if not _valid_timestamp(receipt.get("started_at")) or not _valid_timestamp(
                receipt.get("finished_at")
            ):
                findings.append(Finding(subject, "receipt_time_invalid", command_id))
            if not isinstance(
                receipt.get("repository_commit"), str
            ) or not _COMMIT_RE.fullmatch(receipt["repository_commit"]):
                findings.append(Finding(subject, "receipt_commit_invalid", command_id))
            for field in ("dirty_diff_sha256", "stdout_sha256"):
                value = receipt.get(field)
                if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
                    findings.append(
                        Finding(subject, f"receipt_{field}_invalid", command_id)
                    )
            if evidence_root is not None:
                findings.extend(
                    _receipt_log_findings(
                        subject=subject,
                        command_id=command_id,
                        receipt=receipt,
                        evidence_root=evidence_root,
                    )
                )
            if candidate_level is not None and len(findings) == receipt_finding_start:
                verified_levels.append(candidate_level)

    missing_receipts = sorted(set(command_map) - receipt_command_ids)
    for command_id in missing_receipts:
        findings.append(Finding(subject, "command_receipt_missing", command_id))

    achieved_level = (
        max(verified_levels, key=lambda value: _EVIDENCE_RANK[value])
        if verified_levels
        else "E0"
    )
    return findings, achieved_level


def _evaluate_research_only_matrix(
    *,
    manifest: dict[str, Any],
    manifest_hash: str,
    repository_root: Path,
    evidence_root: Path | None,
) -> Evaluation:
    """Evaluate the canonical 215-row research-only rubric matrix.

    The matrix records assessment state, while completion credit still comes
    only from hash-bound path and command receipts.  This prevents a reviewer
    from turning a narrative ``FULL`` assertion into gate credit.
    """

    findings: list[Finding] = []
    canonical = manifest.get("canonical_source")
    if not isinstance(canonical, dict):
        canonical = {}
        findings.append(Finding("manifest", "canonical_source_missing", "required"))
    expected_criteria = canonical.get("criterion_count")
    expected_blockers = canonical.get("blocker_count")
    expected_areas = canonical.get("area_count")
    for label, actual, expected in (
        ("criterion_count", expected_criteria, 215),
        ("blocker_count", expected_blockers, 11),
        ("area_count", expected_areas, 16),
    ):
        if actual != expected:
            findings.append(
                Finding(
                    "manifest", f"{label}_invalid", f"expected {expected}, got {actual}"
                )
            )
    rubric_hash = canonical.get("sha256")
    if not isinstance(rubric_hash, str) or not _HASH_RE.fullmatch(rubric_hash):
        findings.append(Finding("manifest", "rubric_hash_invalid", repr(rubric_hash)))
        rubric_hash = ""

    policy = manifest.get("decision_policy")
    if not isinstance(policy, dict):
        policy = {}
        findings.append(Finding("manifest", "decision_policy_missing", "required"))
    if policy.get("single_source_of_truth") is not True:
        findings.append(
            Finding("manifest", "single_source_policy_weakened", "must be true")
        )
    completion_text = str(policy.get("completion") or "")
    if not all(token in completion_text for token in ("215", "11", "100")):
        findings.append(
            Finding(
                "manifest",
                "completion_policy_weakened",
                "completion must require 215 criteria, 11 blockers, and score 100",
            )
        )

    areas = manifest.get("areas")
    if not isinstance(areas, list):
        areas = []
        findings.append(Finding("manifest", "areas_missing", "areas must be a list"))
    if len(areas) != 16:
        findings.append(
            Finding("manifest", "area_count_mismatch", f"expected 16, got {len(areas)}")
        )
    area_map: dict[str, tuple[int, tuple[str, ...]]] = {}
    for index, raw_area in enumerate(areas):
        if not isinstance(raw_area, dict):
            findings.append(Finding("manifest", "area_invalid", f"area[{index}]"))
            continue
        area_id = raw_area.get("id")
        weight = raw_area.get("weight")
        raw_ids = raw_area.get("criterion_ids")
        if not isinstance(area_id, str) or not area_id:
            findings.append(Finding("manifest", "area_id_invalid", f"area[{index}]"))
            continue
        if area_id in area_map:
            findings.append(Finding(area_id, "area_duplicate", area_id))
            continue
        if not isinstance(weight, int) or weight <= 0:
            findings.append(Finding(area_id, "area_weight_invalid", repr(weight)))
            weight = 0
        if (
            not isinstance(raw_ids, list)
            or not raw_ids
            or not all(isinstance(item, str) for item in raw_ids)
        ):
            findings.append(Finding(area_id, "area_criteria_invalid", repr(raw_ids)))
            raw_ids = []
        area_map[area_id] = (weight, tuple(raw_ids))
    if sum(item[0] for item in area_map.values()) != 100:
        findings.append(
            Finding(
                "manifest",
                "area_weights_invalid",
                f"expected 100, got {sum(item[0] for item in area_map.values())}",
            )
        )

    raw_criteria = manifest.get("criteria")
    if not isinstance(raw_criteria, list):
        raw_criteria = []
        findings.append(
            Finding("manifest", "criteria_missing", "criteria must be a list")
        )
    if len(raw_criteria) != 215:
        findings.append(
            Finding(
                "manifest",
                "criterion_count_mismatch",
                f"expected 215, got {len(raw_criteria)}",
            )
        )
    ids: list[str] = []
    for item in raw_criteria:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if isinstance(item_id, str):
            ids.append(item_id)
    if len(ids) != len(set(ids)):
        findings.append(
            Finding("manifest", "criterion_ids_duplicate", "IDs must be unique")
        )
    declared_area_ids = [
        item for _weight, values in area_map.values() for item in values
    ]
    if sorted(ids) != sorted(declared_area_ids):
        findings.append(
            Finding(
                "manifest",
                "area_criterion_membership_mismatch",
                "area criterion IDs must cover every criterion exactly once",
            )
        )

    area_scores: dict[str, list[int]] = {area_id: [] for area_id in area_map}
    criterion_results: list[CriterionResult] = []
    allowed_statuses = {"FULL", "PARTIAL", "GAP", "UNDETERMINED"}
    for raw_criterion in raw_criteria:
        if not isinstance(raw_criterion, dict):
            findings.append(
                Finding("manifest", "criterion_invalid", repr(raw_criterion))
            )
            continue
        criterion_id = raw_criterion.get("id")
        if not isinstance(criterion_id, str) or not _ID_RE.fullmatch(criterion_id):
            criterion_id = repr(criterion_id)
            findings.append(Finding(criterion_id, "criterion_id_invalid", criterion_id))
        local: list[Finding] = []
        area_id = raw_criterion.get("area")
        if not isinstance(area_id, str) or area_id not in area_map:
            local.append(Finding(criterion_id, "criterion_area_invalid", repr(area_id)))
            area_id = "unknown"
        elif criterion_id not in area_map[area_id][1]:
            local.append(
                Finding(criterion_id, "criterion_area_membership_invalid", area_id)
            )
        for field in (
            "title",
            "exact_meaning",
            "ideal_state",
            "objective_evidence",
            "verification_method",
            "completion_condition",
        ):
            if (
                not isinstance(raw_criterion.get(field), str)
                or not str(raw_criterion[field]).strip()
            ):
                local.append(Finding(criterion_id, f"{field}_missing", field))
        score = raw_criterion.get("score")
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 5:
            local.append(Finding(criterion_id, "score_missing_or_invalid", repr(score)))
            numeric_score: int | None = None
        else:
            numeric_score = score
            if area_id in area_scores:
                area_scores[area_id].append(score)
        status = raw_criterion.get("current_status")
        if status not in allowed_statuses:
            local.append(
                Finding(criterion_id, "criterion_status_invalid", repr(status))
            )
        elif status != "FULL":
            local.append(Finding(criterion_id, "criterion_not_full", str(status)))
        if status == "FULL" and numeric_score != 5:
            local.append(
                Finding(criterion_id, "full_status_score_mismatch", repr(numeric_score))
            )
        if status == "GAP" and numeric_score not in {0, None}:
            local.append(
                Finding(criterion_id, "gap_status_score_mismatch", repr(numeric_score))
            )
        declared_level = raw_criterion.get("evidence_level")
        if declared_level not in _EVIDENCE_RANK:
            local.append(
                Finding(
                    criterion_id,
                    "declared_evidence_level_invalid",
                    repr(declared_level),
                )
            )
            declared_level = "E0"
        required_level = (
            "E5" if "E5" in str(raw_criterion.get("objective_evidence") or "") else "E4"
        )
        if _EVIDENCE_RANK[declared_level] < _EVIDENCE_RANK[required_level]:
            local.append(
                Finding(
                    criterion_id,
                    "declared_evidence_level_insufficient",
                    f"required {required_level}, got {declared_level}",
                )
            )
        history = raw_criterion.get("assessment_history")
        if not isinstance(history, list) or not history:
            local.append(
                Finding(criterion_id, "assessment_history_missing", "required")
            )
        elif not isinstance(history[-1], dict) or any(
            history[-1].get(field) != value
            for field, value in (
                ("status", status),
                ("score", numeric_score),
                ("evidence_level", declared_level),
            )
        ):
            local.append(
                Finding(
                    criterion_id, "assessment_history_current_state_mismatch", "latest"
                )
            )
        evidence_findings, achieved_level = _evidence_findings(
            subject=criterion_id,
            evidence=raw_criterion.get("evidence"),
            rubric_sha256=rubric_hash,
            repository_root=repository_root,
            evidence_root=evidence_root,
        )
        if status == "FULL":
            local.extend(evidence_findings)
        projected = _criterion_report_bindings(raw_criterion.get("evidence"))
        complete = not local and status == "FULL" and numeric_score == 5
        criterion_results.append(
            CriterionResult(
                criterion_id=criterion_id,
                area_id=area_id,
                score=numeric_score,
                evidence_level=achieved_level,
                complete=complete,
                finding_codes=tuple(sorted({item.code for item in local})),
                required_evidence_level=required_level,
                evidence_paths=projected[1],
                verification_commands=projected[2],
                receipt_bindings=projected[3],
                local_findings=tuple(local),
            )
        )
        findings.extend(local)

    declared_score = 0.0
    for area_id, (weight, criterion_ids) in area_map.items():
        values = area_scores.get(area_id, [])
        if len(values) != len(criterion_ids):
            findings.append(
                Finding(
                    area_id,
                    "area_score_incomplete",
                    f"{len(values)}/{len(criterion_ids)}",
                )
            )
            continue
        declared_score += (sum(values) / len(values)) / 5.0 * weight

    blockers = manifest.get("blockers")
    if not isinstance(blockers, list):
        blockers = []
        findings.append(Finding("manifest", "blockers_missing", "must be a list"))
    if len(blockers) != 11:
        findings.append(
            Finding(
                "manifest",
                "blocker_count_mismatch",
                f"expected 11, got {len(blockers)}",
            )
        )
    blocker_ids: list[str] = []
    for raw_blocker in blockers:
        if not isinstance(raw_blocker, dict):
            findings.append(Finding("manifest", "blocker_invalid", repr(raw_blocker)))
            continue
        blocker_id = raw_blocker.get("id")
        if not isinstance(blocker_id, str) or not re.fullmatch(
            r"B-[0-9]{2}", blocker_id
        ):
            findings.append(Finding("manifest", "blocker_id_invalid", repr(blocker_id)))
            continue
        blocker_ids.append(blocker_id)
        if raw_blocker.get("required_status") != "PASS":
            findings.append(
                Finding(blocker_id, "blocker_policy_weakened", "PASS required")
            )
        current_status = raw_blocker.get("current_status")
        if current_status != "PASS":
            findings.append(
                Finding(
                    blocker_id,
                    "blocker_not_cleared",
                    str(current_status),
                )
            )
        declared_level = raw_blocker.get("evidence_level")
        if declared_level not in _EVIDENCE_RANK:
            findings.append(
                Finding(
                    blocker_id,
                    "declared_evidence_level_invalid",
                    repr(declared_level),
                )
            )
            declared_level = "E0"
        elif current_status == "PASS" and _EVIDENCE_RANK[declared_level] < 4:
            findings.append(
                Finding(
                    blocker_id,
                    "declared_evidence_level_insufficient",
                    f"required E4, got {declared_level}",
                )
            )
        history = raw_blocker.get("assessment_history")
        if not isinstance(history, list) or not history:
            findings.append(Finding(blocker_id, "blocker_history_missing", "required"))
        elif not isinstance(history[-1], dict) or any(
            history[-1].get(field) != value
            for field, value in (
                ("status", current_status),
                ("evidence_level", declared_level),
            )
        ):
            findings.append(
                Finding(blocker_id, "blocker_history_current_state_mismatch", "latest")
            )
        if current_status == "PASS":
            blocker_evidence_findings, _achieved_level = _evidence_findings(
                subject=blocker_id,
                evidence=raw_blocker.get("evidence"),
                rubric_sha256=rubric_hash,
                repository_root=repository_root,
                evidence_root=evidence_root,
            )
            findings.extend(blocker_evidence_findings)
    if len(blocker_ids) != len(set(blocker_ids)):
        findings.append(
            Finding("manifest", "blocker_ids_duplicate", "IDs must be unique")
        )

    return Evaluation(
        manifest_sha256=manifest_hash,
        rubric_sha256=rubric_hash,
        expected_criteria=215,
        declared_score=round(declared_score, 12),
        verified_criteria=sum(item.complete for item in criterion_results),
        criteria=tuple(criterion_results),
        findings=tuple(findings),
    )


def _full_scope_for_criterion_id(criterion_id: str) -> str:
    """Return the rubric product scope encoded by a full-scope criterion ID."""

    stage_text, suffix = criterion_id.split("-", 1)
    prefix = suffix.rstrip("0123456789")
    if prefix == "S":
        return "SPOT"
    if prefix in {"F", "FP"}:
        return "FUTURES"
    if prefix in {"O", "OM", "OP"}:
        return "OPTIONS"
    if prefix == "P":
        return "DERIVATIVES_PORTFOLIO"
    if prefix == "R" and stage_text == "S5":
        return "DERIVATIVES_RISK"
    return "CORE"


def _evaluate_full_scope_matrix(
    *,
    manifest: dict[str, Any],
    manifest_hash: str,
    repository_root: Path,
    evidence_root: Path | None,
) -> Evaluation:
    """Fail closed on the current Spot/Futures/Options 431-row rubric.

    Unlike the historical matrix, the rubric and instruction hashes plus all
    criterion and blocker counts are bound to this evaluator generation.  A
    count or ID can therefore change only with an explicit evaluator and
    canonical-matrix revision.
    """

    findings: list[Finding] = []
    matrix_assessment = manifest.get("assessment")
    if not isinstance(matrix_assessment, dict):
        findings.append(Finding("manifest", "assessment_missing", "required"))
    else:
        iteration = matrix_assessment.get("iteration")
        if not isinstance(iteration, int) or iteration < 2:
            findings.append(
                Finding(
                    "manifest",
                    "assessment_stale",
                    f"current re-assessment iteration required, got {iteration!r}",
                )
            )
    canonical = manifest.get("canonical_source")
    if not isinstance(canonical, dict):
        canonical = {}
        findings.append(Finding("manifest", "canonical_source_missing", "required"))
    rubric_hash = canonical.get("sha256")
    if rubric_hash != _FULL_SCOPE_RUBRIC_SHA256:
        findings.append(
            Finding(
                "manifest",
                "rubric_hash_mismatch",
                f"expected {_FULL_SCOPE_RUBRIC_SHA256}, got {rubric_hash!r}",
            )
        )
        rubric_hash = "" if not isinstance(rubric_hash, str) else rubric_hash
    instruction_hash = canonical.get("instruction_sha256")
    if instruction_hash != _FULL_SCOPE_INSTRUCTION_SHA256:
        findings.append(
            Finding(
                "manifest",
                "instruction_hash_mismatch",
                f"expected {_FULL_SCOPE_INSTRUCTION_SHA256}, got {instruction_hash!r}",
            )
        )
    expected_criteria = canonical.get("criterion_count")
    expected_blockers = canonical.get("blocker_count")
    explicit_count = canonical.get("explicit_criterion_count")
    supplemental_count = canonical.get("supplemental_normative_criterion_count")
    if expected_criteria != _FULL_SCOPE_CRITERION_COUNT:
        findings.append(
            Finding(
                "manifest",
                "criterion_count_invalid",
                f"expected {_FULL_SCOPE_CRITERION_COUNT}, got {expected_criteria!r}",
            )
        )
        expected_criteria = _FULL_SCOPE_CRITERION_COUNT
    if expected_blockers != _FULL_SCOPE_BLOCKER_COUNT:
        findings.append(
            Finding(
                "manifest",
                "blocker_count_invalid",
                f"expected {_FULL_SCOPE_BLOCKER_COUNT}, got {expected_blockers}",
            )
        )
    if (
        explicit_count != _FULL_SCOPE_EXPLICIT_COUNT
        or supplemental_count != _FULL_SCOPE_SUPPLEMENTAL_COUNT
    ):
        findings.append(
            Finding(
                "manifest",
                "criterion_source_count_mismatch",
                "expected "
                f"{_FULL_SCOPE_EXPLICIT_COUNT}+{_FULL_SCOPE_SUPPLEMENTAL_COUNT}, "
                f"got {explicit_count!r}+{supplemental_count!r}",
            )
        )

    criteria = manifest.get("criteria")
    if not isinstance(criteria, list):
        criteria = []
        findings.append(Finding("manifest", "criteria_missing", "must be a list"))
    if len(criteria) != expected_criteria:
        findings.append(
            Finding(
                "manifest",
                "criterion_count_mismatch",
                f"expected {expected_criteria}, got {len(criteria)}",
            )
        )
    ids = [
        row.get("id")
        for row in criteria
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    ]
    if len(ids) != len(set(ids)):
        findings.append(
            Finding("manifest", "criterion_ids_duplicate", "IDs must be unique")
        )

    stage_scores: dict[int, list[int]] = {stage: [] for stage in range(1, 8)}
    stage_weights: dict[int, int] = {}
    criterion_results: list[CriterionResult] = []
    for raw in criteria:
        if not isinstance(raw, dict):
            findings.append(Finding("manifest", "criterion_invalid", repr(raw)))
            continue
        criterion_id = raw.get("id")
        if not isinstance(criterion_id, str) or not _FULL_SCOPE_ID_RE.fullmatch(
            criterion_id
        ):
            findings.append(
                Finding(str(criterion_id), "criterion_id_invalid", repr(criterion_id))
            )
            continue
        local: list[Finding] = []
        stage = raw.get("stage")
        weight = raw.get("stage_weight")
        if not isinstance(stage, int) or stage not in stage_scores:
            local.append(Finding(criterion_id, "criterion_stage_invalid", repr(stage)))
            stage = 0
        elif not isinstance(weight, int) or weight <= 0:
            local.append(Finding(criterion_id, "criterion_weight_invalid", repr(weight)))
        elif stage in stage_weights and stage_weights[stage] != weight:
            local.append(
                Finding(criterion_id, "criterion_weight_inconsistent", repr(weight))
            )
        else:
            stage_weights[stage] = weight
        scope = raw.get("scope")
        if scope not in _FULL_SCOPE_SCOPES:
            local.append(
                Finding(criterion_id, "criterion_scope_invalid", repr(scope))
            )
            scope = "unknown"
        expected_scope = _full_scope_for_criterion_id(criterion_id)
        if scope != "unknown" and scope != expected_scope:
            local.append(
                Finding(
                    criterion_id,
                    "criterion_scope_mismatch",
                    f"expected {expected_scope}, got {scope}",
                )
            )
        for field_name in (
            "title",
            "exact_meaning",
            "ideal_state",
            "completion_condition",
        ):
            if not isinstance(raw.get(field_name), str) or not str(
                raw.get(field_name)
            ).strip():
                local.append(
                    Finding(criterion_id, f"{field_name}_missing", field_name)
                )
        for field_name in (
            "inspection_targets",
            "objective_evidence",
            "dependencies",
            "verification_method",
        ):
            value = raw.get(field_name)
            if (
                not isinstance(value, list)
                or not value
                or not all(isinstance(item, str) and item for item in value)
            ):
                local.append(
                    Finding(criterion_id, f"{field_name}_invalid", field_name)
                )
        assessment = raw.get("current_assessment")
        if not isinstance(assessment, dict):
            assessment = raw.get("baseline_assessment")
            if not isinstance(assessment, dict):
                assessment = {}
            local.append(
                Finding(criterion_id, "current_assessment_missing", "required")
            )
        else:
            for field_name in ("code_evidence", "test_evidence"):
                value = assessment.get(field_name)
                if (
                    not isinstance(value, list)
                    or not value
                    or not all(isinstance(item, str) and item for item in value)
                ):
                    local.append(
                        Finding(
                            criterion_id,
                            f"current_{field_name}_invalid",
                            field_name,
                        )
                    )
        history = raw.get("assessment_history")
        if not isinstance(history, list) or len(history) < 2:
            local.append(
                Finding(criterion_id, "assessment_history_incomplete", "required")
            )
        elif isinstance(assessment, dict) and history[-1] != assessment:
            local.append(
                Finding(
                    criterion_id,
                    "assessment_history_current_state_mismatch",
                    "latest",
                )
            )
        score = assessment.get("score")
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 5:
            local.append(Finding(criterion_id, "score_invalid", repr(score)))
            numeric_score: int | None = None
        else:
            numeric_score = score
            if stage in stage_scores:
                stage_scores[stage].append(score)
        status = assessment.get("status")
        if status not in {"FULL", "PARTIAL", "GAP", "UNDETERMINED"}:
            local.append(Finding(criterion_id, "criterion_status_invalid", repr(status)))
        elif status != "FULL":
            local.append(Finding(criterion_id, "criterion_not_full", str(status)))
        if status == "FULL" and numeric_score != 5:
            local.append(
                Finding(criterion_id, "full_status_score_mismatch", repr(score))
            )
        declared_level = assessment.get("evidence_level")
        if declared_level not in _EVIDENCE_RANK:
            local.append(
                Finding(
                    criterion_id,
                    "declared_evidence_level_invalid",
                    repr(declared_level),
                )
            )
            declared_level = "E0"
        required_level = raw.get("required_evidence_level")
        if required_level not in {"E4", "E5"}:
            required_level = "E5" if int(stage or 0) in {4, 6, 7} else "E4"
        evidence_paths: tuple[tuple[str, str | None], ...] = ()
        verification_commands: tuple[tuple[str, tuple[str, ...]], ...] = ()
        receipt_bindings: tuple[tuple[str, str, str | None], ...] = ()
        achieved_level = "E0"
        if status == "FULL":
            if _EVIDENCE_RANK[str(declared_level)] < _EVIDENCE_RANK[required_level]:
                local.append(
                    Finding(
                        criterion_id,
                        "declared_evidence_level_insufficient",
                        f"required {required_level}, got {declared_level}",
                    )
                )
            evidence_findings, achieved_level = _evidence_findings(
                subject=criterion_id,
                evidence=raw.get("evidence"),
                rubric_sha256=rubric_hash,
                repository_root=repository_root,
                evidence_root=evidence_root,
            )
            local.extend(evidence_findings)
            projected = _criterion_report_bindings(raw.get("evidence"))
            evidence_paths = projected[1]
            verification_commands = projected[2]
            receipt_bindings = projected[3]
        complete = not local and status == "FULL" and numeric_score == 5
        criterion_results.append(
            CriterionResult(
                criterion_id=criterion_id,
                area_id=scope,
                score=numeric_score,
                evidence_level=achieved_level,
                complete=complete,
                finding_codes=tuple(sorted({finding.code for finding in local})),
                required_evidence_level=required_level,
                evidence_paths=evidence_paths,
                verification_commands=verification_commands,
                receipt_bindings=receipt_bindings,
                local_findings=tuple(local),
            )
        )
        findings.extend(local)

    if set(stage_weights) != set(range(1, 8)) or sum(stage_weights.values()) != 100:
        findings.append(
            Finding(
                "manifest",
                "stage_weights_invalid",
                repr(dict(sorted(stage_weights.items()))),
            )
        )
    declared_score = 0.0
    for stage, weight in stage_weights.items():
        values = stage_scores[stage]
        if not values:
            findings.append(
                Finding(f"S{stage}", "stage_score_missing", "no criteria")
            )
            continue
        declared_score += (sum(values) / len(values)) / 5.0 * weight

    blockers = manifest.get("blockers")
    if not isinstance(blockers, list):
        blockers = []
        findings.append(Finding("manifest", "blockers_missing", "must be a list"))
    if len(blockers) != _FULL_SCOPE_BLOCKER_COUNT:
        findings.append(
            Finding("manifest", "blocker_count_mismatch", f"got {len(blockers)}")
        )
    blocker_ids: list[str] = []
    for blocker in blockers:
        if not isinstance(blocker, dict):
            findings.append(Finding("manifest", "blocker_invalid", repr(blocker)))
            continue
        blocker_id = blocker.get("id")
        if not isinstance(blocker_id, str) or not re.fullmatch(r"B-[0-9]{2}", blocker_id):
            findings.append(
                Finding("manifest", "blocker_id_invalid", repr(blocker_id))
            )
            continue
        blocker_ids.append(blocker_id)
        status = blocker.get("current_status")
        if status is None:
            findings.append(
                Finding(blocker_id, "current_blocker_status_missing", "required")
            )
            status = blocker.get("baseline_status")
        blocker_history = blocker.get("assessment_history")
        if not isinstance(blocker_history, list) or len(blocker_history) < 2:
            findings.append(
                Finding(blocker_id, "blocker_history_incomplete", "required")
            )
        elif (
            not isinstance(blocker_history[-1], dict)
            or blocker_history[-1].get("status") != status
        ):
            findings.append(
                Finding(
                    blocker_id,
                    "blocker_history_current_state_mismatch",
                    repr(status),
                )
            )
        if status != "PASS":
            findings.append(Finding(blocker_id, "blocker_not_cleared", str(status)))
        elif blocker.get("evidence") is None:
            findings.append(Finding(blocker_id, "blocker_evidence_missing", "required"))
        else:
            blocker_findings, _level = _evidence_findings(
                subject=blocker_id,
                evidence=blocker.get("evidence"),
                rubric_sha256=rubric_hash,
                repository_root=repository_root,
                evidence_root=evidence_root,
            )
            findings.extend(blocker_findings)
    if blocker_ids != [
        f"B-{number:02d}" for number in range(1, _FULL_SCOPE_BLOCKER_COUNT + 1)
    ]:
        findings.append(
            Finding("manifest", "blocker_ids_mismatch", repr(blocker_ids))
        )

    return Evaluation(
        manifest_sha256=manifest_hash,
        rubric_sha256=rubric_hash,
        expected_criteria=expected_criteria,
        declared_score=round(declared_score, 12),
        verified_criteria=sum(item.complete for item in criterion_results),
        criteria=tuple(criterion_results),
        findings=tuple(sorted(set(findings))),
    )


def evaluate_manifest(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    repository_root: Path = PROJECT_ROOT,
    evidence_root: Path | None = None,
) -> Evaluation:
    raw_bytes = manifest_path.read_bytes()
    manifest_hash = sha256_bytes(raw_bytes)
    manifest = json.loads(
        raw_bytes.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
    )
    if not isinstance(manifest, dict):
        raise ValueError("manifest root must be an object")

    canonical = manifest.get("canonical_source")
    if (
        manifest.get("schema_version") == 2
        and isinstance(canonical, dict)
        and canonical.get("blocker_count") == 19
    ):
        return _evaluate_full_scope_matrix(
            manifest=manifest,
            manifest_hash=manifest_hash,
            repository_root=repository_root,
            evidence_root=evidence_root,
        )

    if "canonical_source" in manifest and "decision_policy" in manifest:
        return _evaluate_research_only_matrix(
            manifest=manifest,
            manifest_hash=manifest_hash,
            repository_root=repository_root,
            evidence_root=evidence_root,
        )

    findings: list[Finding] = []
    if manifest.get("schema_version") != 1:
        findings.append(
            Finding("manifest", "schema_invalid", "expected schema_version 1")
        )

    policy = manifest.get("completion_policy")
    if not isinstance(policy, dict):
        policy = {}
        findings.append(Finding("manifest", "policy_missing", "completion_policy"))
    expected_criteria = policy.get("criterion_count")
    if not isinstance(expected_criteria, int) or expected_criteria <= 0:
        expected_criteria = 0
        findings.append(
            Finding("manifest", "criterion_count_invalid", repr(expected_criteria))
        )
    if policy.get("required_score") != 5:
        findings.append(Finding("manifest", "required_score_weakened", "must be 5"))
    if policy.get("required_capability_status") != "supported":
        findings.append(
            Finding("manifest", "capability_policy_weakened", "must be supported")
        )
    if policy.get("allow_not_applicable") is not False:
        findings.append(Finding("manifest", "na_policy_weakened", "must be false"))

    rubric = manifest.get("rubric")
    rubric_hash = rubric.get("source_sha256") if isinstance(rubric, dict) else None
    if not isinstance(rubric_hash, str) or not _HASH_RE.fullmatch(rubric_hash):
        findings.append(Finding("manifest", "rubric_hash_invalid", repr(rubric_hash)))
        rubric_hash = ""

    areas = manifest.get("areas")
    area_map: dict[str, tuple[int, list[str]]] = {}
    if not isinstance(areas, list):
        areas = []
        findings.append(Finding("manifest", "areas_missing", "areas must be a list"))
    for index, area in enumerate(areas):
        if not isinstance(area, dict):
            findings.append(Finding("manifest", "area_invalid", f"area[{index}]"))
            continue
        area_id = area.get("id")
        weight = area.get("weight")
        criterion_ids = area.get("criterion_ids")
        if not isinstance(area_id, str) or not area_id:
            findings.append(Finding("manifest", "area_id_invalid", f"area[{index}]"))
            continue
        if area_id in area_map:
            findings.append(Finding(area_id, "area_duplicate", area_id))
            continue
        if not isinstance(weight, int) or weight <= 0:
            findings.append(Finding(area_id, "area_weight_invalid", repr(weight)))
            weight = 0
        if not isinstance(criterion_ids, list) or not all(
            isinstance(item, str) for item in criterion_ids
        ):
            findings.append(
                Finding(area_id, "area_criteria_invalid", repr(criterion_ids))
            )
            criterion_ids = []
        area_map[area_id] = (weight, criterion_ids)
    if sum(weight for weight, _ids in area_map.values()) != 100:
        findings.append(
            Finding(
                "manifest",
                "area_weights_invalid",
                f"expected 100, got {sum(weight for weight, _ids in area_map.values())}",
            )
        )

    criteria = manifest.get("criteria")
    if not isinstance(criteria, list):
        criteria = []
        findings.append(
            Finding("manifest", "criteria_missing", "criteria must be a list")
        )
    criterion_ids = [
        value.get("id")
        for value in criteria
        if isinstance(value, dict) and isinstance(value.get("id"), str)
    ]
    if len(criteria) != expected_criteria:
        findings.append(
            Finding(
                "manifest",
                "criterion_count_mismatch",
                f"expected {expected_criteria}, got {len(criteria)}",
            )
        )
    if len(criterion_ids) != len(set(criterion_ids)):
        findings.append(
            Finding("manifest", "criterion_ids_duplicate", "IDs must be unique")
        )
    declared_ids_hash = (
        rubric.get("criterion_ids_sha256") if isinstance(rubric, dict) else None
    )
    actual_ids_hash = criterion_ids_sha256(criterion_ids)
    if declared_ids_hash != actual_ids_hash:
        findings.append(
            Finding(
                "manifest",
                "criterion_ids_hash_mismatch",
                f"expected {declared_ids_hash}, got {actual_ids_hash}",
            )
        )

    area_declared_scores: dict[str, list[int]] = {area_id: [] for area_id in area_map}
    criterion_results: list[CriterionResult] = []
    all_area_ids: set[str] = set()
    for raw_criterion in criteria:
        if not isinstance(raw_criterion, dict):
            findings.append(
                Finding("manifest", "criterion_invalid", repr(raw_criterion))
            )
            continue
        criterion_id = raw_criterion.get("id")
        if not isinstance(criterion_id, str) or not _ID_RE.fullmatch(criterion_id):
            criterion_id = repr(criterion_id)
            findings.append(Finding(criterion_id, "criterion_id_invalid", criterion_id))
        start = len(findings)
        if (
            not isinstance(raw_criterion.get("rubric_title"), str)
            or not raw_criterion["rubric_title"].strip()
        ):
            findings.append(
                Finding(criterion_id, "rubric_title_missing", "rubric_title")
            )
        section_hash = raw_criterion.get("rubric_section_sha256")
        if not isinstance(section_hash, str) or not _HASH_RE.fullmatch(section_hash):
            findings.append(
                Finding(
                    criterion_id,
                    "rubric_section_hash_invalid",
                    repr(section_hash),
                )
            )
        for field in ("acceptance", "verification_expectation", "priority_and_risk"):
            value = raw_criterion.get(field)
            if not isinstance(value, str) or not value.strip():
                findings.append(
                    Finding(criterion_id, "criterion_metadata_missing", field)
                )
        area_id = raw_criterion.get("area_id")
        if not isinstance(area_id, str) or area_id not in area_map:
            findings.append(
                Finding(criterion_id, "criterion_area_invalid", repr(area_id))
            )
            area_id = ""
        else:
            all_area_ids.add(area_id)
            weight, expected_ids = area_map[area_id]
            if raw_criterion.get("area_weight") != weight:
                findings.append(
                    Finding(criterion_id, "criterion_weight_mismatch", repr(weight))
                )
            if criterion_id not in expected_ids:
                findings.append(Finding(criterion_id, "criterion_not_in_area", area_id))
        if raw_criterion.get("required_score") != 5:
            findings.append(
                Finding(criterion_id, "criterion_requirement_weakened", "must be 5")
            )
        score = raw_criterion.get("declared_score")
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 5:
            findings.append(Finding(criterion_id, "score_missing", repr(score)))
            score = None
        elif score < 5:
            findings.append(
                Finding(
                    criterion_id, "score_below_required", f"expected 5, got {score}"
                )
            )
        if area_id in area_declared_scores:
            area_declared_scores[area_id].append(score if score is not None else 0)
        capability = raw_criterion.get("capability_status")
        if capability != "supported":
            findings.append(
                Finding(
                    criterion_id,
                    "capability_not_supported",
                    f"expected supported, got {capability!r}",
                )
            )
        raw_evidence = raw_criterion.get("evidence")
        evidence_findings, evidence_level = _evidence_findings(
            subject=criterion_id,
            evidence=raw_evidence,
            rubric_sha256=rubric_hash,
            repository_root=repository_root,
            evidence_root=evidence_root,
        )
        findings.extend(evidence_findings)
        local_findings = tuple(sorted(set(findings[start:])))
        local_codes = tuple(sorted({item.code for item in local_findings}))
        (
            required_evidence_level,
            evidence_paths,
            verification_commands,
            receipt_bindings,
        ) = _criterion_report_bindings(raw_evidence)
        criterion_results.append(
            CriterionResult(
                criterion_id=criterion_id,
                area_id=area_id,
                score=score,
                evidence_level=evidence_level,
                complete=not local_codes,
                finding_codes=local_codes,
                required_evidence_level=required_evidence_level,
                evidence_paths=evidence_paths,
                verification_commands=verification_commands,
                receipt_bindings=receipt_bindings,
                local_findings=local_findings,
            )
        )

    for area_id, (_weight, expected_ids) in area_map.items():
        actual_ids = [
            result.criterion_id
            for result in criterion_results
            if result.area_id == area_id
        ]
        if actual_ids != expected_ids:
            findings.append(
                Finding(
                    area_id, "area_criterion_order_mismatch", "criterion_ids differ"
                )
            )
    if set(area_map) != all_area_ids:
        findings.append(
            Finding(
                "manifest",
                "empty_area",
                ", ".join(sorted(set(area_map) - all_area_ids)),
            )
        )

    blockers = manifest.get("blockers")
    if not isinstance(blockers, list):
        blockers = []
        findings.append(
            Finding("manifest", "blockers_missing", "blockers must be a list")
        )
    blocker_ids: list[str] = []
    for blocker in blockers:
        if not isinstance(blocker, dict):
            findings.append(Finding("manifest", "blocker_invalid", repr(blocker)))
            continue
        blocker_id = blocker.get("id")
        if not isinstance(blocker_id, str) or not re.fullmatch(
            r"B-[0-9]{2}", blocker_id
        ):
            blocker_id = repr(blocker_id)
            findings.append(Finding(blocker_id, "blocker_id_invalid", blocker_id))
        blocker_ids.append(blocker_id)
        if (
            not isinstance(blocker.get("rubric_title"), str)
            or not blocker["rubric_title"].strip()
        ):
            findings.append(Finding(blocker_id, "rubric_title_missing", "rubric_title"))
        section_hash = blocker.get("rubric_section_sha256")
        if not isinstance(section_hash, str) or not _HASH_RE.fullmatch(section_hash):
            findings.append(
                Finding(
                    blocker_id,
                    "rubric_section_hash_invalid",
                    repr(section_hash),
                )
            )
        for field in ("acceptance", "verification_expectation", "priority_and_risk"):
            value = blocker.get(field)
            if not isinstance(value, str) or not value.strip():
                findings.append(Finding(blocker_id, "blocker_metadata_missing", field))
        if blocker.get("status") != "cleared":
            findings.append(
                Finding(
                    blocker_id,
                    "blocker_not_cleared",
                    f"expected cleared, got {blocker.get('status')!r}",
                )
            )
        blocker_findings, _level = _evidence_findings(
            subject=blocker_id,
            evidence=blocker.get("evidence"),
            rubric_sha256=rubric_hash,
            repository_root=repository_root,
            evidence_root=evidence_root,
        )
        findings.extend(blocker_findings)
    expected_blockers = policy.get("blocker_ids")
    if blocker_ids != expected_blockers:
        findings.append(Finding("manifest", "blocker_ids_mismatch", f"{blocker_ids!r}"))

    declared_score = 0.0
    for area_id, (weight, expected_ids) in area_map.items():
        values = area_declared_scores[area_id]
        if len(values) != len(expected_ids) or not expected_ids:
            continue
        declared_score += (sum(values) / len(expected_ids)) / 5.0 * weight

    sorted_findings = tuple(sorted(set(findings)))
    complete_ids = {
        result.criterion_id for result in criterion_results if result.complete
    }
    return Evaluation(
        manifest_sha256=manifest_hash,
        rubric_sha256=rubric_hash,
        expected_criteria=expected_criteria,
        declared_score=declared_score,
        verified_criteria=len(complete_ids),
        criteria=tuple(criterion_results),
        findings=sorted_findings,
    )


def _markdown_table_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("|", "&#124;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", " ")
    )


def _markdown_table_code(value: str) -> str:
    return f"`{_markdown_table_text(value).replace('`', '&#96;')}`"


def _criterion_evidence_row(result: CriterionResult) -> str:
    score = "missing/5" if result.score is None else f"{result.score}/5"
    implementation_evidence = (
        "<br>".join(
            (
                f"{_markdown_table_code(path)} "
                f"({_markdown_table_code(f'sha256:{expected_hash}')})"
                if expected_hash is not None
                else f"{_markdown_table_code(path)} (hash missing)"
            )
            for path, expected_hash in result.evidence_paths
        )
        or "none declared"
    )
    verification_commands = (
        "<br>".join(
            f"{_markdown_table_code(command_id)}: "
            f"{_markdown_table_code(json.dumps(list(argv), ensure_ascii=False))}"
            for command_id, argv in result.verification_commands
        )
        or "none declared"
    )
    runtime_evidence = (
        f"{_markdown_table_code(result.evidence_level)} "
        f"(required {_markdown_table_code(result.required_evidence_level)})"
    )
    receipt_bindings = (
        "<br>".join(
            f"{_markdown_table_code(command_id)}: {_markdown_table_code(path)} / "
            + (
                _markdown_table_code(f"sha256:{expected_hash}")
                if expected_hash is not None
                else "hash missing"
            )
            for command_id, path, expected_hash in result.receipt_bindings
        )
        or "none declared"
    )
    related_files = (
        "<br>".join(
            _markdown_table_code(path) for path, _expected_hash in result.evidence_paths
        )
        or "none declared"
    )
    remaining_findings = (
        "<br>".join(
            f"{_markdown_table_code(finding.code)} — "
            f"{_markdown_table_text(finding.detail)}"
            for finding in result.local_findings
        )
        or "none"
    )
    return (
        f"| {_markdown_table_text(result.criterion_id)} | {score} | "
        f"{implementation_evidence} | {verification_commands} | "
        f"{runtime_evidence} | {receipt_bindings} | {related_files} | "
        f"{remaining_findings} |"
    )


def render_report(evaluation: Evaluation) -> str:
    status = "COMPLETE" if evaluation.complete else "INCOMPLETE"
    finding_counts: dict[str, int] = {}
    for finding in evaluation.findings:
        finding_counts[finding.code] = finding_counts.get(finding.code, 0) + 1
    open_blockers = sorted(
        {
            finding.subject
            for finding in evaluation.findings
            if re.fullmatch(r"B-[0-9]{2}", finding.subject)
            and finding.code == "blocker_not_cleared"
        }
    )
    lines = [
        "# Platform completeness gate status",
        "",
        "<!-- Generated by tools/platform_completeness.py; do not edit manually. -->",
        "",
        f"- Status: **{status}**",
        f"- Strict declared score: **{evaluation.declared_score:.2f}/100**",
        (
            "- Receipt-verified criteria: "
            f"**{evaluation.verified_criteria}/{evaluation.expected_criteria}**"
        ),
        f"- Open findings: **{len(evaluation.findings)}**",
        f"- Rubric SHA-256: `{evaluation.rubric_sha256}`",
        f"- Manifest SHA-256: `{evaluation.manifest_sha256}`",
        "",
        "A declared score never substitutes for hash-bound path and command-receipt "
        f"evidence. All {evaluation.expected_criteria} criteria remain in the "
        "denominator; unsupported and "
        "not-applicable capabilities fail this completion gate.",
        "",
        "## Failure summary",
        "",
        "| Finding code | Count |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| {code} | {finding_counts[code]} |" for code in sorted(finding_counts)
    )
    if not finding_counts:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "Open blocking conditions: "
            + (", ".join(open_blockers) if open_blockers else "none"),
            "",
            "## Criterion decisions",
            "",
            "| Criterion | Area | Declared | Evidence | Gate | Reasons |",
            "| --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for result in evaluation.criteria:
        score = "missing" if result.score is None else str(result.score)
        gate = "PASS" if result.complete else "FAIL"
        reasons = ", ".join(result.finding_codes) or "none"
        lines.append(
            f"| {result.criterion_id} | {result.area_id} | {score} | "
            f"{result.evidence_level} | {gate} | {reasons} |"
        )
    lines.extend(
        [
            "",
            "## Criterion evidence catalog",
            "",
            "Each criterion is projected directly from its manifest evidence. The "
            "manifest uses one hash-bound path collection, so the same declared "
            "paths are also listed as related files without inferring undocumented "
            "file roles.",
            "",
            "| Criterion | Final score | Production / implementation evidence "
            "paths | Test / verification commands | Runtime evidence level | "
            "Receipt path / hash | Related files | Remaining findings / issues |",
            "| --- | ---: | --- | --- | --- | --- | --- | --- |",
        ]
    )
    lines.extend(_criterion_evidence_row(result) for result in evaluation.criteria)
    lines.extend(
        [
            "",
            "## Findings",
            "",
        ]
    )
    if evaluation.findings:
        lines.extend(
            f"- `{item.subject}` `{item.code}` — {item.detail}"
            for item in evaluation.findings
        )
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Reproduction",
            "",
            "```console",
            "uv run --frozen --no-sync --package market-research python "
            "tools/platform_completeness.py --check-report",
            "```",
            "",
            "Receipts are read from the repository-external directory supplied by "
            "`--evidence-root` or `MARKET_RESEARCH_COMPLETENESS_EVIDENCE_ROOT`.",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _atomic_write_evidence(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validated_evidence_root(*, evidence_root: Path, repository_root: Path) -> Path:
    if not evidence_root.is_absolute():
        raise EvidenceRunError("evidence root must be an absolute path")
    resolved_repository = repository_root.resolve(strict=True)
    resolved_evidence = evidence_root.resolve(strict=False)
    if resolved_evidence == Path(resolved_evidence.anchor):
        raise EvidenceRunError("filesystem root cannot be used as the evidence root")
    if _is_relative_to(resolved_evidence, resolved_repository):
        raise EvidenceRunError("evidence root must be repository-external")
    if resolved_evidence.exists():
        if not resolved_evidence.is_dir():
            raise EvidenceRunError("evidence root exists and is not a directory")
        if any(resolved_evidence.iterdir()):
            raise EvidenceRunError("evidence root must be new or empty")
    return resolved_evidence


def validate_evidence_argv(argv: tuple[str, ...], *, repository_root: Path) -> None:
    """Reject every command outside the intentionally small evidence grammar."""

    if not argv:
        raise EvidenceRunError("empty command argv")
    root = repository_root.resolve(strict=True)
    if argv[0] == ".venv/bin/pytest":
        executable = root / argv[0]
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise EvidenceRunError("allowlisted pytest executable is unavailable")
        selectors = 0
        for token in argv[1:]:
            if token.startswith("-"):
                if token not in _ALLOWED_PYTEST_FLAGS:
                    raise EvidenceRunError(
                        f"pytest argument is not allowlisted: {token}"
                    )
                continue
            raw_path, separator, node_id = token.partition("::")
            if separator and not node_id:
                raise EvidenceRunError(f"pytest node selector is empty: {token}")
            if "\\" in raw_path or not raw_path.endswith(".py"):
                raise EvidenceRunError(f"pytest selector is not a test module: {token}")
            if not raw_path.startswith(_ALLOWED_TEST_ROOTS):
                raise EvidenceRunError(
                    f"pytest selector is outside test roots: {token}"
                )
            candidate = _safe_relative(root, raw_path)
            if candidate is None or not candidate.is_file():
                raise EvidenceRunError(f"pytest selector is unavailable: {token}")
            selectors += 1
        if selectors == 0:
            raise EvidenceRunError("pytest command requires an explicit test selector")
        return
    if argv[0] == "scripts/platform":
        executable = root / argv[0]
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise EvidenceRunError("allowlisted platform executable is unavailable")
        if len(argv) != 2 or argv[1] not in _ALLOWED_PLATFORM_SUBCOMMANDS:
            raise EvidenceRunError(
                "platform command is not an evidence-safe subcommand"
            )
        return
    raise EvidenceRunError(f"command executable is not allowlisted: {argv[0]}")


def _subject_rows(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in ("criteria", "blockers"):
        raw_rows = manifest.get(section)
        if not isinstance(raw_rows, list):
            raise EvidenceRunError(f"manifest {section} must be a list")
        for row in raw_rows:
            if not isinstance(row, dict):
                raise EvidenceRunError(f"manifest {section} contains a non-object")
            rows.append(row)
    return rows


def _rubric_hash_from_manifest(manifest: dict[str, Any]) -> str:
    """Return the rubric identity for either supported manifest generation."""

    if "canonical_source" in manifest:
        source = manifest.get("canonical_source")
        value = source.get("sha256") if isinstance(source, dict) else None
    else:
        source = manifest.get("rubric")
        value = source.get("source_sha256") if isinstance(source, dict) else None
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise EvidenceRunError("manifest rubric SHA-256 is invalid")
    return value


def _prepare_evidence_commands(
    *,
    manifest: dict[str, Any],
    repository_root: Path,
    evidence_root: Path,
) -> tuple[EvidenceCommand, ...]:
    _rubric_hash_from_manifest(manifest)
    receipt_paths: set[str] = set()
    prepared: list[EvidenceCommand] = []
    for row in _subject_rows(manifest):
        subject = row.get("id")
        evidence = row.get("evidence")
        if not isinstance(subject, str) or not (
            _ID_RE.fullmatch(subject) or _FULL_SCOPE_ID_RE.fullmatch(subject)
        ):
            raise EvidenceRunError(f"manifest subject ID is invalid: {subject!r}")
        if not isinstance(evidence, dict):
            raise EvidenceRunError(f"{subject}: evidence must be an object")
        minimum_level = evidence.get("minimum_level")
        if minimum_level not in {"E4", "E5"}:
            raise EvidenceRunError(f"{subject}: evidence level must be E4 or E5")

        path_hashes: dict[str, str] = {}
        raw_paths = evidence.get("paths")
        if not isinstance(raw_paths, list):
            raise EvidenceRunError(f"{subject}: evidence paths must be a list")
        for entry in raw_paths:
            if not isinstance(entry, dict):
                raise EvidenceRunError(f"{subject}: evidence path must be an object")
            raw_path = entry.get("path")
            expected_hash = entry.get("sha256")
            candidate = _safe_relative(repository_root, raw_path)
            if candidate is None or not candidate.is_file():
                raise EvidenceRunError(f"{subject}: evidence path is unavailable")
            if not isinstance(expected_hash, str) or not _HASH_RE.fullmatch(
                expected_hash
            ):
                raise EvidenceRunError(f"{subject}: evidence path hash is invalid")
            relative = candidate.relative_to(repository_root.resolve()).as_posix()
            if relative in _FORBIDDEN_SELF_EVIDENCE:
                raise EvidenceRunError(f"{subject}: self-attestation path is forbidden")
            if relative in path_hashes:
                raise EvidenceRunError(
                    f"{subject}: duplicate evidence path: {relative}"
                )
            if sha256_path(candidate) != expected_hash:
                raise EvidenceRunError(
                    f"{subject}: evidence path hash is stale: {relative}"
                )
            path_hashes[relative] = expected_hash

        raw_commands = evidence.get("commands")
        raw_receipts = evidence.get("receipts")
        if not isinstance(raw_commands, list) or not isinstance(raw_receipts, list):
            raise EvidenceRunError(f"{subject}: commands and receipts must be lists")
        receipt_map: dict[str, tuple[str, object]] = {}
        for receipt in raw_receipts:
            if not isinstance(receipt, dict):
                raise EvidenceRunError(f"{subject}: receipt template must be an object")
            command_id = receipt.get("command_id")
            raw_receipt_path = receipt.get("path")
            if not isinstance(command_id, str) or not command_id:
                raise EvidenceRunError(f"{subject}: receipt command ID is invalid")
            if command_id in receipt_map:
                raise EvidenceRunError(f"{subject}: duplicate receipt command ID")
            receipt_candidate = _safe_relative(evidence_root, raw_receipt_path)
            if receipt_candidate is None:
                raise EvidenceRunError(f"{subject}: receipt path is unsafe")
            relative_receipt = receipt_candidate.relative_to(evidence_root).as_posix()
            if relative_receipt in receipt_paths:
                raise EvidenceRunError(
                    f"{subject}: receipt path is shared by multiple subjects"
                )
            if receipt.get("sha256") is not None:
                raise EvidenceRunError(
                    f"{subject}: input template receipt SHA-256 must remain null"
                )
            receipt_paths.add(relative_receipt)
            receipt_map[command_id] = (relative_receipt, receipt.get("sha256"))

        command_ids: set[str] = set()
        for command in raw_commands:
            if not isinstance(command, dict):
                raise EvidenceRunError(f"{subject}: command must be an object")
            command_id = command.get("id")
            argv_list = _string_list(command.get("argv"))
            if not isinstance(command_id, str) or not command_id:
                raise EvidenceRunError(f"{subject}: command ID is invalid")
            if command_id in command_ids:
                raise EvidenceRunError(f"{subject}: duplicate command ID: {command_id}")
            if argv_list is None:
                raise EvidenceRunError(f"{subject}: command argv is invalid")
            if command_id not in receipt_map:
                raise EvidenceRunError(f"{subject}: command receipt is missing")
            argv = tuple(argv_list)
            validate_evidence_argv(argv, repository_root=repository_root)
            prepared.append(
                EvidenceCommand(
                    subject=subject,
                    command_id=command_id,
                    argv=argv,
                    minimum_level=minimum_level,
                    receipt_path=receipt_map[command_id][0],
                    path_hashes=tuple(sorted(path_hashes.items())),
                )
            )
            command_ids.add(command_id)
        if set(receipt_map) != command_ids:
            raise EvidenceRunError(f"{subject}: receipt has no matching command")
    return tuple(prepared)


def _git_bytes(repository_root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repository_root),
        shell=False,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise EvidenceRunError(f"git provenance command failed: {detail}")
    return completed.stdout


def _repository_provenance(repository_root: Path) -> RepositoryProvenance:
    commit = _git_bytes(repository_root, "rev-parse", "HEAD").decode().strip()
    if not _COMMIT_RE.fullmatch(commit):
        raise EvidenceRunError("repository commit is unavailable")
    raw_paths = _git_bytes(
        repository_root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    deleted_paths = set(
        item
        for item in _git_bytes(
            repository_root,
            "ls-files",
            "--deleted",
            "-z",
        ).split(b"\0")
        if item
    )
    digest = hashlib.sha256()
    for raw_path in sorted(item for item in raw_paths.split(b"\0") if item):
        try:
            relative = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceRunError("repository path is not UTF-8") from exc
        candidate = _safe_relative(repository_root, relative)
        if candidate is None:
            raise EvidenceRunError(f"repository evidence path is unsafe: {relative}")
        digest.update(raw_path)
        digest.update(b"\0")
        if candidate.is_file():
            digest.update(bytes.fromhex(sha256_path(candidate)))
        else:
            lexical_candidate = repository_root / relative
            if (
                raw_path not in deleted_paths
                or lexical_candidate.exists()
                or lexical_candidate.is_symlink()
            ):
                raise EvidenceRunError(
                    f"repository evidence path is unsafe: {relative}"
                )
            digest.update(_REPOSITORY_TRACKED_DELETION_SENTINEL)
        digest.update(b"\0")
    return RepositoryProvenance(commit=commit, dirty_diff_sha256=digest.hexdigest())


def _is_secret_environment_key(name: str) -> bool:
    upper = name.upper()
    return upper.endswith(("_KEY", "_URL")) or any(
        marker in upper for marker in _SECRET_ENVIRONMENT_MARKERS
    )


def _execution_environment(evidence_root: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in _DANGEROUS_ENVIRONMENT_KEYS
    }
    environment.update(_DETERMINISTIC_ENVIRONMENT)
    temporary_root = evidence_root / "tmp"
    temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment["TMPDIR"] = str(temporary_root)
    environment["PYTHONPYCACHEPREFIX"] = str(temporary_root / "pycache")
    return environment


def _redacted_environment(environment: dict[str, str]) -> dict[str, str]:
    return {
        key: "<redacted>" if _is_secret_environment_key(key) else value
        for key, value in sorted(environment.items())
    }


def _as_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return value.encode("utf-8", errors="replace")


def _redact_log(payload: bytes, environment: dict[str, str]) -> bytes:
    text_payload = payload.decode("utf-8", errors="replace")
    secrets = sorted(
        {
            value
            for key, value in environment.items()
            if _is_secret_environment_key(key) and len(value) >= 4
        },
        key=len,
        reverse=True,
    )
    for secret in secrets:
        text_payload = text_payload.replace(secret, "<redacted>")
    return text_payload.encode("utf-8")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _command_group_id(argv: tuple[str, ...]) -> str:
    return sha256_bytes(_json_bytes({"argv": list(argv)}))[:24]


def _expects_pytest_evidence(argv: tuple[str, ...]) -> bool:
    if argv[0] == ".venv/bin/pytest":
        return True
    return (
        argv[0] == "scripts/platform" and len(argv) == 2 and argv[1].startswith("test-")
    )


def _disqualifying_test_outcomes(
    argv: tuple[str, ...], stdout: bytes, stderr: bytes
) -> tuple[str, ...]:
    if not _expects_pytest_evidence(argv):
        return ()
    payload = (_as_bytes(stdout) + b"\n" + _as_bytes(stderr)).decode(
        "utf-8", errors="replace"
    )
    outcomes: set[str] = set()
    for label in ("skipped", "xfailed", "xpassed", "deselected"):
        if re.search(rf"\b[1-9][0-9]*\s+{label}\b", payload, re.IGNORECASE):
            outcomes.add(f"pytest_{label}")
    if _PYTEST_ZERO_RE.search(payload):
        outcomes.add("pytest_no_tests")
    if not _PYTEST_PASS_RE.search(payload):
        outcomes.add("pytest_pass_count_missing")
    return tuple(sorted(outcomes))


def _execute_evidence_command(
    *,
    argv: tuple[str, ...],
    repository_root: Path,
    evidence_root: Path,
    environment: dict[str, str],
    timeout_seconds: float,
) -> CommandExecution:
    group_id = _command_group_id(argv)
    started_at = _utc_now()
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(repository_root),
            env=environment,
            shell=False,
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
        exit_code = completed.returncode
        stdout = _as_bytes(completed.stdout)
        stderr = _as_bytes(completed.stderr)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = _as_bytes(exc.stdout)
        stderr = _as_bytes(exc.stderr) + b"\nevidence command timed out\n"
    except OSError as exc:
        exit_code = 126
        stdout = b""
        stderr = f"evidence command launch failed: {exc}\n".encode("utf-8")
    duration = time.monotonic() - started
    finished_at = _utc_now()
    stdout_payload = _redact_log(stdout, environment)
    stderr_payload = _redact_log(stderr, environment)
    disqualifying_outcomes = _disqualifying_test_outcomes(
        argv, stdout_payload, stderr_payload
    )
    stdout_relative = f"logs/{group_id}.stdout.log"
    stderr_relative = f"logs/{group_id}.stderr.log"
    _atomic_write_evidence(evidence_root / stdout_relative, stdout_payload)
    _atomic_write_evidence(evidence_root / stderr_relative, stderr_payload)
    return CommandExecution(
        group_id=group_id,
        argv=argv,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration,
        exit_code=exit_code,
        timed_out=timed_out,
        evidence_eligible=exit_code == 0 and not disqualifying_outcomes,
        disqualifying_outcomes=disqualifying_outcomes,
        stdout_path=stdout_relative,
        stdout_sha256=sha256_bytes(stdout_payload),
        stderr_path=stderr_relative,
        stderr_sha256=sha256_bytes(stderr_payload),
    )


def _resolved_manifest_payload(
    *,
    manifest: dict[str, Any],
    receipt_hashes: dict[tuple[str, str], str],
) -> dict[str, Any]:
    resolved = copy.deepcopy(manifest)
    for row in _subject_rows(resolved):
        subject = row["id"]
        evidence = row["evidence"]
        for receipt in evidence["receipts"]:
            key = (subject, receipt["command_id"])
            receipt["sha256"] = receipt_hashes[key]
    return resolved


def _render_ledger_markdown(ledger: dict[str, Any]) -> str:
    status = "COMPLETE" if ledger["complete"] else "INCOMPLETE"
    lines = [
        "# Platform completeness validation ledger",
        "",
        "<!-- Generated by the allowlisted evidence runner; do not edit. -->",
        "",
        f"- Status: **{status}**",
        f"- Resolved manifest SHA-256: `{ledger['resolved_manifest_sha256']}`",
        f"- Repository commit: `{ledger['repository']['commit']}`",
        (
            "- Receipt-verified criteria: "
            f"**{ledger['evaluation']['verified_criteria']}/"
            f"{ledger['evaluation']['expected_criteria']}**"
        ),
        f"- Unique command executions: **{len(ledger['command_groups'])}**",
        "",
        "## Command groups",
        "",
        "| Group | Exit | Eligible | Timeout | Subjects | stdout | stderr |",
        "| --- | ---: | --- | --- | --- | --- | --- |",
    ]
    for group in ledger["command_groups"]:
        subjects = ", ".join(item["subject"] for item in group["bindings"])
        lines.append(
            f"| `{group['group_id']}` | {group['exit_code']} | "
            f"{'yes' if group['evidence_eligible'] else 'no'} | "
            f"{'yes' if group['timed_out'] else 'no'} | {subjects} | "
            f"`{group['stdout_sha256']}` | `{group['stderr_sha256']}` |"
        )
    lines.extend(
        [
            "",
            "## Criterion decisions",
            "",
            "| Criterion | Evidence | Gate | Reasons |",
            "| --- | --- | --- | --- |",
        ]
    )
    for criterion in ledger["evaluation"]["criteria"]:
        reasons = ", ".join(criterion["finding_codes"]) or "none"
        lines.append(
            f"| {criterion['criterion_id']} | {criterion['evidence_level']} | "
            f"{'PASS' if criterion['complete'] else 'FAIL'} | {reasons} |"
        )
    lines.extend(["", "## Findings", ""])
    findings = [*ledger["runner_findings"], *ledger["evaluation"]["findings"]]
    if findings:
        lines.extend(
            f"- `{item['subject']}` `{item['code']}` — {item['detail']}"
            for item in findings
        )
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def run_manifest_evidence(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    repository_root: Path = PROJECT_ROOT,
    evidence_root: Path,
    timeout_seconds: float = 1800.0,
) -> EvidenceRun:
    """Execute preflighted evidence commands and emit an immutable run bundle."""

    if not 1.0 <= timeout_seconds <= 21600.0:
        raise EvidenceRunError("timeout must be between 1 and 21600 seconds")
    repository = repository_root.resolve(strict=True)
    output_root = _validated_evidence_root(
        evidence_root=evidence_root,
        repository_root=repository,
    )
    manifest = load_json(manifest_path)
    source_manifest_hash = sha256_path(manifest_path)
    rubric_hash = _rubric_hash_from_manifest(manifest)
    commands = _prepare_evidence_commands(
        manifest=manifest,
        repository_root=repository,
        evidence_root=output_root,
    )
    if not commands:
        raise EvidenceRunError("manifest contains no executable evidence commands")

    before = _repository_provenance(repository)
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_root, 0o700)
    environment = _execution_environment(output_root)
    redacted_environment = _redacted_environment(environment)
    environment_hash = sha256_bytes(_json_bytes(redacted_environment))

    grouped: dict[tuple[str, ...], list[EvidenceCommand]] = {}
    for command in commands:
        grouped.setdefault(command.argv, []).append(command)
    executions: dict[tuple[str, ...], CommandExecution] = {}
    for argv in grouped:
        executions[argv] = _execute_evidence_command(
            argv=argv,
            repository_root=repository,
            evidence_root=output_root,
            environment=environment,
            timeout_seconds=timeout_seconds,
        )

    runner_findings: list[Finding] = []
    for execution in executions.values():
        if execution.disqualifying_outcomes:
            runner_findings.append(
                Finding(
                    execution.group_id,
                    "test_output_disqualified",
                    ", ".join(execution.disqualifying_outcomes),
                )
            )
    after = _repository_provenance(repository)
    if after != before:
        runner_findings.append(
            Finding(
                "runner",
                "repository_state_changed",
                "repository content changed while evidence commands executed",
            )
        )

    receipt_hashes: dict[tuple[str, str], str] = {}
    receipt_hashes_by_path: dict[str, str] = {}
    for command in commands:
        execution = executions[command.argv]
        current_path_hashes: dict[str, str] = {}
        for relative, expected_hash in command.path_hashes:
            actual_hash = sha256_path(repository / relative)
            current_path_hashes[relative] = actual_hash
            if actual_hash != expected_hash:
                runner_findings.append(
                    Finding(command.subject, "evidence_changed_during_run", relative)
                )
        receipt = {
            "schema_version": 1,
            "criterion_id": command.subject,
            "rubric_sha256": rubric_hash,
            "command_id": command.command_id,
            "argv": list(command.argv),
            "exit_code": execution.exit_code,
            "kind": "repository_verification",
            "evidence_level": "E4" if execution.evidence_eligible else "E0",
            "evidence_eligible": execution.evidence_eligible,
            "disqualifying_outcomes": list(execution.disqualifying_outcomes),
            "started_at": execution.started_at,
            "finished_at": execution.finished_at,
            "duration_seconds": execution.duration_seconds,
            "timed_out": execution.timed_out,
            "repository_commit": before.commit,
            "dirty_diff_sha256": before.dirty_diff_sha256,
            "source_manifest_sha256": source_manifest_hash,
            "command_group_id": execution.group_id,
            "stdout_path": execution.stdout_path,
            "stdout_sha256": execution.stdout_sha256,
            "stderr_path": execution.stderr_path,
            "stderr_sha256": execution.stderr_sha256,
            "redacted_environment_sha256": environment_hash,
            "secret_environment_keys_redacted": sorted(
                key for key in environment if _is_secret_environment_key(key)
            ),
            "path_hashes": current_path_hashes,
        }
        receipt_payload = _json_bytes(receipt)
        receipt_path = output_root / command.receipt_path
        _atomic_write_evidence(receipt_path, receipt_payload)
        receipt_hash = sha256_bytes(receipt_payload)
        receipt_hashes[(command.subject, command.command_id)] = receipt_hash
        receipt_hashes_by_path[command.receipt_path] = receipt_hash

    resolved_payload = _resolved_manifest_payload(
        manifest=manifest,
        receipt_hashes=receipt_hashes,
    )
    resolved_path = output_root / "resolved-manifest.json"
    _atomic_write_evidence(resolved_path, _json_bytes(resolved_payload))
    evaluation = evaluate_manifest(
        resolved_path,
        repository_root=repository,
        evidence_root=output_root,
    )

    command_groups: list[dict[str, Any]] = []
    for argv, bindings in grouped.items():
        execution = executions[argv]
        command_groups.append(
            {
                "group_id": execution.group_id,
                "argv": list(argv),
                "bindings": [
                    {"subject": item.subject, "command_id": item.command_id}
                    for item in bindings
                ],
                "started_at": execution.started_at,
                "finished_at": execution.finished_at,
                "duration_seconds": execution.duration_seconds,
                "exit_code": execution.exit_code,
                "timed_out": execution.timed_out,
                "evidence_eligible": execution.evidence_eligible,
                "disqualifying_outcomes": list(execution.disqualifying_outcomes),
                "stdout_path": execution.stdout_path,
                "stdout_sha256": execution.stdout_sha256,
                "stderr_path": execution.stderr_path,
                "stderr_sha256": execution.stderr_sha256,
            }
        )
    ledger: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "complete": evaluation.complete and not runner_findings,
        "source_manifest_sha256": source_manifest_hash,
        "resolved_manifest_path": resolved_path.name,
        "resolved_manifest_sha256": sha256_path(resolved_path),
        "repository": {
            "root": str(repository),
            "commit": before.commit,
            "dirty_diff_sha256": before.dirty_diff_sha256,
        },
        "execution_policy": {
            "cwd": str(repository),
            "shell": False,
            "timeout_seconds": timeout_seconds,
            "command_count": len(commands),
            "unique_command_count": len(grouped),
            "maximum_issued_evidence_level": "E4",
            "removed_environment_keys": sorted(_DANGEROUS_ENVIRONMENT_KEYS),
            "environment": redacted_environment,
            "redacted_environment_sha256": environment_hash,
        },
        "receipt_sha256": dict(sorted(receipt_hashes_by_path.items())),
        "command_groups": command_groups,
        "runner_findings": [
            {"subject": item.subject, "code": item.code, "detail": item.detail}
            for item in sorted(set(runner_findings))
        ],
        "evaluation": {
            "complete": evaluation.complete,
            "declared_score": evaluation.declared_score,
            "verified_criteria": evaluation.verified_criteria,
            "expected_criteria": evaluation.expected_criteria,
            "criteria": [
                {
                    "criterion_id": result.criterion_id,
                    "area_id": result.area_id,
                    "score": result.score,
                    "evidence_level": result.evidence_level,
                    "complete": result.complete,
                    "finding_codes": list(result.finding_codes),
                }
                for result in evaluation.criteria
            ],
            "findings": [
                {
                    "subject": item.subject,
                    "code": item.code,
                    "detail": item.detail,
                }
                for item in evaluation.findings
            ],
        },
    }
    ledger_json = output_root / "validation-ledger.json"
    ledger_markdown = output_root / "validation-ledger.md"
    _atomic_write_evidence(ledger_json, _json_bytes(ledger))
    _atomic_write_evidence(
        ledger_markdown,
        _render_ledger_markdown(ledger).encode("utf-8"),
    )
    return EvidenceRun(
        evidence_root=output_root,
        resolved_manifest=resolved_path,
        ledger_json=ledger_json,
        ledger_markdown=ledger_markdown,
        command_count=len(commands),
        execution_count=len(grouped),
        evaluation=evaluation,
        runner_findings=tuple(sorted(set(runner_findings))),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed unless every completeness criterion is receipt-verified."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repository-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--check-report", action="store_true")
    parser.add_argument(
        "--run-evidence",
        action="store_true",
        help="execute only allowlisted manifest commands into an external evidence root",
    )
    parser.add_argument("--timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    evidence_root = args.evidence_root
    if evidence_root is None:
        configured = os.environ.get("MARKET_RESEARCH_COMPLETENESS_EVIDENCE_ROOT")
        evidence_root = Path(configured) if configured else None
    if args.run_evidence:
        if evidence_root is None:
            print(
                "evidence runner requires --evidence-root or "
                "MARKET_RESEARCH_COMPLETENESS_EVIDENCE_ROOT",
                file=sys.stderr,
            )
            return 2
        if args.write_report or args.check_report:
            print(
                "evidence runner cannot write or check the checked-in status report",
                file=sys.stderr,
            )
            return 2
        try:
            evidence_run = run_manifest_evidence(
                args.manifest,
                repository_root=args.repository_root,
                evidence_root=evidence_root,
                timeout_seconds=args.timeout_seconds,
            )
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            DuplicateKeyError,
        ) as exc:
            print(
                f"platform completeness evidence run rejected: {exc}", file=sys.stderr
            )
            return 2
        if not args.quiet:
            print(
                "platform completeness evidence: "
                f"{'COMPLETE' if evidence_run.complete else 'INCOMPLETE'}; "
                f"commands={evidence_run.command_count}; "
                f"executions={evidence_run.execution_count}; "
                f"root={evidence_run.evidence_root}"
            )
        return 0 if evidence_run.complete else 1
    try:
        evaluation = evaluate_manifest(
            args.manifest,
            repository_root=args.repository_root,
            evidence_root=evidence_root,
        )
    except (OSError, ValueError, json.JSONDecodeError, DuplicateKeyError) as exc:
        print(f"platform completeness manifest invalid: {exc}", file=sys.stderr)
        return 2
    report = render_report(evaluation)
    if args.write_report:
        _atomic_write(args.report, report)
    if args.check_report:
        try:
            current = args.report.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"generated report missing: {exc}", file=sys.stderr)
            return 2
        if current != report:
            print(
                "generated completeness report is stale; rerun with --write-report",
                file=sys.stderr,
            )
            return 2
    if not args.quiet:
        print(
            f"platform completeness: {'COMPLETE' if evaluation.complete else 'INCOMPLETE'}; "
            f"declared={evaluation.declared_score:.2f}/100; "
            f"verified={evaluation.verified_criteria}/{evaluation.expected_criteria}; "
            f"findings={len(evaluation.findings)}"
        )
        for finding in evaluation.findings:
            print(finding.render())
    return 0 if evaluation.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
