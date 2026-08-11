#!/usr/bin/env python3
"""Create and validate local self-attested run receipts for the A--J audit.

The receipt is evidence, not configuration.  It is written only after this
tool directly runs the exact pytest file targets required by the matrix,
observes zero failures/errors/skips, and confirms that the audited source
surface did not change during the run.  The resulting unkeyed document is not
an authenticated CI or independent-party attestation; validation always makes
that trust limitation explicit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from xml.etree import ElementTree

try:
    from tools.reference_audit_surface import audit_surface
except ModuleNotFoundError:  # direct ``python tools/...`` execution
    from reference_audit_surface import audit_surface  # type: ignore[import-not-found,no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = PROJECT_ROOT / "docs/investment-research-platform-audit.json"
DEFAULT_RECEIPT = (
    PROJECT_ROOT / "docs/investment-research-platform-audit-execution-receipt.json"
)
RECEIPT_SCHEMA_VERSION = 1


def _absolute_executable(executable: str) -> str:
    """Return an absolute launcher path without dereferencing a venv symlink."""

    return os.path.abspath(executable)


def _normalized_python_executable(
    executable: str,
    *,
    prefix: str | None = None,
    base_prefix: str | None = None,
) -> str:
    """Normalize Python aliases only inside the active virtual environment.

    Virtual environments normally expose ``python``, ``python3``, and a
    versioned launcher as aliases for the same environment.  Bind receipts to
    the stable launcher inside that environment without resolving its symlink
    to the host interpreter.  Launchers outside the active environment retain
    their exact absolute path.
    """

    absolute = _absolute_executable(executable)
    environment_prefix = _absolute_executable(prefix or sys.prefix)
    host_prefix = _absolute_executable(base_prefix or sys.base_prefix)
    if os.path.normcase(environment_prefix) == os.path.normcase(host_prefix):
        return absolute

    launcher_directory_name = "Scripts" if os.name == "nt" else "bin"
    launcher_directory = os.path.join(environment_prefix, launcher_directory_name)
    if os.path.normcase(os.path.dirname(absolute)) != os.path.normcase(
        launcher_directory
    ):
        return absolute

    suffix = ".exe" if os.name == "nt" else ""
    aliases = {
        f"python{suffix}",
        f"python{sys.version_info.major}{suffix}",
        f"python{sys.version_info.major}.{sys.version_info.minor}{suffix}",
    }
    launcher_name = os.path.basename(absolute)
    if os.name == "nt":
        launcher_name = launcher_name.lower()
    if launcher_name not in aliases:
        return absolute

    canonical_name = "python.exe" if os.name == "nt" else "python"
    canonical = _absolute_executable(os.path.join(launcher_directory, canonical_name))
    if not os.path.isfile(canonical) or not os.access(canonical, os.X_OK):
        return absolute
    return canonical


PYTHON_EXECUTABLE = _normalized_python_executable(sys.executable)
RUBRIC_SHA256 = "ce507e16b37a8915ba34f12907aac3145dd512859951d391781e5a390fb675a5"
INSTRUCTION_SHA256 = "26871e2de2deb4a86b8bee87bdbb30b731eb19e82e61ee0a64bbf0c2cebfc8de"
_DETERMINISTIC_ENVIRONMENT = {
    "PYTHONHASHSEED": "0",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "BLIS_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "DJANGO_SETTINGS_MODULE": "market_research_web.settings_test",
}


def _receipt_temp_parent() -> str:
    """Use the short POSIX temp namespace required by forkserver sockets."""

    posix_tmp = Path("/tmp")
    if os.name == "posix" and posix_tmp.is_dir():
        return str(posix_tmp)
    return tempfile.gettempdir()


class DuplicateReceiptKeyError(ValueError):
    """Raised when a receipt contains ambiguous duplicate JSON keys."""


@dataclass(frozen=True, slots=True)
class ReceiptValidation:
    status: str
    findings: tuple[str, ...]
    content_sha256: str | None
    required_target_count: int
    tests_passed: int | None

    @property
    def clean_local_run(self) -> bool:
        return self.status == "VALID_LOCAL_SELF_ATTESTED"

    @property
    def trusted(self) -> bool:
        """A repository-owned unkeyed receipt never authenticates its author."""

        return False

    @property
    def trust_level(self) -> str:
        return "LOCAL_SELF_ATTESTED" if self.clean_local_run else "NONE"

    def summary(self, *, relative_path: str) -> dict[str, object]:
        return {
            "path": relative_path,
            "status": self.status,
            "clean_local_run": self.clean_local_run,
            "trusted": self.trusted,
            "trust_level": self.trust_level,
            "content_sha256": self.content_sha256,
            "required_target_count": self.required_target_count,
            "tests_passed": self.tests_passed,
            "findings": list(self.findings),
        }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateReceiptKeyError(f"duplicate_receipt_json_key:{key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"nonfinite_receipt_json_constant:{value}")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def required_test_hashes_from_matrix(
    matrix: Mapping[str, Any],
    *,
    root: Path,
) -> dict[str, str]:
    """Return the exact, repository-owned pytest files needed by the audit."""

    targets: set[str] = set()
    criteria = matrix.get("criteria")
    gates = matrix.get("fatal_gates")
    if not isinstance(criteria, list) or not isinstance(gates, list):
        raise ValueError("receipt_matrix_evidence_inventory_missing")
    for criterion in criteria:
        if not isinstance(criterion, dict):
            raise ValueError("receipt_matrix_criterion_invalid")
        evidence = criterion.get("objective_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("receipt_matrix_criterion_evidence_missing")
        for item in evidence:
            if not isinstance(item, dict) or not isinstance(item.get("test"), str):
                raise ValueError("receipt_matrix_criterion_test_invalid")
            targets.add(item["test"])
    for gate in gates:
        if not isinstance(gate, dict):
            raise ValueError("receipt_matrix_gate_invalid")
        command = gate.get("verification_method")
        if not isinstance(command, str):
            raise ValueError("receipt_matrix_gate_command_invalid")
        target = command.rsplit(" ", 1)[-1]
        targets.add(target)

    result: dict[str, str] = {}
    resolved_root = root.resolve()
    for target in sorted(targets):
        relative = Path(target)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".py":
            raise ValueError(f"receipt_test_target_invalid:{target}")
        path = (resolved_root / relative).resolve()
        try:
            path.relative_to(resolved_root)
        except ValueError as error:
            raise ValueError(f"receipt_test_target_outside_root:{target}") from error
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"receipt_test_target_missing:{target}")
        result[target] = _sha256(path.read_bytes())
    if not result:
        raise ValueError("receipt_test_target_inventory_empty")
    return result


def _target_records(required_tests: Mapping[str, str]) -> list[dict[str, str]]:
    return [
        {"path": path, "sha256": digest}
        for path, digest in sorted(required_tests.items())
    ]


def build_receipt_document(
    *,
    source_surface: Mapping[str, object],
    required_tests: Mapping[str, str],
    tests_passed: int,
    duration_seconds: float,
    output_sha256: str,
    created_at: str,
) -> dict[str, object]:
    """Build a hash-addressed receipt from an observed successful run."""

    if tests_passed < 1:
        raise ValueError("receipt_tests_passed_invalid")
    payload: dict[str, object] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "created_at": created_at,
        "canonical_source": {
            "rubric_sha256": RUBRIC_SHA256,
            "instruction_sha256": INSTRUCTION_SHA256,
        },
        "source_surface": dict(source_surface),
        "pytest": {
            "status": "PASSED",
            "exit_code": 0,
            "tests_passed": tests_passed,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "duration_seconds": round(duration_seconds, 6),
            "output_sha256": output_sha256,
            "command": [
                PYTHON_EXECUTABLE,
                "-m",
                "pytest",
                "-q",
                *sorted(required_tests),
            ],
            "targets": _target_records(required_tests),
        },
    }
    return {
        "payload": payload,
        "content_sha256": _sha256(_canonical(payload)),
    }


def validate_receipt(
    path: Path,
    *,
    source_surface: Mapping[str, object],
    required_tests: Mapping[str, str],
) -> ReceiptValidation:
    """Validate a receipt against the current source and exact test bytes."""

    required_target_count = len(required_tests)
    if not path.is_file():
        return ReceiptValidation(
            status="MISSING",
            findings=("execution_receipt_missing",),
            content_sha256=None,
            required_target_count=required_target_count,
            tests_passed=None,
        )
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        return ReceiptValidation(
            status="INVALID",
            findings=(f"execution_receipt_json_invalid:{type(error).__name__}",),
            content_sha256=None,
            required_target_count=required_target_count,
            tests_passed=None,
        )
    findings: list[str] = []
    stale_findings = {
        "execution_receipt_canonical_source_mismatch",
        "execution_receipt_source_surface_mismatch",
        "execution_receipt_pytest_command_mismatch",
        "execution_receipt_test_targets_mismatch",
    }
    if not isinstance(document, dict) or set(document) != {
        "payload",
        "content_sha256",
    }:
        findings.append("execution_receipt_envelope_invalid")
        payload: object = {}
    else:
        payload = document["payload"]
        expected_hash = _sha256(_canonical(payload))
        if document.get("content_sha256") != expected_hash:
            findings.append("execution_receipt_content_hash_mismatch")
    if not isinstance(payload, dict):
        findings.append("execution_receipt_payload_invalid")
        payload = {}
    expected_payload_fields = {
        "schema_version",
        "created_at",
        "canonical_source",
        "source_surface",
        "pytest",
    }
    if set(payload) != expected_payload_fields:
        findings.append("execution_receipt_payload_fields_invalid")
    if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        findings.append("execution_receipt_schema_version_invalid")
    created_at = payload.get("created_at")
    if not isinstance(created_at, str):
        findings.append("execution_receipt_created_at_invalid")
    else:
        try:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            findings.append("execution_receipt_created_at_invalid")
        else:
            if parsed.tzinfo is None:
                findings.append("execution_receipt_created_at_invalid")
    if payload.get("canonical_source") != {
        "rubric_sha256": RUBRIC_SHA256,
        "instruction_sha256": INSTRUCTION_SHA256,
    }:
        findings.append("execution_receipt_canonical_source_mismatch")
    if payload.get("source_surface") != dict(source_surface):
        findings.append("execution_receipt_source_surface_mismatch")

    pytest_result = payload.get("pytest")
    tests_passed: int | None = None
    if not isinstance(pytest_result, dict) or set(pytest_result) != {
        "status",
        "exit_code",
        "tests_passed",
        "failures",
        "errors",
        "skipped",
        "duration_seconds",
        "output_sha256",
        "command",
        "targets",
    }:
        findings.append("execution_receipt_pytest_result_invalid")
    else:
        tests_passed_value = pytest_result.get("tests_passed")
        if isinstance(tests_passed_value, int) and not isinstance(
            tests_passed_value, bool
        ):
            tests_passed = tests_passed_value
        success_shape = (
            pytest_result.get("status") == "PASSED"
            and pytest_result.get("exit_code") == 0
            and isinstance(tests_passed, int)
            and tests_passed > 0
            and tests_passed >= required_target_count
            and pytest_result.get("failures") == 0
            and pytest_result.get("errors") == 0
            and pytest_result.get("skipped") == 0
        )
        if not success_shape:
            findings.append("execution_receipt_pytest_not_clean_pass")
        expected_command = [
            PYTHON_EXECUTABLE,
            "-m",
            "pytest",
            "-q",
            *sorted(required_tests),
        ]
        if pytest_result.get("command") != expected_command:
            findings.append("execution_receipt_pytest_command_mismatch")
        if pytest_result.get("targets") != _target_records(required_tests):
            findings.append("execution_receipt_test_targets_mismatch")
        output_hash = pytest_result.get("output_sha256")
        if (
            not isinstance(output_hash, str)
            or len(output_hash) != 64
            or set(output_hash) - set("0123456789abcdef")
        ):
            findings.append("execution_receipt_output_hash_invalid")
        duration = pytest_result.get("duration_seconds")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or duration < 0
        ):
            findings.append("execution_receipt_duration_invalid")

    status = "VALID_LOCAL_SELF_ATTESTED"
    if findings:
        status = "STALE" if set(findings).issubset(stale_findings) else "INVALID"
    else:
        findings.append("execution_authenticity_unverified")
    content_sha256 = (
        str(document.get("content_sha256"))
        if isinstance(document, dict)
        and isinstance(document.get("content_sha256"), str)
        else None
    )
    return ReceiptValidation(
        status=status,
        findings=tuple(sorted(set(findings))),
        content_sha256=content_sha256,
        required_target_count=required_target_count,
        tests_passed=tests_passed,
    )


def _junit_counts(path: Path) -> tuple[int, int, int, int]:
    root = ElementTree.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = list(root.iter("failure"))
    errors = list(root.iter("error"))
    skipped = list(root.iter("skipped"))
    return len(cases), len(failures), len(errors), len(skipped)


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_and_write_receipt(
    *,
    root: Path,
    matrix_path: Path,
    output_path: Path,
) -> ReceiptValidation:
    """Run the exact audit evidence tests and atomically publish a receipt."""

    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    if not isinstance(matrix, dict):
        raise ValueError("receipt_matrix_root_invalid")
    before = audit_surface(root)
    assessment = matrix.get("assessment")
    if (
        not isinstance(assessment, dict)
        or assessment.get("assessment_surface") != before
    ):
        raise ValueError("receipt_matrix_source_surface_stale")
    required_tests = required_test_hashes_from_matrix(matrix, root=root)
    with tempfile.TemporaryDirectory(
        prefix="mra-",
        dir=_receipt_temp_parent(),
    ) as temporary:
        temporary_root = Path(temporary)
        junit = temporary_root / "pytest.xml"
        environment = dict(os.environ)
        environment.update(_DETERMINISTIC_ENVIRONMENT)
        environment.update(
            {
                "TMPDIR": str(temporary_root),
                "TEMP": str(temporary_root),
                "TMP": str(temporary_root),
                "RESEARCH_DATA_ROOT": str(temporary_root / "datasets"),
                "RESEARCH_ARTIFACT_ROOT": str(temporary_root / "artifacts"),
                "RESEARCH_REPORT_ROOT": str(temporary_root / "reports"),
                "RESEARCH_CACHE_ROOT": str(temporary_root / "cache"),
                "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH": str(
                    temporary_root / "identity" / "experiment-identities.jsonl"
                ),
                "XDG_STATE_HOME": str(temporary_root / "xdg-state"),
            }
        )
        command = [
            PYTHON_EXECUTABLE,
            "-m",
            "pytest",
            "-q",
            f"--junitxml={junit}",
            *sorted(required_tests),
        ]
        started = datetime.now(UTC)
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=False,
            check=False,
        )
        duration = (datetime.now(UTC) - started).total_seconds()
        output = completed.stdout + b"\n--- STDERR ---\n" + completed.stderr
        if completed.returncode != 0:
            sys.stdout.buffer.write(output)
            raise RuntimeError(
                f"execution_receipt_pytest_failed:exit={completed.returncode}"
            )
        tests, failures, errors, skipped = _junit_counts(junit)
        if failures or errors or skipped or tests < len(required_tests):
            raise RuntimeError(
                "execution_receipt_pytest_not_clean_pass:"
                f"tests={tests}:failures={failures}:errors={errors}:skipped={skipped}"
            )
        after = audit_surface(root)
        if after != before:
            raise RuntimeError("execution_receipt_source_changed_during_run")
        document = build_receipt_document(
            source_surface=before,
            required_tests=required_tests,
            tests_passed=tests,
            duration_seconds=duration,
            output_sha256=_sha256(output),
            created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        _atomic_write(
            output_path,
            json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
        )
    return validate_receipt(
        output_path,
        source_surface=before,
        required_tests=required_tests,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--output", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the current receipt without executing pytest",
    )
    args = parser.parse_args(argv)
    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    required_tests = required_test_hashes_from_matrix(matrix, root=PROJECT_ROOT)
    if args.check:
        validation = validate_receipt(
            args.output,
            source_surface=audit_surface(PROJECT_ROOT),
            required_tests=required_tests,
        )
    else:
        validation = run_and_write_receipt(
            root=PROJECT_ROOT,
            matrix_path=args.matrix,
            output_path=args.output,
        )
    print(
        json.dumps(
            validation.summary(
                relative_path=args.output.resolve()
                .relative_to(PROJECT_ROOT.resolve())
                .as_posix()
                if args.output.resolve().is_relative_to(PROJECT_ROOT.resolve())
                else str(args.output.resolve())
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if validation.clean_local_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
