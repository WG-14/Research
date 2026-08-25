"""Administrator-pinned public-key assertions for independent verification.

The research runtime verifies assertions but never loads an issuer private key.
Private-key custody and issuance belong to an external identity authority.  The
issuance helper accepts an already-held private-key object only for external
issuer adapters and test fixtures.
"""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from market_research.paths import ResearchPathManager

from .hashing import canonical_json_bytes, sha256_prefixed


PRINCIPAL_ASSERTION_SCHEMA_VERSION = 2
PRINCIPAL_ASSERTION_ALGORITHM = "ed25519"
PRINCIPAL_TRUST_STORE_SCHEMA_VERSION = 1
INDEPENDENT_VERIFICATION_ASSERTION_AUDIENCE = "market-research:independent-verification"
INDEPENDENT_VERIFICATION_ASSERTION_PURPOSE = (
    "research-reproduce-run:independent-verification-publication"
)
INDEPENDENT_VERIFIER_ROLE = "independent_verifier"
MAXIMUM_PRINCIPAL_ASSERTION_LIFETIME_SECONDS = 28_800
OPERATED_INDEPENDENT_VERIFIER_TRUST_STORE_PATH = Path(
    "/etc/research-ops/independent-verifier-trust.json"
)
OPERATED_INDEPENDENT_VERIFIER_PUBLIC_KEY_ROOT = Path(
    "/etc/research-ops/independent-verifier-keys"
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$")
_RESEARCH_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SIGNATURE_DOMAIN = b"market-research:principal-assertion:v2\x00"
_MAXIMUM_TRUST_STORE_BYTES = 256 * 1024
_MAXIMUM_PUBLIC_KEY_BYTES = 256
_TRUST_STORE_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_type",
        "authority_id",
        "issued_at",
        "expires_at",
        "keys",
    }
)
_TRUST_KEY_FIELDS = frozenset(
    {
        "key_id",
        "algorithm",
        "public_key_path",
        "public_key_content_hash",
        "valid_from",
        "valid_until",
        "revoked_at",
        "revocation_reason",
    }
)


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
        _decode_signature(self.signature)
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
            # Schema v1 used a symmetric runtime secret.  It is deliberately
            # rejected rather than silently translated to the public-key
            # authority contract.
            raise PrincipalAssertionError("principal_assertion_schema_unsupported")
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
    private_key: Ed25519PrivateKey,
) -> PrincipalAssertion:
    """Issue one assertion for an external identity adapter or test issuer.

    The production runtime never calls this function and never reads a private
    key file.  File-path based private-key loading is intentionally unsupported.
    """

    if not isinstance(private_key, Ed25519PrivateKey):
        raise PrincipalAssertionError("principal_assertion_private_key_invalid")
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
    signature = _signature(
        private_key,
        {**material, "content_hash": content_hash},
    )
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
    raw = _read_pinned_file(
        path,
        code="principal_assertion_file",
        manager=manager,
        maximum_bytes=65_536,
        operated=False,
        expected_hash=None,
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PrincipalAssertionError("principal_assertion_file_invalid") from exc
    if not isinstance(payload, Mapping):
        raise PrincipalAssertionError("principal_assertion_file_invalid")
    return PrincipalAssertion.from_dict(payload)


@dataclass(frozen=True, slots=True)
class _TrustedPublicKey:
    key_id: str
    public_key_path: Path
    public_key_content_hash: str
    valid_from: datetime
    valid_until: datetime
    revoked_at: datetime | None
    public_key: Ed25519PublicKey


@dataclass(frozen=True, slots=True)
class _PrincipalTrustStore:
    authority_id: str
    issued_at: datetime
    expires_at: datetime
    keys: tuple[_TrustedPublicKey, ...]


def verify_principal_assertion(
    *,
    assertion: PrincipalAssertion,
    expected_scope: IndependentVerificationAssertionScope,
    trust_store_path: str | Path | None,
    manager: ResearchPathManager,
    now: datetime | None = None,
) -> PrincipalAssertion:
    """Authenticate a scope with the sole administrator-pinned public authority."""

    if assertion.scope != expected_scope:
        raise PrincipalAssertionError("principal_assertion_scope_mismatch")
    current = _aware_utc(
        now or datetime.now(timezone.utc),
        "principal_assertion_now_timezone_required",
    )
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
    configured_path = manager.settings.independent_verifier_trust_store_path
    configured_hash = manager.settings.independent_verifier_trust_store_hash
    if configured_path is None or configured_hash is None:
        raise PrincipalAssertionError(
            "principal_assertion_administrator_pinned_trust_required"
        )
    if trust_store_path is None or _lexical_path(trust_store_path) != _lexical_path(
        configured_path
    ):
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_caller_override_forbidden"
        )
    operated = _operated_runtime()
    if operated and _lexical_path(configured_path) != (
        OPERATED_INDEPENDENT_VERIFIER_TRUST_STORE_PATH
    ):
        raise PrincipalAssertionError(
            "principal_assertion_operated_trust_store_path_not_pinned"
        )
    store = _load_trust_store(
        path=configured_path,
        expected_hash=configured_hash,
        manager=manager,
        now=current,
        operated=operated,
    )
    if assertion.issuer != store.authority_id:
        raise PrincipalAssertionError("principal_assertion_trusted_key_not_found")
    matches = [key for key in store.keys if key.key_id == assertion.key_id]
    if len(matches) != 1:
        raise PrincipalAssertionError("principal_assertion_trusted_key_not_found")
    trusted = matches[0]
    if trusted.revoked_at is not None:
        raise PrincipalAssertionError("principal_assertion_key_revoked")
    if (
        authenticated_at < trusted.valid_from
        or expires_at > trusted.valid_until
        or current < trusted.valid_from
        or current > trusted.valid_until
    ):
        raise PrincipalAssertionError("principal_assertion_key_outside_validity")
    try:
        trusted.public_key.verify(
            _decode_signature(assertion.signature),
            _SIGNATURE_DOMAIN + canonical_json_bytes(assertion.signed_material()),
        )
    except InvalidSignature as exc:
        raise PrincipalAssertionError("principal_assertion_signature_invalid") from exc
    return assertion


