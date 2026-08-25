"""One-shot in-process authority for authenticated Web governance calls.

The public CLI cannot serialize, construct, or transport this capability.  A
trusted Web adapter enters it only after deriving the actor snapshot from an
authenticated server-side session.  The application service consumes the
exact action/actor/request binding once before any governance mutation.
"""

from __future__ import annotations

import os
import threading
from contextvars import ContextVar, Token
from typing import Never

from market_research.research.governance import GovernanceError
from market_research.research.hashing import sha256_prefixed

from .contracts import ActorContext, ApplicationRequest


RECORD_REVIEW_ACTION = "record_review"
APPROVE_CANDIDATE_ACTION = "approve_candidate"
_ACTIONS = frozenset({RECORD_REVIEW_ACTION, APPROVE_CANDIDATE_ACTION})
_ISSUER_SENTINEL = object()


def _binding_hash(
    *,
    action: str,
    actor: ActorContext,
    request: ApplicationRequest,
) -> str:
    return sha256_prefixed(
        {
            "schema_version": 1,
            "action": action,
            "actor": actor.model_dump(mode="json"),
            "request": request.model_dump(mode="json"),
        },
        label="authenticated_web_governance_request",
    )


class _AuthenticatedWebGovernanceCapability:
    __slots__ = (
        "_action",
        "_binding_hash",
        "_closed",
        "_consumed",
        "_entered",
        "_pid",
        "_reset_token",
        "_session_subject",
        "_thread_id",
    )

    def __init__(
        self,
        sentinel: object,
        *,
        action: str,
        session_subject: str,
        actor: ActorContext,
        request: ApplicationRequest,
    ) -> None:
        if sentinel is not _ISSUER_SENTINEL:
            raise TypeError("web_governance_capability_constructor_private")
        if action not in _ACTIONS:
            raise GovernanceError("web_governance_capability_action_invalid")
        if actor.source != "web" or actor.actor_id != session_subject:
            raise GovernanceError("web_governance_session_actor_mismatch")
        if "*" in actor.permissions:
            raise GovernanceError("governance_wildcard_permission_forbidden")
        self._action = action
        self._session_subject = session_subject
        self._binding_hash = _binding_hash(
            action=action,
            actor=actor,
            request=request,
        )
        self._pid = os.getpid()
        self._thread_id = threading.get_ident()
        self._entered = False
        self._closed = False
        self._consumed = False
        self._reset_token: (
            Token[_AuthenticatedWebGovernanceCapability | None] | None
        ) = None

    def __enter__(self) -> _AuthenticatedWebGovernanceCapability:
        if (
            self._entered
            or self._closed
            or self._pid != os.getpid()
            or self._thread_id != threading.get_ident()
            or _ACTIVE_WEB_GOVERNANCE_CAPABILITY.get() is not None
        ):
            raise GovernanceError("web_governance_capability_reuse")
        self._entered = True
        self._reset_token = _ACTIVE_WEB_GOVERNANCE_CAPABILITY.set(self)
        return self

    def __exit__(self, *_exc: object) -> None:
        token = self._reset_token
        self._reset_token = None
        self._closed = True
        if token is not None:
            _ACTIVE_WEB_GOVERNANCE_CAPABILITY.reset(token)

    def consume(
        self,
        *,
        action: str,
        actor: ActorContext,
        request: ApplicationRequest,
    ) -> None:
        if (
            not self._entered
            or self._closed
            or self._pid != os.getpid()
            or self._thread_id != threading.get_ident()
            or _ACTIVE_WEB_GOVERNANCE_CAPABILITY.get() is not self
        ):
            raise GovernanceError("web_governance_capability_inactive")
        if self._consumed:
            raise GovernanceError("web_governance_capability_replayed")
        if action != self._action or actor.actor_id != self._session_subject:
            raise GovernanceError("web_governance_capability_scope_mismatch")
        if self._binding_hash != _binding_hash(
            action=action,
            actor=actor,
            request=request,
        ):
            raise GovernanceError("web_governance_capability_request_mismatch")
        self._consumed = True

    def __copy__(self) -> Never:
        raise TypeError("web_governance_capability_not_copyable")

    def __deepcopy__(self, _memo: object) -> Never:
        raise TypeError("web_governance_capability_not_copyable")

    def __reduce__(self) -> Never:
        raise TypeError("web_governance_capability_not_serializable")


_ACTIVE_WEB_GOVERNANCE_CAPABILITY: ContextVar[
    _AuthenticatedWebGovernanceCapability | None
] = ContextVar("market_research_authenticated_web_governance", default=None)


def _authenticated_web_governance_context(
    *,
    action: str,
    session_subject: str,
    actor: ActorContext,
    request: ApplicationRequest,
) -> _AuthenticatedWebGovernanceCapability:
    """Private adapter seam; only the authenticated Web adapter may import it."""

    return _AuthenticatedWebGovernanceCapability(
        _ISSUER_SENTINEL,
        action=action,
        session_subject=session_subject,
        actor=actor,
        request=request,
    )


def require_authenticated_web_governance(
    *,
    action: str,
    actor: ActorContext,
    request: ApplicationRequest,
) -> None:
    capability = _ACTIVE_WEB_GOVERNANCE_CAPABILITY.get()
    if capability is None:
        raise GovernanceError("authenticated_web_governance_capability_required")
    capability.consume(action=action, actor=actor, request=request)


__all__ = [
    "APPROVE_CANDIDATE_ACTION",
    "RECORD_REVIEW_ACTION",
    "require_authenticated_web_governance",
]
