from __future__ import annotations

import hashlib
import hmac
import ipaddress
import unicodedata
from datetime import datetime, timedelta
from typing import Iterable

from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone

from .models import LoginThrottle


_SUBJECT_DOMAIN = "internal-web-login-throttle:v1"
_GENERIC_LOGIN_ERROR = "아이디 또는 비밀번호를 확인해 주세요."


def _subject_hash(kind: str, value: str) -> str:
    key = str(settings.SECRET_KEY).encode("utf-8")
    message = f"{_SUBJECT_DOMAIN}:{kind}\0{value}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def login_subject_hashes(request: HttpRequest | None, username: object) -> tuple[str, ...]:
    """Return stable, secret-keyed subjects without retaining login identifiers."""

    normalized_username = unicodedata.normalize("NFKC", str(username)).strip().casefold()
    if not normalized_username:
        normalized_username = "<empty>"

    remote_addr = ""
    if request is not None:
        remote_addr = str(request.META.get("REMOTE_ADDR", "")).strip()
    try:
        normalized_address = str(ipaddress.ip_address(remote_addr))
    except ValueError:
        normalized_address = "<unknown>"

    return (
        _subject_hash("username", normalized_username),
        _subject_hash("remote-address", normalized_address),
    )


def _deduplicated(subjects: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(subjects))


def login_is_blocked(
    subjects: Iterable[str],
    *,
    observed_at: datetime | None = None,
) -> bool:
    subject_hashes = _deduplicated(subjects)
    if not subject_hashes:
        return False
    now = observed_at or timezone.now()
    return LoginThrottle.objects.filter(
        subject_hash__in=subject_hashes,
        blocked_until__gt=now,
    ).exists()


def _record_subject_failure(subject_hash: str, *, observed_at: datetime) -> None:
    window = timedelta(seconds=int(settings.INTERNAL_WEB_LOGIN_FAILURE_WINDOW_SECONDS))
    block = timedelta(seconds=int(settings.INTERNAL_WEB_LOGIN_BLOCK_SECONDS))
    failure_limit = int(settings.INTERNAL_WEB_LOGIN_FAILURE_LIMIT)

    # A concurrent first failure can race on the unique subject.  Retrying
    # outside the failed savepoint lets the winner's row become lockable.
    for attempt in range(2):
        try:
            with transaction.atomic():
                record = (
                    LoginThrottle.objects.select_for_update()
                    .filter(subject_hash=subject_hash)
                    .first()
                )
                if record is None:
                    LoginThrottle.objects.create(
                        subject_hash=subject_hash,
                        failure_count=1,
                        window_started_at=observed_at,
                        blocked_until=(
                            observed_at + block if failure_limit == 1 else None
                        ),
                    )
                    return

                if record.blocked_until is not None and record.blocked_until > observed_at:
                    return

                if record.window_started_at <= observed_at - window:
                    failure_count = 1
                    window_started_at = observed_at
                else:
                    failure_count = record.failure_count + 1
                    window_started_at = record.window_started_at

                record.failure_count = failure_count
                record.window_started_at = window_started_at
                record.blocked_until = (
                    observed_at + block if failure_count >= failure_limit else None
                )
                record.save(
                    update_fields=(
                        "failure_count",
                        "window_started_at",
                        "blocked_until",
                        "updated_at",
                    )
                )
                return
        except IntegrityError:
            if attempt == 1:
                raise


def record_login_failure(
    subjects: Iterable[str],
    *,
    observed_at: datetime | None = None,
) -> None:
    now = observed_at or timezone.now()
    for subject_hash in _deduplicated(subjects):
        _record_subject_failure(subject_hash, observed_at=now)


def reset_login_failures(subjects: Iterable[str]) -> None:
    subject_hashes = _deduplicated(subjects)
    if subject_hashes:
        LoginThrottle.objects.filter(subject_hash__in=subject_hashes).delete()


def _generic_login_error() -> ValidationError:
    return ValidationError(_GENERIC_LOGIN_ERROR, code="invalid_login")


class ThrottledAuthenticationForm(AuthenticationForm):
    """Authenticate with persistent username and source-address throttles."""

    def clean(self) -> dict[str, object]:
        username = self.data.get(self.add_prefix("username"))
        password = self.data.get(self.add_prefix("password"))
        subjects: tuple[str, ...] = ()
        blocked = False

        if username and password:
            subjects = login_subject_hashes(self.request, username)
            try:
                blocked = login_is_blocked(subjects)
            except DatabaseError as exc:
                raise _generic_login_error() from exc

        try:
            cleaned_data = super().clean()
        except ValidationError as exc:
            if subjects and not blocked:
                try:
                    record_login_failure(subjects)
                except DatabaseError as database_exc:
                    raise _generic_login_error() from database_exc
            raise _generic_login_error() from exc

        if not subjects:
            return cleaned_data

        if blocked:
            self.user_cache = None
            raise _generic_login_error()

        try:
            # Successful authentication clears only the account-scoped
            # subject. Keeping the source-address counter prevents a valid
            # account at the same address from erasing an address-wide spray
            # throttle.
            reset_login_failures(subjects[:1])
        except DatabaseError as exc:
            self.user_cache = None
            raise _generic_login_error() from exc
        return cleaned_data


__all__ = [
    "ThrottledAuthenticationForm",
    "login_is_blocked",
    "login_subject_hashes",
    "record_login_failure",
    "reset_login_failures",
]