def _load_trust_store(
    *,
    path: Path,
    expected_hash: str,
    manager: ResearchPathManager,
    now: datetime,
    operated: bool,
) -> _PrincipalTrustStore:
    raw = _read_pinned_file(
        path,
        expected_hash=_sha256(
            expected_hash,
            "principal_assertion_trust_store_hash_invalid",
        ),
        code="principal_assertion_trust_store",
        manager=manager,
        maximum_bytes=_MAXIMUM_TRUST_STORE_BYTES,
        operated=operated,
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrincipalAssertionError("principal_assertion_trust_store_invalid") from exc
    if not isinstance(payload, dict) or set(payload) != _TRUST_STORE_FIELDS:
        raise PrincipalAssertionError("principal_assertion_trust_store_schema_invalid")
    if raw != canonical_json_bytes(payload) + b"\n":
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_not_canonical_json"
        )
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != PRINCIPAL_TRUST_STORE_SCHEMA_VERSION
    ):
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_schema_unsupported"
        )
    if payload.get("artifact_type") != "independent_verifier_trust_store":
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_artifact_type_unsupported"
        )
    authority_id = _identifier(
        payload.get("authority_id"),
        "principal_assertion_trust_store_authority_id_invalid",
    )
    issued_at = _trust_timestamp(
        payload.get("issued_at"),
        "principal_assertion_trust_store_issued_at_invalid",
    )
    expires_at = _trust_timestamp(
        payload.get("expires_at"),
        "principal_assertion_trust_store_expires_at_invalid",
    )
    if expires_at <= issued_at:
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_validity_invalid"
        )
    if now < issued_at:
        raise PrincipalAssertionError("principal_assertion_trust_store_not_yet_valid")
    if now > expires_at:
        raise PrincipalAssertionError("principal_assertion_trust_store_expired")
    raw_keys = payload.get("keys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise PrincipalAssertionError("principal_assertion_trust_store_keys_required")
    keys = tuple(
        _parse_trusted_key(item, manager=manager, operated=operated)
        for item in raw_keys
    )
    identities = [(key.key_id, str(key.public_key_path)) for key in keys]
    if identities != sorted(identities):
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_keys_not_canonical_order"
        )
    if len({key.key_id for key in keys}) != len(keys):
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_key_id_duplicate"
        )
    if len({key.public_key_path for key in keys}) != len(keys) or len(
        {key.public_key_content_hash for key in keys}
    ) != len(keys):
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_public_key_rebound"
        )
    return _PrincipalTrustStore(
        authority_id=authority_id,
        issued_at=issued_at,
        expires_at=expires_at,
        keys=keys,
    )


