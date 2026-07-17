#!/usr/bin/env python3
"""Standalone cryptographic verification for native backup evidence."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_ROLE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_OFFSITE_FIELDS = {
    "schema_version",
    "status",
    "backup_id",
    "target_id",
    "encrypted",
    "encryption",
    "encryption_key_id",
    "manifest_hash",
    "remote_object_digest",
    "remote_object_version",
    "uploaded_at",
    "receipt_signature",
}
_REQUIRED_BACKUP_ROLES = {
    "postgresql",
    "data",
    "manifest",
    "artifact",
    "report",
    "identity_registry",
}


class EvidenceError(RuntimeError):
    """Fail-closed evidence error with a stable non-secret reason code."""


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode("ascii")


def _regular_file(path: Path, code: str, *, maximum: int) -> os.stat_result:
    try:
        if not path.is_absolute() or path.resolve(strict=True) != path.absolute():
            raise OSError
        link_status = path.lstat()
        status = path.stat()
    except OSError as exc:
        raise EvidenceError(code) from exc
    if (
        stat.S_ISLNK(link_status.st_mode)
        or not stat.S_ISREG(status.st_mode)
        or status.st_size < 1
        or status.st_size > maximum
    ):
        raise EvidenceError(code)
    return status


def _read_file(path: Path, code: str, *, maximum: int) -> bytes:
    _regular_file(path, code, maximum=maximum)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            payload = handle.read(maximum + 1)
    except OSError as exc:
        raise EvidenceError(code) from exc
    if not payload or len(payload) > maximum:
        raise EvidenceError(code)
    return payload


def trusted_public_key(path: Path) -> Path:
    if not path.is_absolute():
        raise EvidenceError("verification_key")
    status = _regular_file(path, "verification_key", maximum=65_536)
    if status.st_uid not in {0, os.geteuid()} or stat.S_IMODE(status.st_mode) & 0o022:
        raise EvidenceError("verification_key")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise EvidenceError("verification_key") from exc
    completed = _run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-pubin",
            "-in",
            str(resolved),
            "-text_pub",
            "-noout",
        ],
        capture_stdout=True,
    )
    description = completed.stdout or b""
    supported = description.startswith(b"ED25519 Public-Key:\n") or (
        description.startswith(b"Public-Key: (") and b"\nModulus:\n" in description
    )
    if completed.returncode != 0 or not supported:
        raise EvidenceError("verification_key")
    return resolved


def _run(
    arguments: list[str], *, capture_stdout: bool = False
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            arguments,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceError("openssl") from exc


def verify_signature(payload: bytes, signature: bytes, public_key: Path) -> None:
    """Verify RSA/SHA-256 or Ed25519 signatures with an installed trusted key."""

    key = trusted_public_key(public_key)
    if not signature or len(signature) > 16_384:
        raise EvidenceError("signature")
    with tempfile.TemporaryDirectory(prefix="research-evidence-verification-") as name:
        temporary = Path(name)
        payload_path = temporary / "payload"
        signature_path = temporary / "signature"
        payload_path.write_bytes(payload)
        signature_path.write_bytes(signature)
        rsa = _run(
            [
                "/usr/bin/openssl",
                "dgst",
                "-sha256",
                "-verify",
                str(key),
                "-signature",
                str(signature_path),
                str(payload_path),
            ],
        )
        if rsa.returncode == 0:
            return
        ed25519 = _run(
            [
                "/usr/bin/openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(key),
                "-sigfile",
                str(signature_path),
                "-rawin",
                "-in",
                str(payload_path),
            ],
        )
        if ed25519.returncode != 0:
            raise EvidenceError("signature")


def read_offsite_receipt(path: Path) -> dict[str, object]:
    status = _regular_file(path, "receipt_file", maximum=65_536)
    if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) & 0o077:
        raise EvidenceError("receipt_file")
    payload = _read_file(path, "receipt_file", maximum=65_536)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("receipt_json") from exc
    if not isinstance(value, dict) or set(value) != _OFFSITE_FIELDS:
        raise EvidenceError("receipt_shape")
    return value


def _text(value: object, code: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise EvidenceError(code)
    return value


def _decode_embedded_signature(value: object) -> bytes:
    encoded = _text(value, "receipt_signature", maximum=22_000)
    if not encoded.startswith("base64:"):
        raise EvidenceError("receipt_signature")
    try:
        signature = base64.b64decode(encoded.removeprefix("base64:"), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise EvidenceError("receipt_signature") from exc
    if not signature or len(signature) > 16_384:
        raise EvidenceError("receipt_signature")
    return signature


def verify_offsite_receipt(
    receipt: dict[str, object],
    *,
    public_key: Path,
    backup_id: str,
    manifest_hash: str,
    target_id: str,
    encryption: str,
    encryption_key_id: str,
    now: datetime | None = None,
    maximum_age: timedelta | None = timedelta(days=1),
) -> datetime:
    expected = {
        "schema_version": 1,
        "status": "VERIFIED",
        "backup_id": backup_id,
        "target_id": target_id,
        "encrypted": True,
        "encryption": encryption,
        "encryption_key_id": encryption_key_id,
    }
    if not _UUID.fullmatch(backup_id) or any(
        receipt.get(key) != value for key, value in expected.items()
    ):
        raise EvidenceError("binding")
    observed_manifest = _text(receipt.get("manifest_hash"), "manifest_hash")
    remote_digest = _text(receipt.get("remote_object_digest"), "remote_digest")
    if (
        not _DIGEST.fullmatch(observed_manifest)
        or not _DIGEST.fullmatch(remote_digest)
        or observed_manifest != manifest_hash
    ):
        raise EvidenceError("manifest_binding")
    _text(receipt.get("remote_object_version"), "remote_object_version")
    uploaded_at = _text(receipt.get("uploaded_at"), "uploaded_at")
    try:
        uploaded = datetime.fromisoformat(uploaded_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError("uploaded_at") from exc
    if uploaded.tzinfo is None:
        raise EvidenceError("uploaded_at")
    uploaded = uploaded.astimezone(UTC)
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    if uploaded > observed_at + timedelta(minutes=5) or (
        maximum_age is not None and uploaded < observed_at - maximum_age
    ):
        raise EvidenceError("uploaded_at_window")
    unsigned = dict(receipt)
    signature = _decode_embedded_signature(unsigned.pop("receipt_signature"))
    verify_signature(canonical_json(unsigned), signature, public_key)
    return uploaded


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise EvidenceError("manifest_file_path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise EvidenceError("manifest_file_path")
    if path.as_posix() != value:
        raise EvidenceError("manifest_file_path")
    return value


def _hash_file(path: Path) -> tuple[str, int]:
    _regular_file(path, "backup_file", maximum=2**63 - 1)
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
                size += len(block)
    except OSError as exc:
        raise EvidenceError("backup_file") from exc
    return "sha256:" + digest.hexdigest(), size


def verify_backup_directory(
    backup_directory: Path,
    *,
    public_key: Path,
    backup_id: str,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Verify the signed manifest, every bound file, and CLI verification marker."""

    manifest_path = backup_directory / "manifest.json"
    signature_path = backup_directory / "manifest.sig"
    payload = _read_file(manifest_path, "manifest", maximum=2 * 1024 * 1024)
    signature = _read_file(signature_path, "manifest_signature", maximum=16_384)
    verify_signature(payload, signature, public_key)
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("manifest_json") from exc
    schema = manifest.get("schema_version") if isinstance(manifest, dict) else None
    fields = {
        "schema_version",
        "backup_id",
        "created_at",
        "release_id",
        "build_digest",
        "migration_digest",
        "postgresql_major",
        "fence",
        "audit",
        "files",
    }
    if schema == 2:
        fields.update({"git_sha", "release_bundle_digest"})
    if (
        schema not in {1, 2}
        or not isinstance(manifest, dict)
        or set(manifest) != fields
        or payload != canonical_json(manifest)
        or manifest.get("backup_id") != backup_id
    ):
        raise EvidenceError("manifest_shape")
    for field in ("build_digest", "migration_digest"):
        if not _DIGEST.fullmatch(str(manifest.get(field) or "")):
            raise EvidenceError("manifest_shape")
    if (
        not isinstance(manifest.get("release_id"), str)
        or not 1 <= len(manifest["release_id"]) <= 128
        or any(ord(character) < 32 for character in manifest["release_id"])
    ):
        raise EvidenceError("manifest_shape")
    postgresql_major = manifest.get("postgresql_major")
    if (
        isinstance(postgresql_major, bool)
        or not isinstance(postgresql_major, int)
        or not 12 <= postgresql_major <= 30
    ):
        raise EvidenceError("manifest_shape")
    if schema == 2 and (
        not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("git_sha") or ""))
        or not _DIGEST.fullmatch(str(manifest.get("release_bundle_digest") or ""))
    ):
        raise EvidenceError("manifest_shape")
    fence = manifest.get("fence")
    if (
        not isinstance(fence, dict)
        or set(fence) != {"generation", "token_hash"}
        or isinstance(fence.get("generation"), bool)
        or not isinstance(fence.get("generation"), int)
        or fence["generation"] < 1
        or not _DIGEST.fullmatch(str(fence.get("token_hash") or ""))
    ):
        raise EvidenceError("manifest_shape")
    audit = manifest.get("audit")
    if (
        not isinstance(audit, dict)
        or set(audit)
        != {"status", "row_count", "terminal_hash", "segmented_stream_required"}
        or audit.get("status") != "PASS"
        or audit.get("segmented_stream_required") is not True
        or isinstance(audit.get("row_count"), bool)
        or not isinstance(audit.get("row_count"), int)
        or audit["row_count"] < 0
        or (audit["row_count"] == 0 and audit.get("terminal_hash") is not None)
        or (
            audit["row_count"] > 0
            and not _DIGEST.fullmatch(str(audit.get("terminal_hash") or ""))
        )
    ):
        raise EvidenceError("manifest_shape")
    try:
        created_at = datetime.fromisoformat(
            str(manifest.get("created_at") or "").replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise EvidenceError("manifest_created_at") from exc
    if created_at.tzinfo is None:
        raise EvidenceError("manifest_created_at")
    created_at = created_at.astimezone(UTC)
    if created_at > (now or datetime.now(UTC)).astimezone(UTC) + timedelta(minutes=5):
        raise EvidenceError("manifest_created_at")

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= 32:
        raise EvidenceError("manifest_files")
    observed_roles: set[str] = set()
    observed_paths: set[str] = set()
    normalized: list[tuple[str, str]] = []
    for record in raw_files:
        if not isinstance(record, dict) or set(record) != {
            "role",
            "relative_path",
            "size_bytes",
            "sha256",
            "snapshot_id",
        }:
            raise EvidenceError("manifest_file_record")
        role = str(record.get("role") or "")
        relative = _safe_relative(record.get("relative_path"))
        size = record.get("size_bytes")
        expected_hash = str(record.get("sha256") or "")
        if (
            not _ROLE.fullmatch(role)
            or role in observed_roles
            or relative in observed_paths
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not _DIGEST.fullmatch(expected_hash)
            or record.get("snapshot_id") != expected_hash
        ):
            raise EvidenceError("manifest_file_record")
        candidate = backup_directory.joinpath(*PurePosixPath(relative).parts)
        try:
            current = backup_directory
            for part in PurePosixPath(relative).parts:
                current /= part
                if current.is_symlink():
                    raise OSError
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(backup_directory.resolve(strict=True))
        except (OSError, RuntimeError, ValueError) as exc:
            raise EvidenceError("backup_file") from exc
        actual_hash, actual_size = _hash_file(resolved)
        if actual_hash != expected_hash or actual_size != size:
            raise EvidenceError("backup_file_hash")
        observed_roles.add(role)
        observed_paths.add(relative)
        normalized.append((role, relative))
    if not _REQUIRED_BACKUP_ROLES.issubset(observed_roles) or normalized != sorted(
        normalized
    ):
        raise EvidenceError("manifest_files")

    manifest_hash = "sha256:" + hashlib.sha256(payload).hexdigest()
    verification_payload = _read_file(
        backup_directory / "verification.json",
        "verification",
        maximum=65_536,
    )
    try:
        verification = json.loads(verification_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("verification") from exc
    if (
        not isinstance(verification, dict)
        or verification.get("status") != "PASS"
        or verification.get("backup_id") != backup_id
        or verification.get("manifest_hash") != manifest_hash
    ):
        raise EvidenceError("verification")
    return manifest_hash, created_at


__all__ = [
    "EvidenceError",
    "canonical_json",
    "read_offsite_receipt",
    "verify_backup_directory",
    "verify_offsite_receipt",
]
