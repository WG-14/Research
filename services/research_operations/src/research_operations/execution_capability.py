"""Private issuer for one-shot core execution capability contexts.

Only Operations code imports the core issuer.  Web adapters can verify a
capability but cannot create one, and no environment string is accepted as an
authorization token.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import pwd
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from market_research.application.cli_execution import (
    ADMITTED_CLI_EXECUTION_SCOPE,
    RESEARCH_JOB_DISPATCH_SCOPE,
    OperatedAdmissionBinding,
    OperatedExecutionDenied,
    _issue_operated_execution_capability,
    is_operated_runtime,
)

from .admission import ACTIVE, AdmissionDecision

_ISSUABLE_OPERATIONS_SCOPES = frozenset(
    {ADMITTED_CLI_EXECUTION_SCOPE, RESEARCH_JOB_DISPATCH_SCOPE}
)
_HMAC_SHA256 = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_SYSTEMD_CREDENTIAL_NAME = "operated-execution.key"
_SYSTEMD_CREDENTIAL_ROOT = Path("/run/credentials")
_OPERATIONS_SERVICE_USER = "research-ops"


def _create_operated_execution_proof(
    scope: str,
    binding: OperatedAdmissionBinding,
) -> str:
    """Create an HMAC proof only inside the credential-bearing worker."""

    if scope not in _ISSUABLE_OPERATIONS_SCOPES:
        raise ValueError("operated_execution_capability_scope_not_issuable")
    if not is_operated_runtime():
        return "local-runtime-no-credential"
    return _operated_execution_proof(scope, binding)


def _verify_operated_execution_proof(
    scope: str,
    binding: OperatedAdmissionBinding,
    authorization_evidence: object,
) -> None:
    """Verify worker evidence without exposing credential semantics to Core."""

    if scope not in _ISSUABLE_OPERATIONS_SCOPES:
        raise OperatedExecutionDenied(
            "operated_execution_capability_scope_not_issuable"
        )
    expected = _operated_execution_proof(scope, binding)
    if (
        not isinstance(authorization_evidence, str)
        or not _HMAC_SHA256.fullmatch(authorization_evidence)
        or not hmac.compare_digest(expected, authorization_evidence)
    ):
        raise OperatedExecutionDenied("operated_execution_capability_proof_invalid")


def _operated_execution_proof(
    scope: str,
    binding: OperatedAdmissionBinding,
) -> str:
    material = (
        b"market-research-operated-execution-v1\0"
        + scope.encode("ascii")
        + b"\0"
        + binding.canonical_bytes()
    )
    digest = hmac.new(
        _load_systemd_worker_credential(),
        material,
        hashlib.sha256,
    ).hexdigest()
    return "hmac-sha256:" + digest


def _load_systemd_worker_credential() -> bytes:
    """Read the worker-only systemd credential after OS identity checks."""

    try:
        effective_uid = os.geteuid()
        identity = pwd.getpwuid(effective_uid)
    except (AttributeError, KeyError, OSError) as exc:
        raise OperatedExecutionDenied(
            "operated_execution_service_identity_invalid"
        ) from exc
    if identity.pw_name != _OPERATIONS_SERVICE_USER:
        raise OperatedExecutionDenied("operated_execution_service_identity_invalid")

    raw_directory = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    directory = Path(raw_directory)
    if (
        not raw_directory
        or not directory.is_absolute()
        or ".." in directory.parts
        or directory == _SYSTEMD_CREDENTIAL_ROOT
    ):
        raise OperatedExecutionDenied("operated_execution_credential_unavailable")
    try:
        resolved_directory = directory.resolve(strict=True)
        resolved_directory.relative_to(_SYSTEMD_CREDENTIAL_ROOT)
        if resolved_directory != directory:
            raise ValueError
        directory_status = directory.lstat()
    except (OSError, ValueError) as exc:
        raise OperatedExecutionDenied(
            "operated_execution_credential_unavailable"
        ) from exc
    if (
        directory.is_symlink()
        or not stat.S_ISDIR(directory_status.st_mode)
        or directory_status.st_uid not in {0, effective_uid}
        or stat.S_IMODE(directory_status.st_mode) & 0o022
    ):
        raise OperatedExecutionDenied("operated_execution_credential_unsafe")

    path = directory / _SYSTEMD_CREDENTIAL_NAME
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid not in {0, effective_uid}
                or stat.S_IMODE(status.st_mode) & 0o077
                or status.st_size != 32
            ):
                raise OperatedExecutionDenied("operated_execution_credential_unsafe")
            credential = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
    except OperatedExecutionDenied:
        raise
    except OSError as exc:
        raise OperatedExecutionDenied(
            "operated_execution_credential_unavailable"
        ) from exc
    if len(credential) != 32:
        raise OperatedExecutionDenied("operated_execution_credential_unsafe")
    return credential


def _binding(decision: AdmissionDecision) -> OperatedAdmissionBinding:
    if (
        decision.status != ACTIVE
        or not decision.acquired
        or decision.lease_token is None
        or decision.fencing_token is None
        or decision.lease_expires_at is None
    ):
        raise PermissionError("operated_execution_requires_active_admission_claim")
    return OperatedAdmissionBinding(
        authority=decision.authority,
        experiment_id=decision.experiment_id,
        manifest_hash=decision.manifest_hash,
        request_id=decision.request_id,
        request_hash=decision.request_hash,
        owner_id=decision.owner_id,
        claim_id=str(decision.run_id),
        lease_token=str(decision.lease_token),
        fencing_token=decision.fencing_token,
        lease_expires_at=decision.lease_expires_at.isoformat(),
    )


@contextmanager
def admitted_cli_execution_context(decision: AdmissionDecision) -> Iterator[None]:
    """Authorize exactly one admitted CLI application invocation."""

    binding = _binding(decision)
    proof = _create_operated_execution_proof(
        ADMITTED_CLI_EXECUTION_SCOPE,
        binding,
    )
    with _issue_operated_execution_capability(
        ADMITTED_CLI_EXECUTION_SCOPE,
        binding=binding,
        authorization_evidence=proof,
        evidence_verifier=_verify_operated_execution_proof,
    ):
        yield


@contextmanager
def research_job_execution_context(decision: AdmissionDecision) -> Iterator[None]:
    """Authorize exactly one admitted Web job dispatch invocation."""

    binding = _binding(decision)
    proof = _create_operated_execution_proof(
        RESEARCH_JOB_DISPATCH_SCOPE,
        binding,
    )
    with _issue_operated_execution_capability(
        RESEARCH_JOB_DISPATCH_SCOPE,
        binding=binding,
        authorization_evidence=proof,
        evidence_verifier=_verify_operated_execution_proof,
    ):
        yield


__all__ = ["admitted_cli_execution_context", "research_job_execution_context"]
