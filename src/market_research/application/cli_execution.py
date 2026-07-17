"""Application boundary for Operations-admitted Research execution.

The operated runtime is deliberately fail-closed.  Merely setting an
environment variable cannot authorize an in-process execution: the Operations
distribution must create a short-lived, in-memory capability context and the
called adapter must consume it exactly once.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Sequence


OPERATED_RUNTIME_PROFILE = "operated"
ADMITTED_CLI_EXECUTION_SCOPE = "operations.admitted-cli"
RESEARCH_JOB_DISPATCH_SCOPE = "operations.research-job-dispatch"
LEGACY_WEB_WORKER_SCOPE = "operations.legacy-web-worker"
LEGACY_WEB_CLAIM_SCOPE = "operations.legacy-web-claim"

_ISSUABLE_OPERATIONS_SCOPES = frozenset(
    {ADMITTED_CLI_EXECUTION_SCOPE, RESEARCH_JOB_DISPATCH_SCOPE}
)
_ISSUER_SENTINEL = object()
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class OperatedExecutionDenied(PermissionError):
    """Raised when operated execution lacks its in-process one-shot authority."""


@dataclass(frozen=True, slots=True)
class OperatedAdmissionBinding:
    """Immutable identity of one fenced PostgreSQL admission claim."""

    authority: str
    experiment_id: str
    manifest_hash: str
    request_id: str
    request_hash: str
    owner_id: str
    claim_id: str
    lease_token: str
    fencing_token: int
    lease_expires_at: str

    def canonical_bytes(self) -> bytes:
        self._validate()
        return json.dumps(
            {"schema_version": 1, **self.as_dict()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def as_dict(self) -> dict[str, str | int]:
        return {
            "authority": self.authority,
            "experiment_id": self.experiment_id,
            "manifest_hash": self.manifest_hash,
            "request_id": self.request_id,
            "request_hash": self.request_hash,
            "owner_id": self.owner_id,
            "claim_id": self.claim_id,
            "lease_token": self.lease_token,
            "fencing_token": self.fencing_token,
            "lease_expires_at": self.lease_expires_at,
        }

    def _validate(self) -> None:
        bounded = {
            "authority": (self.authority, 128),
            "experiment_id": (self.experiment_id, 255),
            "request_id": (self.request_id, 255),
            "owner_id": (self.owner_id, 255),
        }
        if any(not value or len(value) > limit for value, limit in bounded.values()):
            raise OperatedExecutionDenied("operated_admission_binding_invalid")
        if not _SHA256.fullmatch(self.manifest_hash) or not _SHA256.fullmatch(
            self.request_hash
        ):
            raise OperatedExecutionDenied("operated_admission_binding_invalid")
        try:
            if (
                str(uuid.UUID(self.claim_id)) != self.claim_id
                or str(uuid.UUID(self.lease_token)) != self.lease_token
            ):
                raise ValueError
            expires_at = datetime.fromisoformat(
                self.lease_expires_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise OperatedExecutionDenied("operated_admission_binding_invalid") from exc
        if (
            expires_at.tzinfo is None
            or expires_at <= datetime.now(UTC)
            or isinstance(self.fencing_token, bool)
            or self.fencing_token < 1
        ):
            raise OperatedExecutionDenied("operated_admission_binding_invalid")


OperatedCapabilityEvidenceVerifier = Callable[
    [str, OperatedAdmissionBinding, object], None
]


class _OperatedExecutionCapability:
    """Process- and thread-bound capability which may be entered only once."""

    __slots__ = (
        "_closed",
        "_consumed",
        "_entered",
        "_binding",
        "_pid",
        "_reset_token",
        "_scope",
        "_thread_id",
    )

    def __init__(
        self,
        sentinel: object,
        scope: str,
        binding: OperatedAdmissionBinding,
    ) -> None:
        if sentinel is not _ISSUER_SENTINEL:
            raise TypeError("operated_execution_capability_constructor_private")
        self._scope = scope
        self._binding = binding
        self._pid = os.getpid()
        self._thread_id = threading.get_ident()
        self._entered = False
        self._closed = False
        self._consumed = False
        self._reset_token: Token[_OperatedExecutionCapability | None] | None = None

    def __enter__(self) -> _OperatedExecutionCapability:
        if (
            self._entered
            or self._closed
            or self._pid != os.getpid()
            or self._thread_id != threading.get_ident()
        ):
            raise OperatedExecutionDenied("operated_execution_capability_reuse")
        self._entered = True
        self._reset_token = _ACTIVE_OPERATED_CAPABILITY.set(self)
        return self

    def __exit__(self, *_exc: object) -> None:
        token = self._reset_token
        self._reset_token = None
        self._closed = True
        if token is not None:
            _ACTIVE_OPERATED_CAPABILITY.reset(token)

    def _consume(
        self,
        expected_scope: str,
        *,
        admission_request_id: str | None,
        admission_request_hash: str | None,
    ) -> None:
        if (
            not self._entered
            or self._closed
            or self._pid != os.getpid()
            or self._thread_id != threading.get_ident()
            or _ACTIVE_OPERATED_CAPABILITY.get() is not self
        ):
            raise OperatedExecutionDenied("operated_execution_capability_inactive")
        if self._scope != expected_scope:
            raise OperatedExecutionDenied(
                "operated_execution_capability_scope_mismatch"
            )
        if (
            admission_request_id is None
            or admission_request_hash is None
            or self._binding.request_id != admission_request_id
            or self._binding.request_hash != admission_request_hash
        ):
            raise OperatedExecutionDenied(
                "operated_execution_capability_claim_mismatch"
            )
        self._binding._validate()
        if self._consumed:
            raise OperatedExecutionDenied("operated_execution_capability_replayed")
        self._consumed = True

    def __copy__(self) -> None:
        raise TypeError("operated_execution_capability_not_copyable")

    def __deepcopy__(self, _memo: object) -> None:
        raise TypeError("operated_execution_capability_not_copyable")

    def __reduce__(self) -> None:
        raise TypeError("operated_execution_capability_not_serializable")


_ACTIVE_OPERATED_CAPABILITY: ContextVar[_OperatedExecutionCapability | None] = (
    ContextVar("market_research_operated_execution_capability", default=None)
)


def is_operated_runtime() -> bool:
    """Return whether this process is configured for Operations ownership."""

    return (
        os.environ.get("RESEARCH_RUNTIME_PROFILE", "").strip().lower()
        == OPERATED_RUNTIME_PROFILE
    )


def require_operated_execution_capability(
    scope: str,
    *,
    admission_request_id: str | None = None,
    admission_request_hash: str | None = None,
) -> None:
    """Consume the active capability when running in the operated profile.

    Local research remains intentionally usable without Operations.  In the
    operated profile the authority is an object held in a ``ContextVar``; no
    environment value is interpreted as a credential.
    """

    if not is_operated_runtime():
        return
    capability = _ACTIVE_OPERATED_CAPABILITY.get()
    if capability is None:
        raise OperatedExecutionDenied("operated_execution_capability_missing")
    capability._consume(
        scope,
        admission_request_id=admission_request_id,
        admission_request_hash=admission_request_hash,
    )


def _issue_operated_execution_capability(
    scope: str,
    *,
    binding: OperatedAdmissionBinding,
    authorization_evidence: object,
    evidence_verifier: OperatedCapabilityEvidenceVerifier | None = None,
) -> _OperatedExecutionCapability:
    """Issue one capability after a caller-owned evidence verifier accepts it.

    This is a private cross-distribution seam.  Architecture tests constrain
    imports of this issuer to ``services/research_operations``; Web receives
    only the public consumer above.  Core deliberately knows nothing about the
    verifier's credential source, proof format, service identity, or operating
    system.  Legacy Web worker scopes are intentionally not issuable.
    """

    if scope not in _ISSUABLE_OPERATIONS_SCOPES:
        raise ValueError("operated_execution_capability_scope_not_issuable")
    binding.canonical_bytes()
    if is_operated_runtime():
        if evidence_verifier is None:
            raise OperatedExecutionDenied(
                "operated_execution_capability_verifier_missing"
            )
        try:
            evidence_verifier(scope, binding, authorization_evidence)
        except OperatedExecutionDenied:
            raise
        except Exception as exc:
            raise OperatedExecutionDenied(
                "operated_execution_capability_verification_failed"
            ) from exc
    return _OperatedExecutionCapability(_ISSUER_SENTINEL, scope, binding)


@dataclass(frozen=True, slots=True)
class ResearchCliOutcome:
    """Stable result material returned to the operational admission layer."""

    exit_code: int
    run_id: str | None
    result_hash: str | None


def execute_admitted_research_cli(
    argv: Sequence[str],
    *,
    admission_request_id: str | None = None,
    admission_request_hash: str | None = None,
) -> ResearchCliOutcome:
    """Execute a pre-admitted command and return its reproducibility binding.

    This function deliberately does not perform admission.  The Operations
    service owns authorization, admission, lease fencing, and audit evidence;
    this application adapter only keeps it from importing CLI internals.
    """

    require_operated_execution_capability(
        ADMITTED_CLI_EXECUTION_SCOPE,
        admission_request_id=admission_request_id,
        admission_request_hash=admission_request_hash,
    )

    from market_research.research_cli.context import build_research_context
    from market_research.research_cli.main import main

    context = build_research_context()
    exit_code = int(main(list(argv), context=context))
    return ResearchCliOutcome(
        exit_code=exit_code,
        run_id=context.run_id,
        result_hash=context.run_result_hash,
    )


__all__ = [
    "ADMITTED_CLI_EXECUTION_SCOPE",
    "LEGACY_WEB_CLAIM_SCOPE",
    "LEGACY_WEB_WORKER_SCOPE",
    "OPERATED_RUNTIME_PROFILE",
    "OperatedAdmissionBinding",
    "OperatedExecutionDenied",
    "RESEARCH_JOB_DISPATCH_SCOPE",
    "ResearchCliOutcome",
    "execute_admitted_research_cli",
    "is_operated_runtime",
    "require_operated_execution_capability",
]
