from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.validate_multi_asset_audit_matrix import (
    INSTRUCTION_ATTACHMENT_SHA256,
    INSTRUCTION_COPY,
    INSTRUCTION_COPY_SHA256,
    MatrixValidationError,
    RUBRIC_COPY,
    RUBRIC_NORMALIZED_SHA256,
    validate_matrix,
)


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/multi-asset-investment-research-audit-matrix.json"
VALIDATOR = ROOT / "tools/validate_multi_asset_audit_matrix.py"


def test_frozen_multi_asset_matrix_is_complete_and_source_bound() -> None:
    result = validate_matrix(ROOT, MATRIX)

    assert result["valid"] is True
    assert result["counts"] == {
        "areas": 14,
        "atomic_criteria": 140,
        "critical_fail_gates": 8,
        "end_to_end_scenarios": 5,
    }
    assert result["source_hashes"] == {
        "rubric_sha256": RUBRIC_NORMALIZED_SHA256,
        "instructions_sha256": INSTRUCTION_COPY_SHA256,
    }
    assert result["initial_assessment"]["triggered_critical_fail_gates"] == [
        "CF-01",
        "CF-04",
        "CF-05",
    ]
    assert result["initial_assessment"]["verdict"] == "CRITICAL FAIL — 완전 충족 아님"


def test_canonical_copies_are_lf_normalized_without_content_drift() -> None:
    rubric = (ROOT / RUBRIC_COPY).read_bytes()
    instructions = (ROOT / INSTRUCTION_COPY).read_bytes()

    assert b"\r" not in rubric
    assert b"\r" not in instructions
    assert hashlib.sha256(rubric).hexdigest() == RUBRIC_NORMALIZED_SHA256
    assert hashlib.sha256(instructions).hexdigest() == INSTRUCTION_COPY_SHA256
    assert instructions.endswith(b"\n")
    assert (
        hashlib.sha256(instructions[:-1]).hexdigest() == INSTRUCTION_ATTACHMENT_SHA256
    )


def test_validator_rejects_a_missing_atomic_criterion(tmp_path: Path) -> None:
    payload = json.loads(MATRIX.read_text(encoding="utf-8"))
    payload["criteria"].pop()
    invalid = tmp_path / "invalid-matrix.json"
    invalid.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(MatrixValidationError, match="criterion_ids_or_order_mismatch"):
        validate_matrix(ROOT, invalid)


def test_validator_cli_emits_machine_readable_result() -> None:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, str(VALIDATOR), "--root", str(ROOT), "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["valid"] is True
    assert result["counts"]["atomic_criteria"] == 140
