#!/usr/bin/env python3
"""Single fail-closed authority for the current reference-audit session.

The raw files are byte-for-byte copies of the two user attachments.  Audit
generators, receipt validators, and completeness gates import this module
instead of repeating hashes that can remain self-consistent after the user has
supplied a different instruction authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUTHORITY_ROOT = (
    PROJECT_ROOT / "docs" / "investment-research-platform-audit-authority"
)

RUBRIC_SHA256 = (
    "ce507e16b37a8915ba34f12907aac3145dd512859951d391781e5a390fb675a5"
)
INSTRUCTION_SHA256 = (
    "a367c42b2d13824e8a5933b7bd6369eea35903dccc8a66027c7e450b3eef4564"
)
RUBRIC_RELATIVE_PATH = Path(
    "docs/investment-research-platform-audit-authority/"
    f"{RUBRIC_SHA256}-rubric.txt"
)
INSTRUCTION_RELATIVE_PATH = Path(
    "docs/investment-research-platform-audit-authority/"
    f"{INSTRUCTION_SHA256}-instructions.txt"
)
RUBRIC_PATH = PROJECT_ROOT / RUBRIC_RELATIVE_PATH
INSTRUCTION_PATH = PROJECT_ROOT / INSTRUCTION_RELATIVE_PATH

# A session is an ordered pair of exact authorities, not merely a mutable task
# name.  This keeps the current ten-iteration budget separate from prior audits
# that happened to use the same rubric with a different execution instruction.
AUDIT_SESSION_ID = f"{RUBRIC_SHA256[:16]}-{INSTRUCTION_SHA256[:16]}"
AUDIT_SESSION_HISTORY_RELATIVE_ROOT = Path(
    "docs/investment-research-platform-audit-history"
) / AUDIT_SESSION_ID
AUDIT_SESSION_HISTORY_ROOT = PROJECT_ROOT / AUDIT_SESSION_HISTORY_RELATIVE_ROOT


class AuditAuthorityError(ValueError):
    """The repository no longer contains the exact attached authorities."""


@dataclass(frozen=True, slots=True)
class AuditAuthority:
    rubric_sha256: str
    instruction_sha256: str
    rubric_path: str
    instruction_path: str
    session_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "rubric_sha256": self.rubric_sha256,
            "instruction_sha256": self.instruction_sha256,
            "rubric_path": self.rubric_path,
            "instruction_path": self.instruction_path,
            "session_id": self.session_id,
        }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_audit_authority(*, project_root: Path = PROJECT_ROOT) -> AuditAuthority:
    """Validate and return the exact raw attachment authority.

    Paths are resolved from ``project_root`` so tests can copy a repository and
    prove that renamed, missing, or tampered authority bytes fail closed.
    """

    rubric_path = project_root / RUBRIC_RELATIVE_PATH
    instruction_path = project_root / INSTRUCTION_RELATIVE_PATH
    for label, path, expected_hash in (
        ("rubric", rubric_path, RUBRIC_SHA256),
        ("instruction", instruction_path, INSTRUCTION_SHA256),
    ):
        if path.is_symlink() or not path.is_file():
            raise AuditAuthorityError(f"audit_{label}_authority_path_invalid")
        try:
            actual_hash = _sha256(path)
        except OSError as exc:
            raise AuditAuthorityError(
                f"audit_{label}_authority_unreadable"
            ) from exc
        if actual_hash != expected_hash:
            raise AuditAuthorityError(
                f"audit_{label}_authority_hash_mismatch"
            )
    return AuditAuthority(
        rubric_sha256=RUBRIC_SHA256,
        instruction_sha256=INSTRUCTION_SHA256,
        rubric_path=RUBRIC_RELATIVE_PATH.as_posix(),
        instruction_path=INSTRUCTION_RELATIVE_PATH.as_posix(),
        session_id=AUDIT_SESSION_ID,
    )


__all__ = [
    "AUDIT_SESSION_HISTORY_RELATIVE_ROOT",
    "AUDIT_SESSION_HISTORY_ROOT",
    "AUDIT_SESSION_ID",
    "AuditAuthority",
    "AuditAuthorityError",
    "INSTRUCTION_PATH",
    "INSTRUCTION_RELATIVE_PATH",
    "INSTRUCTION_SHA256",
    "PROJECT_ROOT",
    "RUBRIC_PATH",
    "RUBRIC_RELATIVE_PATH",
    "RUBRIC_SHA256",
    "validate_audit_authority",
]