def _parse_trusted_key(
    payload: Any,
    *,
    manager: ResearchPathManager,
    operated: bool,
) -> _TrustedPublicKey:
    if not isinstance(payload, dict) or set(payload) != _TRUST_KEY_FIELDS:
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_key_schema_invalid"
        )
    key_id = _identifier(
        payload.get("key_id"),
        "principal_assertion_trust_store_key_id_invalid",
    )
    if payload.get("algorithm") != PRINCIPAL_ASSERTION_ALGORITHM:
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_key_algorithm_unsupported"
        )
    public_key_path = _normalized_absolute_path(
        payload.get("public_key_path"),
        "principal_assertion_trust_store_public_key_path_invalid",
    )
    if operated and public_key_path.parent != (
        OPERATED_INDEPENDENT_VERIFIER_PUBLIC_KEY_ROOT
    ):
        raise PrincipalAssertionError(
            "principal_assertion_operated_public_key_path_not_pinned"
        )
    public_key_content_hash = _sha256(
        payload.get("public_key_content_hash"),
        "principal_assertion_trust_store_public_key_hash_invalid",
    )
    valid_from = _trust_timestamp(
        payload.get("valid_from"),
        "principal_assertion_trust_store_key_valid_from_invalid",
    )
    valid_until = _trust_timestamp(
        payload.get("valid_until"),
        "principal_assertion_trust_store_key_valid_until_invalid",
    )
    if valid_until <= valid_from:
        raise PrincipalAssertionError(
            "principal_assertion_trust_store_key_validity_invalid"
        )
    raw_revoked_at = payload.get("revoked_at")
    raw_reason = payload.get("revocation_reason")
    if raw_revoked_at is None:
        if raw_reason != "":
            raise PrincipalAssertionError(
                "principal_assertion_trust_store_key_revocation_invalid"
            )
        revoked_at = None
    else:
        revoked_at = _trust_timestamp(
            raw_revoked_at,
            "principal_assertion_trust_store_key_revoked_at_invalid",
        )
        if (
            revoked_at < valid_from
            or revoked_at > valid_until
            or not isinstance(raw_reason, str)
            or not raw_reason.strip()
        ):
            raise PrincipalAssertionError(
                "principal_assertion_trust_store_key_revocation_invalid"
            )
    raw_key = _read_pinned_file(
        public_key_path,
        expected_hash=public_key_content_hash,
        manager=manager,
        code="principal_assertion_trust_public_key",
        maximum_bytes=_MAXIMUM_PUBLIC_KEY_BYTES,
        operated=operated,
    )
    return _TrustedPublicKey(
        key_id=key_id,
        public_key_path=public_key_path,
        public_key_content_hash=public_key_content_hash,
        valid_from=valid_from,
        valid_until=valid_until,
        revoked_at=revoked_at,
        public_key=_parse_public_key(raw_key),
    )


