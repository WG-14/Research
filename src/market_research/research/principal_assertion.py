"""Authenticated, time-bounded principal assertions for research verification.

The research engine owns only the assertion contract and verification logic.
Identity lifecycle and key issuance remain external adapter responsibilities.
Trusted HMAC key material is read from a repository-external file selected by a
repository-external trust store; key bytes are never returned or serialized
into research evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from market_research.paths import ResearchPathManager

from .hashing import canonical_json_bytes, sha256_prefixed


PRINCIPAL_ASSERTION_SCHEMA_VERSION = 1
PRINCIPAL_ASSERTION_ALGORITHM = "hmac-sha256"
INDEPENDENT_VERIFICATION_ASSERTION_AUDIENCE = "market-research:independent-verification"
INDEPENDENT_VERIFICATION_ASSERTION_PURPOSE = (
    "research-reproduce-run:independent-verification-publication"
)
INDEPENDENT_VERIFIER_ROLE = "independent_verifier"
MAXIMUM_PRINCIPAL_ASSERTION_LIFETIME_SECONDS = 28_800

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$")
_RESEARCH_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_SIGNATURE_DOMAIN = b"market-research:principal-assertion:v1\x00"


class PrincipalAssertionError(ValueError):
    """A principal assertion or its external trust configuration is invalid."""


@dataclass(frozen=True, slots=True)
class IndependentVerificationAssertionScope:
    """One non-transferable independent-verification publication scope."""

    verification_id: str
    verification_version: str
    experiment_id: str
    research_version: str
    source_report_hash: str
    baseline_receipt_hash: str
    purpose: str = INDEPENDENT_VERIFICATION_ASSERTION_PURPOSE

    def __post_init__(self) -> None:
        for name, value in (
            ("verification_id", self.verification_id),
            ("verification_version", self.verification_version),
            ("experiment_id", self.experiment_id),
        ):
            if (
                not isinstance(value, str)
                or _RESEARCH_IDENTIFIER.fullmatch(value) is None
            ):
                raise PrincipalAssertionError(
                    f"principal_assertion_scope_{name}_invalid"
                )
        if (
            not isinstance(self.research_version, str)
            or not self.research_version.strip()
            or self.research_version != self.research_version.strip()
        ):
            raise PrincipalAssertionError(
                "principal_assertion_scope_research_version_invalid"
            )
        for name, value in (
            ("source_report_hash", self.source_report_hash),
            ("baseline_receipt_hash", self.baseline_receipt_hash),
        ):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise PrincipalAssertionError(
                    f"principal_assertion_scope_{name}_invalid"
                )
        if self.purpose != INDEPENDENT_VERIFICATION_ASSERTION_PURPOSE:
            raise PrincipalAssertionError("principal_assertion_scope_purpose_invalid")

    def as_dict(self) -> dict[str, str]:
        return {
            "purpose": self.purpose,
            "verification_id": self.verification_id,
            "verification_version": self.verification_version,
            "experiment_id": self.experiment_id,
            "research_version": self.research_version,
            "source_report_hash": self.source_report_hash,
            "baseline_receipt_hash": self.baseline_receipt_hash,
        }

    def content_hash(self) -> str:
        return sha256_prefixed(
            self.as_dict(),
            label="independent_verification_principal_assertion_scope",
        )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> IndependentVerificationAssertionScope:
        expected = {
            "purpose",
            "verification_id",
            "verification_version",
            "experiment_id",
            "research_version",
            "source_report_hash",
            "baseline_receipt_hash",
        }
        if set(payload) != expected or any(
            not isinstance(payload.get(key), str) for key in expected
        ):
            raise PrincipalAssertionError("principal_assertion_scope_schema_invalid")
        return cls(
            purpose=str(payload["purpose"]),
            verification_id=str(payload["verification_id"]),
            verification_version=str(payload["verification_version"]),
            experiment_id=str(payload["experiment_id"]),
            research_version=str(payload["research_version"]),
            source_report_hash=str(payload["source_report_hash"]),
            baseline_receipt_hash=str(payload["baseline_receipt_hash"]),
        )


@dataclass(frozen=True, slots=True)
class PrincipalAssertion:
    """A signed external identity assertion with an immutable research scope."""

    issuer: str
    key_id: str
    subject: str
    roles: tuple[str, ...]
    authenticated_at: str
    expires_at: str
    nonce: str
    audience: str
    scope: IndependentVerificationAssertionScope
    content_hash: str
    signature: str
    algorithm: str = PRINCIPAL_ASSERTION_ALGORITHM

    def __post_init__(self) -> None:
        for name, value in (
            ("issuer", self.issuer),
            ("key_id", self.key_id),
            ("subject", self.subject),
            ("nonce", self.nonce),
        ):
            if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
                raise PrincipalAssertionError(f"principal_assertion_{name}_invalid")
        if (
            not isinstance(self.roles, tuple)
            or not self.roles
            or any(
                not isinstance(role, str) or _IDENTIFIER.fullmatch(role) is None
                for role in self.roles
            )
            or tuple(sorted(set(self.roles))) != self.roles
        ):
            raise PrincipalAssertionError("principal_assertion_roles_invalid")
        if self.algorithm != PRINCIPAL_ASSERTION_ALGORITHM:
            raise PrincipalAssertionError("principal_assertion_algorithm_invalid")
        if self.audience != INDEPENDENT_VERIFICATION_ASSERTION_AUDIENCE:
            raise PrincipalAssertionError("principal_assertion_audience_invalid")
        authenticated_at = _timestamp(
            self.authenticated_at,
            "principal_assertion_authenticated_at_invalid",
        )
        expires_at = _timestamp(
            self.expires_at,
            "principal_assertion_expires_at_invalid",
        )
        lifetime = expires_at - authenticated_at
        if lifetime <= timedelta(0) or lifetime > timedelta(
            seconds=MAXIMUM_PRINCIPAL_ASSERTION_LIFETIME_SECONDS
        ):
            raise PrincipalAssertionError("principal_assertion_lifetime_invalid")
        if (
            not isinstance(self.content_hash, str)
            or _SHA256.fullmatch(self.content_hash) is None
        ):
            raise PrincipalAssertionError("principal_assertion_content_hash_invalid")
        if (
            not isinstance(self.signature, str)
            or _SIGNATURE.fullmatch(self.signature) is None
        ):
            raise PrincipalAssertionError("principal_assertion_signature_invalid")
        if self.content_hash != self.expected_content_hash():
            raise PrincipalAssertionError("principal_assertion_content_hash_mismatch")

    def material(self) -> dict[str, Any]:
        return {
            "schema_version": PRINCIPAL_ASSERTION_SCHEMA_VERSION,
            "algorithm": self.algorithm,
            "issuer": self.issuer,
            "key_id": self.key_id,
            "subject": self.subject,
            "roles": list(self.roles),
            "authenticated_at": self.authenticated_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "audience": self.audience,
            "scope": self.scope.as_dict(),
        }

    def expected_content_hash(self) -> str:
        return sha256_prefixed(
            self.material(),
            label="independent_verifier_principal_assertion",
        )

    def signed_material(self) -> dict[str, Any]:
        return {**self.material(), "content_hash": self.content_hash}

    def as_dict(self) -> dict[str, Any]:
        return {**self.signed_material(), "signature": self.signature}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PrincipalAssertion:
        expected = {
            "schema_version",
            "algorithm",
            "issuer",
            "key_id",
            "subject",
            "roles",
            "authenticated_at",
            "expires_at",
            "nonce",
            "audience",
            "scope",
            "content_hash",
            "signature",
        }
        if (
            set(payload) != expected
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != PRINCIPAL_ASSERTION_SCHEMA_VERSION
        ):
            raise PrincipalAssertionError("principal_assertion_schema_invalid")
        roles = payload.get("roles")
        scope = payload.get("scope")
        string_fields = expected - {"schema_version", "roles", "scope"}
        if (
            not isinstance(roles, (list, tuple))
            or not all(isinstance(role, str) for role in roles)
            or not isinstance(scope, Mapping)
            or any(not isinstance(payload.get(key), str) for key in string_fields)
        ):
            raise PrincipalAssertionError("principal_assertion_schema_invalid")
        return cls(
            algorithm=str(payload["algorithm"]),
            issuer=str(payload["issuer"]),
            key_id=str(payload["key_id"]),
            subject=str(payload["subject"]),
            roles=tuple(roles),
            authenticated_at=str(payload["authenticated_at"]),
            expires_at=str(payload["expires_at"]),
            nonce=str(payload["nonce"]),
            audience=str(payload["audience"]),
            scope=IndependentVerificationAssertionScope.from_dict(scope),
            content_hash=str(payload["content_hash"]),
            signature=str(payload["signature"]),
        )


def issue_principal_assertion(
    *,
    issuer: str,
    key_id: str,
    subject: str,
    roles: tuple[str, ...],
    authenticated_at: str,
    expires_at: str,
    nonce: str,
    scope: IndependentVerificationAssertionScope,
    key: bytes,
) -> PrincipalAssertion:
    """Issue one assertion; intended for external adapters and test issuers."""

    normalized_roles = tuple(sorted(set(roles)))
    material = {
        "schema_version": PRINCIPAL_ASSERTION_SCHEMA_VERSION,
        "algorithm": PRINCIPAL_ASSERTION_ALGORITHM,
        "issuer": issuer,
        "key_id": key_id,
        "subject": subject,
        "roles": list(normalized_roles),
        "authenticated_at": authenticated_at,
        "expires_at": expires_at,
        "nonce": nonce,
        "audience": INDEPENDENT_VERIFICATION_ASSERTION_AUDIENCE,
        "scope": scope.as_dict(),
    }
    content_hash = sha256_prefixed(
        material,
        label="independent_verifier_principal_assertion",
    )
    signature = _signature(key, {**material, "content_hash": content_hash})
    return PrincipalAssertion(
        issuer=issuer,
        key_id=key_id,
        subject=subject,
        roles=normalized_roles,
        authenticated_at=authenticated_at,
        expires_at=expires_at,
        nonce=nonce,
        audience=INDEPENDENT_VERIFICATION_ASSERTION_AUDIENCE,
        scope=scope,
        content_hash=content_hash,
        signature=signature,
    )


def load_principal_assertion(
    path: str | Path,
    *,
    manager: ResearchPathManager,
) -> PrincipalAssertion:
    assertion_path = _external_regular_file(
        path,
        code="principal_assertion_file",
        manager=manager,
        secret=False,
        maximum_bytes=65_536,
    )
    try:
        payload = json.loads(assertion_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PrincipalAssertionError("principal_assertion_file_invalid") from exc
    if not isinstance(payload, Mapping):
        raise PrincipalAssertionError("principal_assertion_file_invalid")
    return PrincipalAssertion.from_dict(payload)


def verify_principal_assertion(
    *,
    assertion: PrincipalAssertion,
    expected_scope: IndependentVerificationAssertionScope,
    trust_store_path: str | Path | None,
    manager: ResearchPathManager,
    now: datetime | None = None,
) -> PrincipalAssertion:
    """Authenticate one scoped assertion using configured external key material."""

    if assertion.scope != expected_scope:
        raise PrincipalAssertionError("principal_assertion_scope_mismatch")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise PrincipalAssertionError("principal_assertion_now_timezone_required")
    current = current.astimezone(timezone.utc)
    authenticated_at = _timestamp(
        assertion.authenticated_at,
        "principal_assertion_authenticated_at_invalid",
    ).astimezone(timezone.utc)
    expires_at = _timestamp(
        assertion.expires_at,
        "principal_assertion_expires_at_invalid",
    ).astimezone(timezone.utc)
    if current < authenticated_at:
        raise PrincipalAssertionError("principal_assertion_not_yet_valid")
    if current > expires_at:
        raise PrincipalAssertionError("principal_assertion_expired")
    if INDEPENDENT_VERIFIER_ROLE not in assertion.roles:
        raise PrincipalAssertionError(
            "principal_assertion_independent_verifier_role_required"
        )
    key = _trusted_key(
        trust_store_path=trust_store_path,
        issuer=assertion.issuer,
        key_id=assertion.key_id,
        manager=manager,
    )
    expected_signature = _signature(key, assertion.signed_material())
    if not hmac.compare_digest(expected_signature, assertion.signature):
        raise PrincipalAssertionError("principal_assertion_signature_invalid")
    return assertion


def _trusted_key(
    *,
    trust_store_path: str | Path | None,
    issuer: str,
    key_id: str,
    manager: ResearchPathManager,
) -> bytes:
    if trust_store_path is None:
        raise PrincipalAssertionError("principal_assertion_trust_store_required")
    path = _external_regular_file(
        trust_store_path,
        code="principal_assertion_trust_store",
        manager=manager,
        secret=False,
        maximum_bytes=65_536,
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_invalid"
        ) from exc
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"schema_version", "keys"}
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("keys"), list)
    ):
        raise PrincipalAssertionError("principal_assertion_trust_store_invalid")
    matches: list[Mapping[str, Any]] = []
    for item in payload["keys"]:
        if not isinstance(item, Mapping) or set(item) != {
            "issuer",
            "key_id",
            "algorithm",
            "key_path",
        }:
            raise PrincipalAssertionError("principal_assertion_trust_store_invalid")
        if (
            item.get("issuer") == issuer
            and item.get("key_id") == key_id
            and item.get("algorithm") == PRINCIPAL_ASSERTION_ALGORITHM
        ):
            matches.append(item)
    if len(matches) != 1:
        raise PrincipalAssertionError("principal_assertion_trusted_key_not_found")
    key_path = matches[0].get("key_path")
    if not isinstance(key_path, str):
        raise PrincipalAssertionError("principal_assertion_trust_store_invalid")
    path = _external_regular_file(
        key_path,
        code="principal_assertion_key",
        manager=manager,
        secret=True,
        maximum_bytes=4_096,
    )
    try:
        key = path.read_bytes()
    except OSError as exc:
        raise PrincipalAssertionError("principal_assertion_key_unreadable") from exc
    if len(key) < 32:
        raise PrincipalAssertionError("principal_assertion_key_invalid")
    return key


def _external_regular_file(
    path: str | Path,
    *,
    code: str,
    manager: ResearchPathManager,
    secret: bool,
    maximum_bytes: int,
) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise PrincipalAssertionError(f"{code}_absolute_path_required")
    if candidate.is_symlink():
        raise PrincipalAssertionError(f"{code}_symlink_forbidden")
    try:
        resolved = candidate.resolve(strict=True)
        status = resolved.stat()
    except OSError as exc:
        raise PrincipalAssertionError(f"{code}_invalid") from exc
    if not resolved.is_file() or status.st_size <= 0 or status.st_size > maximum_bytes:
        raise PrincipalAssertionError(f"{code}_invalid")
    if secret:
        # Python exposes portable POSIX mode semantics only on POSIX hosts.
        # Refuse secret-key loading elsewhere until the hosting adapter can
        # supply an ACL verifier with equivalent fail-closed guarantees.
        if os.name != "posix":
            raise PrincipalAssertionError(f"{code}_permissions_unverifiable")
        if stat.S_IMODE(status.st_mode) & 0o077:
            raise PrincipalAssertionError(f"{code}_permissions_too_open")
    forbidden_roots = [manager.project_root]
    if secret:
        forbidden_roots.extend(
            (
                manager.data_root,
                manager.artifact_root,
                manager.report_root,
                manager.cache_root,
            )
        )
    if any(ResearchPathManager.is_within(resolved, root) for root in forbidden_roots):
        raise PrincipalAssertionError(f"{code}_must_be_external")
    # These checks deliberately do not claim to protect a path whose containing
    # directory can be modified by an untrusted local principal between stat()
    # and read(). Operators must keep the trust store, key, and parent
    # directories under the identity authority's exclusive ownership.
    return resolved


def _signature(key: bytes, payload: Mapping[str, Any]) -> str:
    if not isinstance(key, bytes) or len(key) < 32:
        raise PrincipalAssertionError("principal_assertion_key_invalid")
    digest = hmac.new(
        key,
        _SIGNATURE_DOMAIN + canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    return f"hmac-sha256:{digest}"


def _timestamp(value: str, code: str) -> datetime:
    if not isinstance(value, str):
        raise PrincipalAssertionError(code)
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PrincipalAssertionError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PrincipalAssertionError(code)
    return parsed


__all__ = [
    "INDEPENDENT_VERIFICATION_ASSERTION_AUDIENCE",
    "INDEPENDENT_VERIFICATION_ASSERTION_PURPOSE",
    "INDEPENDENT_VERIFIER_ROLE",
    "IndependentVerificationAssertionScope",
    "PrincipalAssertion",
    "PrincipalAssertionError",
    "issue_principal_assertion",
    "load_principal_assertion",
    "verify_principal_assertion",
]
