"""Authentication audit signals with fail-closed session handling."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.db import transaction
from django.dispatch import receiver
from django.http import HttpRequest

from .audit import record_web_audit_event
from .login_throttle import login_subject_hashes


AUTH_LOGIN_SUCCEEDED = "authentication_login_succeeded"
AUTH_LOGIN_FAILED = "authentication_login_failed"
AUTH_LOGOUT = "authentication_logout"
AUTH_OBJECT_TYPE = "authentication_subject"
AUTH_SUBJECT_SCHEME = "secret_hmac_sha256_v1"
AUTH_AUDIT_FAILURE_POLICY = {
    "intent_insert_failure": "fail_closed_no_authenticated_session",
    "logout_insert_failure": "terminate_session_and_report_unavailable",
    "projection_failure": "retain_pending_outbox_intent_and_fail_readiness",
}


class AuthenticationAuditUnavailable(RuntimeError):
    """Raised when an authentication event cannot enter the durable outbox."""


def terminate_session_without_signal(request: HttpRequest | None) -> None:
    """End a session without recursively emitting another auth signal."""

    if request is None:
        return
    request.session.flush()
    request.user = AnonymousUser()


def _correlation_id(request: HttpRequest | None) -> str:
    value = getattr(request, "correlation_id", None) if request is not None else None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return str(uuid.uuid4())


def _username_subject_value(
    *, user: Any | None, credentials: Mapping[str, Any] | None
) -> object:
    if user is not None and callable(getattr(user, "get_username", None)):
        return user.get_username()
    values = credentials or {}
    # Only the account identifier is read and immediately HMACed.  Passwords,
    # tokens and the credentials mapping itself never enter audit material.
    for key in ("username", "email"):
        if key in values:
            return values.get(key)
    return ""


def record_authentication_audit(
    *,
    action: str,
    request: HttpRequest | None,
    user: Any | None = None,
    credentials: Mapping[str, Any] | None = None,
) -> None:
    """Commit one bounded pseudonymous authentication event to the outbox."""

    username_subject, network_subject = login_subject_hashes(
        request,
        _username_subject_value(user=user, credentials=credentials),
    )
    authenticated = bool(getattr(user, "is_authenticated", False))
    actor_prefix = "authenticated" if authenticated else "unauthenticated"
    try:
        with transaction.atomic():
            record_web_audit_event(
                action=action,
                actor_id=f"{actor_prefix}-subject:{username_subject}",
                object_type=AUTH_OBJECT_TYPE,
                object_id=f"account-subject:{username_subject}",
                correlation_id=_correlation_id(request),
                details={
                    "outcome": action,
                    "authenticated_subject": authenticated,
                    "account_subject_hash": username_subject,
                    "network_subject_hash": network_subject,
                    "subject_scheme": AUTH_SUBJECT_SCHEME,
                    "failure_policy": AUTH_AUDIT_FAILURE_POLICY,
                },
            )
    except Exception as exc:
        raise AuthenticationAuditUnavailable(
            "authentication_audit_outbox_unavailable"
        ) from exc


@receiver(
    user_logged_in,
    dispatch_uid="portal.authentication_audit.user_logged_in.v1",
)
def _audit_login_success(
    sender: type[Any], request: HttpRequest | None, user: Any, **kwargs: Any
) -> None:
    del sender, kwargs
    try:
        record_authentication_audit(
            action=AUTH_LOGIN_SUCCEEDED,
            request=request,
            user=user,
        )
    except AuthenticationAuditUnavailable:
        # django.contrib.auth.login mutates the in-memory session before
        # dispatching this signal.  Revoke it here so every login surface,
        # including the Django admin, fails closed.
        terminate_session_without_signal(request)
        raise


@receiver(
    user_login_failed,
    dispatch_uid="portal.authentication_audit.user_login_failed.v1",
)
def _audit_login_failure(
    sender: str,
    credentials: Mapping[str, Any],
    request: HttpRequest | None,
    **kwargs: Any,
) -> None:
    del sender, kwargs
    record_authentication_audit(
        action=AUTH_LOGIN_FAILED,
        request=request,
        credentials=credentials,
    )


@receiver(
    user_logged_out,
    dispatch_uid="portal.authentication_audit.user_logged_out.v1",
)
def _audit_logout(
    sender: type[Any], request: HttpRequest | None, user: Any | None, **kwargs: Any
) -> None:
    del sender, kwargs
    try:
        record_authentication_audit(
            action=AUTH_LOGOUT,
            request=request,
            user=user,
        )
    except AuthenticationAuditUnavailable:
        # Logout is security-reducing and must complete even when audit intent
        # insertion is unavailable.  The caller receives a fixed failure, but
        # the session is not kept alive.
        terminate_session_without_signal(request)
        raise


__all__ = [
    "AUTH_AUDIT_FAILURE_POLICY",
    "AUTH_LOGIN_FAILED",
    "AUTH_LOGIN_SUCCEEDED",
    "AUTH_LOGOUT",
    "AuthenticationAuditUnavailable",
    "record_authentication_audit",
    "terminate_session_without_signal",
]