def _read_pinned_file(
    path: str | Path,
    *,
    expected_hash: str | None,
    manager: ResearchPathManager,
    code: str,
    maximum_bytes: int,
    operated: bool,
) -> bytes:
    """Read exact bytes through no-follow file and pinned parent descriptors."""

    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        raise PrincipalAssertionError(f"{code}_permissions_unverifiable")
    candidate = _normalized_absolute_path(path, f"{code}_path_invalid")
    forbidden_roots = (
        manager.project_root,
        manager.data_root,
        manager.artifact_root,
        manager.report_root,
        manager.cache_root,
    )
    if any(ResearchPathManager.is_within(candidate, root) for root in forbidden_roots):
        raise PrincipalAssertionError(f"{code}_must_be_external_to_runtime_state")
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    identities: list[tuple[int, int, int, int, int]] = []
    file_descriptor = -1
    raw = b""
    try:
        current = os.open(candidate.parts[0], directory_flags)
        descriptors.append(current)
        for component in candidate.parts[1:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
        for descriptor in descriptors:
            status = os.fstat(descriptor)
            if not stat.S_ISDIR(status.st_mode):
                raise PrincipalAssertionError(f"{code}_parent_invalid")
            if operated and (
                status.st_uid != 0
                or status.st_gid != 0
                or stat.S_IMODE(status.st_mode) & 0o022
            ):
                raise PrincipalAssertionError(
                    f"{code}_administrator_parent_required"
                )
            identities.append(_directory_identity(status))
        file_descriptor = os.open(
            candidate.parts[-1],
            file_flags,
            dir_fd=descriptors[-1],
        )
        before = os.fstat(file_descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > maximum_bytes
        ):
            raise PrincipalAssertionError(f"{code}_regular_single_link_required")
        if mode & 0o022:
            raise PrincipalAssertionError(f"{code}_permissions_too_open")
        if operated and (
            before.st_uid != 0 or before.st_gid != 0 or mode != 0o644
        ):
            raise PrincipalAssertionError(f"{code}_administrator_owner_required")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(file_descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(file_descriptor)
        path_status = os.stat(
            candidate.parts[-1],
            dir_fd=descriptors[-1],
            follow_symlinks=False,
        )
        if (
            len(raw) > maximum_bytes
            or _pinned_identity(before) != _pinned_identity(after)
            or _pinned_identity(before) != _pinned_identity(path_status)
            or any(
                _directory_identity(os.fstat(descriptor)) != identity
                for descriptor, identity in zip(descriptors, identities, strict=True)
            )
        ):
            raise PrincipalAssertionError(f"{code}_changed_during_verification")
    except PrincipalAssertionError:
        raise
    except OSError as exc:
        raise PrincipalAssertionError(f"{code}_unavailable_or_symlinked") from exc
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    if expected_hash is not None and (
        f"sha256:{hashlib.sha256(raw).hexdigest()}" != expected_hash
    ):
        raise PrincipalAssertionError(f"{code}_content_hash_mismatch")
    return raw


def _signature(
    private_key: Ed25519PrivateKey,
    payload: Mapping[str, Any],
) -> str:
    raw = private_key.sign(_SIGNATURE_DOMAIN + canonical_json_bytes(payload))
    return "ed25519:" + base64.b64encode(raw).decode("ascii")


def _decode_signature(value: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("ed25519:"):
        raise PrincipalAssertionError("principal_assertion_signature_invalid")
    try:
        encoded = value.removeprefix("ed25519:").encode("ascii", errors="strict")
        decoded = base64.b64decode(encoded, validate=True)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise PrincipalAssertionError("principal_assertion_signature_invalid") from exc
    if len(decoded) != 64 or base64.b64encode(decoded) != encoded:
        raise PrincipalAssertionError("principal_assertion_signature_invalid")
    return decoded


def _parse_public_key(raw: bytes) -> Ed25519PublicKey:
    if not raw.startswith(b"ed25519:") or not raw.endswith(b"\n"):
        raise PrincipalAssertionError("principal_assertion_public_key_invalid")
    encoded = raw[len(b"ed25519:") : -1]
    if b"\n" in encoded or b"\r" in encoded:
        raise PrincipalAssertionError("principal_assertion_public_key_invalid")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise PrincipalAssertionError("principal_assertion_public_key_invalid") from exc
    if len(decoded) != 32 or base64.b64encode(decoded) != encoded:
        raise PrincipalAssertionError("principal_assertion_public_key_invalid")
    try:
        return Ed25519PublicKey.from_public_bytes(decoded)
    except ValueError as exc:
        raise PrincipalAssertionError("principal_assertion_public_key_invalid") from exc


def _timestamp(value: str, code: str) -> datetime:
    if not isinstance(value, str):
        raise PrincipalAssertionError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PrincipalAssertionError(code) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PrincipalAssertionError(code)
    return parsed


def _trust_timestamp(value: Any, code: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PrincipalAssertionError(code)
    parsed = _timestamp(value, code)
    if parsed.utcoffset() != timedelta(0):
        raise PrincipalAssertionError(code)
    return parsed.astimezone(timezone.utc)


def _aware_utc(value: datetime, code: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PrincipalAssertionError(code)
    return value.astimezone(timezone.utc)


def _identifier(value: Any, code: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise PrincipalAssertionError(code)
    return value


def _sha256(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PrincipalAssertionError(code)
    return value


def _normalized_absolute_path(value: Any, code: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise PrincipalAssertionError(code)
    path = Path(value).expanduser()
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        raise PrincipalAssertionError(code)
    return path


def _lexical_path(value: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(value).expanduser())))


def _operated_runtime() -> bool:
    return os.environ.get("RESEARCH_RUNTIME_PROFILE", "").strip().lower() == "operated"


def _pinned_identity(status: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _directory_identity(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_uid,
        status.st_gid,
    )


__all__ = [
    "INDEPENDENT_VERIFICATION_ASSERTION_AUDIENCE",
    "INDEPENDENT_VERIFICATION_ASSERTION_PURPOSE",
    "INDEPENDENT_VERIFIER_ROLE",
    "OPERATED_INDEPENDENT_VERIFIER_PUBLIC_KEY_ROOT",
    "OPERATED_INDEPENDENT_VERIFIER_TRUST_STORE_PATH",
    "IndependentVerificationAssertionScope",
    "PrincipalAssertion",
    "PrincipalAssertionError",
    "issue_principal_assertion",
    "load_principal_assertion",
    "verify_principal_assertion",
]
