#!/usr/bin/env python3
"""Fail-closed deployment checks for the official native service profile."""

from __future__ import annotations

import grp
import hashlib
import hmac
import ipaddress
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_MIGRATION = re.compile(r"^[0-9]{4}_[A-Za-z0-9_]+$")
_RELEASE_MANIFEST_FIELDS = {
    "schema_version",
    "release_id",
    "git_sha",
    "components",
    "migrations",
    "migration_digest",
    "lock_digest",
    "deployment_digest",
    "artifacts",
    "build_digest",
    "release_bundle_digest",
}
_ARTIFACT_LABELS = {
    f"{component}-{kind}"
    for component in ("core", "web", "operations")
    for kind in ("wheel", "sdist")
}
_OWNER_KEYS = (
    "RESEARCH_OPS_SERVICE_OWNER",
    "RESEARCH_OPS_SECURITY_OWNER",
    "RESEARCH_OPS_DATA_OWNER",
    "RESEARCH_OPS_ON_CALL_OWNER",
    "RESEARCH_OPS_INCIDENT_COMMANDER",
    "RESEARCH_OPS_BACKUP_OWNER",
    "RESEARCH_OPS_RECOVERY_APPROVER",
)
_PLACEHOLDERS = ("SET_REQUIRED", "REPLACE", "CHANGEME", "UNASSIGNED", "TODO")
_PREFLIGHT_RECEIPT = Path("/run/research-operations-preflight/observation.json")
_OPERATIONS_PROJECT_ROOT = Path("services/research_operations")
_DEPLOYMENT_MARKER = Path("deploy/OFFICIAL_DEPLOYMENT")
_DEPLOYMENT_TREES = (Path("deploy/native"), Path("scripts"))


class PreflightError(RuntimeError):
    """Stable, secret-free deployment rejection."""


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise PreflightError(f"required:{key}")
    return value


def _assigned(env: Mapping[str, str], key: str) -> str:
    value = _required(env, key)
    normalized = value.upper()
    if any(marker in normalized for marker in _PLACEHOLDERS):
        raise PreflightError(f"placeholder:{key}")
    if any(ord(character) < 32 for character in value) or len(value) > 255:
        raise PreflightError(f"invalid:{key}")
    return value


def _bounded_integer(
    env: Mapping[str, str], key: str, minimum: int, maximum: int
) -> int:
    raw = _required(env, key)
    if not raw.isascii() or not raw.isdecimal():
        raise PreflightError(f"invalid:{key}")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise PreflightError(f"invalid:{key}")
    return value


def _absolute(env: Mapping[str, str], key: str) -> Path:
    path = Path(_required(env, key))
    if not path.is_absolute():
        raise PreflightError(f"absolute_path_required:{key}")
    return path


def _regular_file(path: Path, code: str) -> os.stat_result:
    try:
        link_status = path.lstat()
        status = path.stat()
    except OSError as error:
        raise PreflightError(f"file_unavailable:{code}") from error
    if stat.S_ISLNK(link_status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise PreflightError(f"file_invalid:{code}")
    if stat.S_IMODE(status.st_mode) & 0o022:
        raise PreflightError(f"file_writable_by_untrusted:{code}")
    return status


def _public_file(path: Path, code: str, allowed_uids: set[int]) -> os.stat_result:
    status = _regular_file(path, code)
    if status.st_uid not in allowed_uids or stat.S_IMODE(status.st_mode) & 0o002:
        raise PreflightError(f"file_owner_invalid:{code}")
    return status


def _secret_for_identity(
    path: Path, code: str, identity_uid: int, identity_gid: int
) -> None:
    status = _regular_file(path, code)
    mode = stat.S_IMODE(status.st_mode)
    if status.st_uid not in {0, identity_uid}:
        raise PreflightError(f"secret_owner_invalid:{code}")
    if mode & 0o007 or mode & 0o020:
        raise PreflightError(f"secret_permissions_invalid:{code}")
    if mode & 0o040 and status.st_gid != identity_gid:
        raise PreflightError(f"secret_group_invalid:{code}")
    readable = (status.st_uid == identity_uid and bool(mode & 0o400)) or (
        status.st_gid == identity_gid and bool(mode & 0o040)
    )
    if not readable:
        raise PreflightError(f"secret_unreadable_by_service:{code}")


def _root_configuration_for_group(path: Path, code: str, group_gid: int) -> None:
    status = _regular_file(path, code)
    mode = stat.S_IMODE(status.st_mode)
    if status.st_uid != 0 or mode not in {0o600, 0o640}:
        raise PreflightError(f"configuration_permissions_invalid:{code}")
    if mode == 0o640 and status.st_gid != group_gid:
        raise PreflightError(f"configuration_group_invalid:{code}")


def _safe_directory(
    path: Path,
    code: str,
    *,
    allowed_uids: set[int],
    allow_root_symlink: bool = False,
) -> Path:
    try:
        link_status = path.lstat()
    except OSError as error:
        raise PreflightError(f"directory_unavailable:{code}") from error
    if stat.S_ISLNK(link_status.st_mode):
        if not allow_root_symlink or link_status.st_uid != 0:
            raise PreflightError(f"directory_symlink:{code}")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise PreflightError(f"directory_unavailable:{code}") from error
    else:
        resolved = path
    try:
        status = resolved.stat()
    except OSError as error:
        raise PreflightError(f"directory_unavailable:{code}") from error
    if not stat.S_ISDIR(status.st_mode) or status.st_uid not in allowed_uids:
        raise PreflightError(f"directory_owner_invalid:{code}")
    if stat.S_IMODE(status.st_mode) & 0o022:
        raise PreflightError(f"directory_writable_by_untrusted:{code}")
    return resolved


def _load_json(path: Path, code: str) -> dict[str, object]:
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 1_048_576:
            raise ValueError
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise PreflightError(f"json_invalid:{code}") from error
    if not isinstance(value, dict):
        raise PreflightError(f"json_invalid:{code}")
    return value


def _receipt_payload(
    env: Mapping[str, str], *, status: str, failure_code: str | None
) -> dict[str, object]:
    if status not in {"PASS", "FAIL"}:
        raise PreflightError("preflight_receipt_status_invalid")
    return {
        "schema_version": 1,
        "status": status,
        "checked_at": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "git_sha": env.get("RESEARCH_OPS_GIT_SHA", ""),
        "release_id": env.get("RESEARCH_OPS_RELEASE_ID", ""),
        "build_digest": env.get("RESEARCH_OPS_BUILD_DIGEST", ""),
        "release_bundle_digest": env.get("RESEARCH_OPS_RELEASE_BUNDLE_DIGEST", ""),
        "failure_code": failure_code,
    }


def _validate_receipt_contract(env: Mapping[str, str], group_gid: int) -> None:
    if _absolute(env, "RESEARCH_OPS_PREFLIGHT_RECEIPT") != _PREFLIGHT_RECEIPT:
        raise PreflightError("preflight_receipt_path_invalid")
    _bounded_integer(env, "RESEARCH_OPS_PREFLIGHT_MAX_AGE_SECONDS", 300, 172800)
    try:
        parent_status = _PREFLIGHT_RECEIPT.parent.lstat()
    except OSError as error:
        raise PreflightError("preflight_receipt_parent_invalid") from error
    if (
        stat.S_ISLNK(parent_status.st_mode)
        or not stat.S_ISDIR(parent_status.st_mode)
        or parent_status.st_uid != 0
        or parent_status.st_gid != group_gid
        or stat.S_IMODE(parent_status.st_mode) != 0o750
    ):
        raise PreflightError("preflight_receipt_parent_invalid")


def _write_receipt(
    env: Mapping[str, str], *, group_gid: int, status: str, failure_code: str | None
) -> None:
    payload = _canonical(
        _receipt_payload(env, status=status, failure_code=failure_code)
    )
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        if _PREFLIGHT_RECEIPT.exists() or _PREFLIGHT_RECEIPT.is_symlink():
            current = _PREFLIGHT_RECEIPT.lstat()
            if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
                raise PreflightError("preflight_receipt_file_invalid")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=_PREFLIGHT_RECEIPT.parent,
            prefix=".observation.",
            suffix=".json",
        )
        temporary = Path(temporary_name)
        os.fchown(descriptor, 0, group_gid)
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _PREFLIGHT_RECEIPT)
        temporary = None
        directory = os.open(_PREFLIGHT_RECEIPT.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except PreflightError:
        raise
    except OSError as error:
        raise PreflightError("preflight_receipt_write_failed") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _stable_failure_code(error: object) -> str:
    raw = str(error) if isinstance(error, PreflightError) else "identity_missing"
    return re.sub(r"[^A-Za-z0-9_.:-]", "_", raw)[:160] or "preflight_failed"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()


def _object_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path, code: str) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        payload = path.read_bytes()
    except OSError as error:
        raise PreflightError(f"release_input_invalid:{code}") from error
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _deployment_file_record(source: Path, path: Path) -> dict[str, object]:
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode):
            raise OSError
        payload = path.read_bytes()
    except OSError as error:
        raise PreflightError("release_deployment_invalid") from error
    if len(payload) != status.st_size:
        raise PreflightError("release_deployment_invalid")
    return {
        "path": path.relative_to(source).as_posix(),
        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "mode": stat.S_IMODE(status.st_mode),
    }


def _deployment_digest(source: Path) -> str:
    operations_root = source / _OPERATIONS_PROJECT_ROOT
    marker = operations_root / _DEPLOYMENT_MARKER
    records = [_deployment_file_record(source, marker)]

    for relative_tree in _DEPLOYMENT_TREES:
        tree = operations_root / relative_tree
        try:
            tree_status = tree.lstat()
            if not stat.S_ISDIR(tree_status.st_mode):
                raise OSError
            paths = sorted(tree.rglob("*"))
        except OSError as error:
            raise PreflightError("release_deployment_invalid") from error

        regular_file_count = 0
        for path in paths:
            try:
                status = path.lstat()
            except OSError as error:
                raise PreflightError("release_deployment_invalid") from error
            if stat.S_ISLNK(status.st_mode):
                raise PreflightError("release_deployment_invalid")
            if stat.S_ISDIR(status.st_mode):
                continue
            if not stat.S_ISREG(status.st_mode):
                raise PreflightError("release_deployment_invalid")
            records.append(_deployment_file_record(source, path))
            regular_file_count += 1
        if regular_file_count == 0:
            raise PreflightError("release_deployment_invalid")

    records.sort(key=lambda record: str(record["path"]))
    return _object_digest(records)


def _manifest_text(value: object, code: str, maximum: int = 255) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise PreflightError(f"release_manifest_shape:{code}")
    return value


def _validate_release_manifest_shape(manifest: dict[str, object]) -> None:
    if set(manifest) != _RELEASE_MANIFEST_FIELDS:
        raise PreflightError("release_manifest_shape:top_level")
    components = manifest.get("components")
    if not isinstance(components, dict) or set(components) != {
        "core",
        "web",
        "operations",
    }:
        raise PreflightError("release_manifest_shape:components")
    for label, value in components.items():
        if not isinstance(value, dict) or set(value) != {"distribution", "version"}:
            raise PreflightError(f"release_manifest_shape:component_{label}")
        _manifest_text(value.get("distribution"), f"component_{label}_distribution")
        _manifest_text(value.get("version"), f"component_{label}_version")

    migrations = manifest.get("migrations")
    if not isinstance(migrations, dict) or set(migrations) != {"web", "operations"}:
        raise PreflightError("release_manifest_shape:migrations")
    for label, value in migrations.items():
        if not isinstance(value, dict) or set(value) != {"count", "latest", "digest"}:
            raise PreflightError(f"release_manifest_shape:migration_{label}")
        count = value.get("count")
        latest = _manifest_text(value.get("latest"), f"migration_{label}_latest")
        digest = _manifest_text(value.get("digest"), f"migration_{label}_digest")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
            or not _MIGRATION.fullmatch(latest)
            or not _DIGEST.fullmatch(digest)
        ):
            raise PreflightError(f"release_manifest_shape:migration_{label}")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != _ARTIFACT_LABELS:
        raise PreflightError("release_manifest_shape:artifacts")
    for label, value in artifacts.items():
        if not isinstance(value, dict) or set(value) != {
            "filename",
            "sha256",
            "size_bytes",
        }:
            raise PreflightError(f"release_manifest_shape:artifact_{label}")
        filename = _manifest_text(value.get("filename"), f"artifact_{label}_filename")
        digest = _manifest_text(value.get("sha256"), f"artifact_{label}_digest")
        size = value.get("size_bytes")
        if (
            Path(filename).name != filename
            or (label.endswith("-wheel") and not filename.endswith(".whl"))
            or (label.endswith("-sdist") and not filename.endswith(".tar.gz"))
            or not _DIGEST.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 1
        ):
            raise PreflightError(f"release_manifest_shape:artifact_{label}")

    for key in (
        "migration_digest",
        "lock_digest",
        "deployment_digest",
        "build_digest",
        "release_bundle_digest",
    ):
        if not _DIGEST.fullmatch(_manifest_text(manifest.get(key), key)):
            raise PreflightError(f"release_manifest_shape:{key}")


def _validate_release_metadata(env: Mapping[str, str]) -> None:
    if _required(env, "RESEARCH_RUNTIME_PROFILE") != "operated":
        raise PreflightError("runtime_profile_invalid")
    release_id = _assigned(env, "RESEARCH_OPS_RELEASE_ID")
    git_sha = _required(env, "RESEARCH_OPS_GIT_SHA")
    build_digest = _required(env, "RESEARCH_OPS_BUILD_DIGEST")
    migration_digest = _required(env, "RESEARCH_OPS_EXPECTED_MIGRATION_DIGEST")
    lock_digest = _required(env, "RESEARCH_OPS_LOCK_DIGEST")
    deployment_digest = _required(env, "RESEARCH_OPS_DEPLOYMENT_DIGEST")
    bundle_digest = _required(env, "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST")
    if not _GIT_SHA.fullmatch(git_sha):
        raise PreflightError("release_git_sha_invalid")
    if not _DIGEST.fullmatch(build_digest):
        raise PreflightError("release_build_digest_invalid")
    if not _DIGEST.fullmatch(migration_digest):
        raise PreflightError("release_migration_digest_invalid")
    if not all(
        _DIGEST.fullmatch(value)
        for value in (lock_digest, deployment_digest, bundle_digest)
    ):
        raise PreflightError("release_evidence_digest_invalid")

    source = _safe_directory(
        _absolute(env, "RESEARCH_OPS_SOURCE_ROOT"),
        "source_root",
        allowed_uids={0},
        allow_root_symlink=True,
    )
    manifest_path = _absolute(env, "RESEARCH_OPS_RELEASE_MANIFEST")
    _public_file(manifest_path, "release_manifest", {0})
    try:
        manifest_path.resolve(strict=True).relative_to(source)
    except (OSError, ValueError) as error:
        raise PreflightError("release_manifest_outside_source") from error
    manifest = _load_json(manifest_path, "release_manifest")
    _validate_release_manifest_shape(manifest)
    expected = {
        "schema_version": 1,
        "git_sha": git_sha,
        "release_id": release_id,
        "build_digest": build_digest,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise PreflightError("release_manifest_mismatch")
    if manifest.get("migration_digest") != migration_digest:
        raise PreflightError("release_migration_digest_mismatch")
    if manifest.get("lock_digest") != lock_digest:
        raise PreflightError("release_lock_digest_mismatch")
    if manifest.get("deployment_digest") != deployment_digest:
        raise PreflightError("release_deployment_digest_mismatch")
    if manifest.get("release_bundle_digest") != bundle_digest:
        raise PreflightError("release_bundle_digest_mismatch")
    if _object_digest(manifest.get("migrations")) != migration_digest:
        raise PreflightError("release_migration_digest_invalid")
    if _object_digest(manifest.get("artifacts")) != build_digest:
        raise PreflightError("release_build_digest_invalid")
    unsigned = dict(manifest)
    unsigned.pop("release_bundle_digest")
    if _object_digest(unsigned) != bundle_digest:
        raise PreflightError("release_bundle_digest_invalid")
    if _file_digest(source / "uv.lock", "lock") != lock_digest:
        raise PreflightError("release_lock_digest_invalid")
    if _deployment_digest(source) != deployment_digest:
        raise PreflightError("release_deployment_digest_invalid")


def _validate_owner_assignments(env: Mapping[str, str]) -> None:
    for key in _OWNER_KEYS:
        _assigned(env, key)
    if env["RESEARCH_OPS_BACKUP_OWNER"] == env["RESEARCH_OPS_RECOVERY_APPROVER"]:
        raise PreflightError("backup_recovery_duties_not_separated")
    if env["RESEARCH_OPS_SERVICE_OWNER"] == env["RESEARCH_OPS_SECURITY_OWNER"]:
        raise PreflightError("service_security_duties_not_separated")


def _validate_native_path_contracts(env: Mapping[str, str]) -> None:
    expected = {
        "RESEARCH_OPS_SOURCE_ROOT": "/opt/research-platform/current",
        "RESEARCH_OPS_RELEASE_MANIFEST": "/opt/research-platform/current/release.json",
        "RESEARCH_DATA_ROOT": "/srv/research/data",
        "RESEARCH_ARTIFACT_ROOT": "/srv/research/artifacts",
        "RESEARCH_REPORT_ROOT": "/srv/research/reports",
        "RESEARCH_CACHE_ROOT": "/srv/research/cache",
        "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH": (
            "/srv/research/registry/research_validate_experiment_identity.jsonl"
        ),
        "INTERNAL_WEB_STATIC_ROOT": ("/srv/research/artifacts/_internal_web/static"),
        "RESEARCH_OPS_FILESYSTEM_QUALIFICATION_RECEIPT": (
            "/etc/research-ops/filesystem-qualification.json"
        ),
        "RESEARCH_OPS_NGINX_CONFIG_FILE": (
            "/etc/nginx/conf.d/research-operations.conf"
        ),
        "INTERNAL_WEB_DATABASE_SSLROOTCERT": ("/etc/research-ops/pki/database-ca.crt"),
        "RESEARCH_OPS_POSTGRESQL_DROP_IN": (
            "/etc/postgresql/16/main/conf.d/90-research-operations.conf"
        ),
        "RESEARCH_OPS_POSTGRESQL_HBA_FILE": (
            "/etc/research-ops/postgresql/pg_hba.conf"
        ),
        "OPS_HTPASSWD_FILE": "/etc/research-ops/secrets/ops.htpasswd",
        "RESEARCH_OPS_EXECUTION_CAPABILITY_KEY_SOURCE_FILE": (
            "/etc/research-ops/secrets/operated-execution.key"
        ),
        "BACKUP_ROOT": "/srv/research-backups",
        "RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY": "/run/research-operations",
        "RESEARCH_OPS_OFFSITE_RECEIPT_ROOT": "/srv/research-offsite-receipts",
        "RESEARCH_OPS_OFFSITE_RECEIPT_VERIFICATION_KEY_FILE": (
            "/etc/research-ops/offsite-receipt-signing.pub"
        ),
    }
    for key, value in expected.items():
        if _required(env, key) != value:
            raise PreflightError(f"native_path_contract_invalid:{key}")
    database_identities = {
        "POSTGRES_DB": "research",
        "POSTGRES_OWNER_USER": "research_owner",
        "POSTGRES_RUNTIME_USER": "research_runtime",
        "POSTGRES_DIAGNOSTICS_USER": "research_diagnostics",
        "POSTGRES_VALIDATOR_USER": "research_validator",
        "POSTGRES_BACKUP_USER": "research_backup",
        "POSTGRES_MAJOR": "16",
    }
    if any(_required(env, key) != value for key, value in database_identities.items()):
        raise PreflightError("native_postgresql_identity_contract_invalid")


def _validate_backup_policy(
    env: Mapping[str, str], service_uid: int, service_gid: int
) -> None:
    if _required(env, "RESEARCH_OPS_OFFSITE_REQUIRED") != "true":
        raise PreflightError("offsite_backup_required")
    if _required(env, "RESEARCH_OPS_LEGAL_HOLD_ENFORCEMENT") != "true":
        raise PreflightError("legal_hold_enforcement_required")
    _bounded_integer(env, "RESEARCH_OPS_BACKUP_RETENTION_DAYS", 7, 3650)
    _bounded_integer(env, "RESEARCH_OPS_BACKUP_RETENTION_MINIMUM_COUNT", 2, 1000)
    _bounded_integer(env, "RESEARCH_OPS_RPO_SECONDS", 300, 604800)
    _bounded_integer(env, "RESEARCH_OPS_RTO_SECONDS", 300, 604800)
    _assigned(env, "RESEARCH_OPS_OFFSITE_TARGET_ID")
    encryption = _required(env, "RESEARCH_OPS_BACKUP_ENCRYPTION")
    if encryption not in {"age", "kms-envelope"}:
        raise PreflightError("backup_encryption_invalid")
    _assigned(env, "RESEARCH_OPS_BACKUP_ENCRYPTION_KEY_ID")
    _assigned(env, "BACKUP_OPERATOR_ID")

    for key in ("BACKUP_ROOT", "RESEARCH_OPS_OFFSITE_RECEIPT_ROOT"):
        _safe_directory(
            _absolute(env, key),
            key.lower(),
            allowed_uids={service_uid},
        )
    hook = _absolute(env, "RESEARCH_OPS_OFFSITE_EXPORT_HOOK")
    status = _regular_file(hook, "offsite_export_hook")
    mode = stat.S_IMODE(status.st_mode)
    if status.st_uid != 0 or not mode & 0o111:
        raise PreflightError("offsite_export_hook_invalid")
    executable = (status.st_gid == service_gid and bool(mode & 0o010)) or bool(
        mode & 0o001
    )
    if not executable:
        raise PreflightError("offsite_export_hook_unexecutable")


def _run_openssl(arguments: list[str], code: str) -> bytes:
    try:
        completed = subprocess.run(
            ["/usr/bin/openssl", *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PreflightError(f"openssl_failed:{code}") from error
    if completed.returncode != 0:
        raise PreflightError(f"openssl_failed:{code}")
    return completed.stdout


def _check_host(cert: Path, hostname: str, code: str) -> None:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        option = "-checkhost"
    else:
        option = "-checkip"
    _run_openssl(["x509", option, hostname, "-noout", "-in", str(cert)], code)


def _validate_certificate_pair(
    *,
    cert: Path,
    key: Path,
    ca: Path,
    hostname: str,
    minimum_validity: int,
    code: str,
) -> None:
    _run_openssl(
        ["x509", "-checkend", str(minimum_validity), "-noout", "-in", str(cert)],
        f"{code}_expiry",
    )
    _run_openssl(["verify", "-CAfile", str(ca), str(cert)], f"{code}_chain")
    _check_host(cert, hostname, f"{code}_identity")
    certificate_key = _run_openssl(
        ["x509", "-pubkey", "-noout", "-in", str(cert)], f"{code}_cert_key"
    )
    private_key = _run_openssl(
        ["pkey", "-pubout", "-in", str(key)], f"{code}_private_key"
    )
    if (
        not certificate_key
        or not private_key
        or not hmac.compare_digest(
            hashlib.sha256(certificate_key).digest(),
            hashlib.sha256(private_key).digest(),
        )
    ):
        raise PreflightError(f"certificate_key_mismatch:{code}")


def _validate_pki(
    env: Mapping[str, str],
    postgres_uid: int,
    postgres_gid: int,
) -> None:
    minimum_validity = _bounded_integer(
        env, "RESEARCH_OPS_PKI_MINIMUM_VALIDITY_SECONDS", 86400, 15552000
    )
    paths = {
        key: _absolute(env, key)
        for key in (
            "PROXY_SERVER_CA_FILE",
            "PROXY_SERVER_CERT_FILE",
            "PROXY_SERVER_KEY_FILE",
            "OPS_CLIENT_CA_FILE",
            "DATABASE_CA_FILE",
            "DATABASE_SERVER_CERT_FILE",
            "DATABASE_SERVER_KEY_FILE",
        )
    }
    expected_paths = {
        "PROXY_SERVER_CA_FILE": "/etc/research-ops/pki/proxy-ca.crt",
        "PROXY_SERVER_CERT_FILE": "/etc/research-ops/pki/proxy.crt",
        "PROXY_SERVER_KEY_FILE": "/etc/research-ops/pki/proxy.key",
        "OPS_CLIENT_CA_FILE": "/etc/research-ops/pki/ops-client-ca.crt",
        "DATABASE_CA_FILE": "/etc/research-ops/pki/database-ca.crt",
        "DATABASE_SERVER_CERT_FILE": "/etc/research-ops/pki/postgres.crt",
        "DATABASE_SERVER_KEY_FILE": "/etc/research-ops/pki/postgres.key",
    }
    if any(str(paths[key]) != value for key, value in expected_paths.items()):
        raise PreflightError("pki_path_contract_invalid")
    for key in (
        "PROXY_SERVER_CA_FILE",
        "PROXY_SERVER_CERT_FILE",
        "OPS_CLIENT_CA_FILE",
        "DATABASE_CA_FILE",
        "DATABASE_SERVER_CERT_FILE",
    ):
        _public_file(paths[key], key.lower(), {0, postgres_uid})
        _run_openssl(
            [
                "x509",
                "-checkend",
                str(minimum_validity),
                "-noout",
                "-in",
                str(paths[key]),
            ],
            f"{key.lower()}_expiry",
        )
    _secret_for_identity(paths["PROXY_SERVER_KEY_FILE"], "proxy_server_key", 0, 0)
    proxy_status = paths["PROXY_SERVER_KEY_FILE"].stat()
    if (
        proxy_status.st_uid != 0
        or proxy_status.st_gid != 0
        or stat.S_IMODE(proxy_status.st_mode) != 0o600
    ):
        raise PreflightError("proxy_server_key_permissions_invalid")
    _secret_for_identity(
        paths["DATABASE_SERVER_KEY_FILE"],
        "database_server_key",
        postgres_uid,
        postgres_gid,
    )
    database_status = paths["DATABASE_SERVER_KEY_FILE"].stat()
    database_identity = (
        database_status.st_uid == postgres_uid
        and stat.S_IMODE(database_status.st_mode) == 0o600
    ) or (
        database_status.st_uid == 0
        and database_status.st_gid == postgres_gid
        and stat.S_IMODE(database_status.st_mode) == 0o640
    )
    if not database_identity:
        raise PreflightError("database_server_key_permissions_invalid")
    if _required(env, "RESEARCH_OPS_DEPLOYMENT_ENVIRONMENT") == "production":
        parents = {path.parent for path in paths.values()}
        if any((parent / "TEST_ONLY").exists() for parent in parents):
            raise PreflightError("test_pki_forbidden_in_production")
    elif env["RESEARCH_OPS_DEPLOYMENT_ENVIRONMENT"] != "acceptance":
        raise PreflightError("deployment_environment_invalid")

    _validate_certificate_pair(
        cert=paths["PROXY_SERVER_CERT_FILE"],
        key=paths["PROXY_SERVER_KEY_FILE"],
        ca=paths["PROXY_SERVER_CA_FILE"],
        hostname=_required(env, "EMPLOYEE_SERVER_NAME"),
        minimum_validity=minimum_validity,
        code="proxy",
    )
    _validate_certificate_pair(
        cert=paths["DATABASE_SERVER_CERT_FILE"],
        key=paths["DATABASE_SERVER_KEY_FILE"],
        ca=paths["DATABASE_CA_FILE"],
        hostname=_required(env, "INTERNAL_WEB_DATABASE_HOST"),
        minimum_validity=minimum_validity,
        code="database",
    )


def _validate_secret_files(
    env: Mapping[str, str], service_uid: int, service_gid: int
) -> None:
    for key in (
        "POSTGRES_OWNER_PASSWORD_FILE",
        "POSTGRES_RUNTIME_PASSWORD_FILE",
        "POSTGRES_DIAGNOSTICS_PASSWORD_FILE",
        "POSTGRES_VALIDATOR_PASSWORD_FILE",
        "POSTGRES_BACKUP_PASSWORD_FILE",
        "DJANGO_SECRET_KEY_FILE",
        "OPS_HTPASSWD_FILE",
        "BACKUP_SIGNING_KEY_FILE",
        "CONTROL_DATABASE_URL_FILE",
    ):
        _secret_for_identity(_absolute(env, key), key.lower(), service_uid, service_gid)
    _public_file(
        _absolute(env, "BACKUP_VERIFICATION_KEY_FILE"),
        "backup_verification_key",
        {0, service_uid},
    )
    offsite_verification_key = _absolute(
        env,
        "RESEARCH_OPS_OFFSITE_RECEIPT_VERIFICATION_KEY_FILE",
    )
    _public_file(
        offsite_verification_key,
        "offsite_receipt_verification_key",
        {0},
    )
    offsite_key_description = _run_openssl(
        [
            "pkey",
            "-pubin",
            "-in",
            str(offsite_verification_key),
            "-text_pub",
            "-noout",
        ],
        "offsite_receipt_verification_key",
    )
    if not (
        offsite_key_description.startswith(b"ED25519 Public-Key:\n")
        or (
            offsite_key_description.startswith(b"Public-Key: (")
            and b"\nModulus:\n" in offsite_key_description
        )
    ):
        raise PreflightError("offsite_receipt_verification_key_unsupported")
    capability_key = _absolute(
        env,
        "RESEARCH_OPS_EXECUTION_CAPABILITY_KEY_SOURCE_FILE",
    )
    status = _regular_file(capability_key, "operated_execution_capability_key")
    if (
        status.st_uid != 0
        or status.st_gid != 0
        or stat.S_IMODE(status.st_mode) != 0o400
        or status.st_size != 32
    ):
        raise PreflightError("operated_execution_capability_key_invalid")


def _validate_runtime_files(env: Mapping[str, str]) -> None:
    installed_postgresql = {
        _absolute(env, "RESEARCH_OPS_POSTGRESQL_DROP_IN"): (
            Path(_required(env, "RESEARCH_OPS_SOURCE_ROOT"))
            / "services/research_operations/deploy/native/postgresql"
            / "90-research-operations.conf"
        ),
        _absolute(env, "RESEARCH_OPS_POSTGRESQL_HBA_FILE"): (
            Path(_required(env, "RESEARCH_OPS_SOURCE_ROOT"))
            / "services/research_operations/deploy/native/postgresql/pg_hba.conf"
        ),
    }
    for installed, source in installed_postgresql.items():
        _public_file(installed, "postgresql_native_configuration", {0})
        try:
            matches = hmac.compare_digest(installed.read_bytes(), source.read_bytes())
        except OSError as error:
            raise PreflightError("postgresql_native_configuration_invalid") from error
        if not matches:
            raise PreflightError("postgresql_native_configuration_drift")
    receipt = _absolute(env, "RESEARCH_OPS_FILESYSTEM_QUALIFICATION_RECEIPT")
    _public_file(receipt, "filesystem_qualification_receipt", {0})
    qualification = _load_json(receipt, "filesystem_qualification_receipt")
    if (
        qualification.get("schema_version") != 1
        or qualification.get("status") != "PASS"
        or qualification.get("scope") != "single-host"
    ):
        raise PreflightError("filesystem_qualification_invalid")
    roles = {
        item.get("role")
        for item in qualification.get("roots", [])
        if isinstance(item, dict)
    }
    if roles != {"data", "artifact", "report", "cache", "identity_registry"}:
        raise PreflightError("filesystem_qualification_roles_invalid")
    nginx_config = _absolute(env, "RESEARCH_OPS_NGINX_CONFIG_FILE")
    if str(nginx_config) != "/etc/nginx/conf.d/research-operations.conf":
        raise PreflightError("nginx_configuration_path_invalid")
    _public_file(nginx_config, "nginx_config", {0})
    try:
        completed = subprocess.run(
            ["/usr/sbin/nginx", "-t", "-q"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PreflightError("nginx_configuration_invalid") from error
    if completed.returncode != 0:
        raise PreflightError("nginx_configuration_invalid")


def _validate_native_tools() -> None:
    for path in (
        "/usr/bin/jq",
        "/usr/bin/openssl",
        "/usr/bin/pg_dump",
        "/usr/bin/pg_restore",
        "/usr/bin/psql",
        "/usr/bin/python3",
        "/usr/bin/realpath",
        "/usr/bin/id",
        "/usr/bin/stat",
        "/usr/bin/sync",
        "/usr/bin/tar",
        "/usr/sbin/nginx",
    ):
        candidate = Path(path)
        try:
            status = candidate.stat()
        except OSError as error:
            raise PreflightError("native_tool_missing") from error
        if not stat.S_ISREG(status.st_mode) or not os.access(candidate, os.X_OK):
            raise PreflightError("native_tool_invalid")


def main() -> int:
    env = dict(os.environ)
    receipt_group: int | None = None
    receipt_contract_valid = False
    try:
        if os.geteuid() != 0:
            raise PreflightError("preflight_requires_root")
        if _required(env, "RESEARCH_OPS_SERVICE_USER") != "research-ops":
            raise PreflightError("service_identity_invalid")
        if _required(env, "RESEARCH_OPS_SERVICE_GROUP") != "research-ops":
            raise PreflightError("service_group_invalid")
        if _required(env, "RESEARCH_OPS_WEB_USER") != "research-web":
            raise PreflightError("web_identity_invalid")
        if _required(env, "RESEARCH_OPS_POSTGRES_USER") != "postgres":
            raise PreflightError("postgres_identity_invalid")
        if _required(env, "RESEARCH_OPS_POSTGRES_GROUP") != "postgres":
            raise PreflightError("postgres_group_invalid")
        service = pwd.getpwnam("research-ops")
        web = pwd.getpwnam("research-web")
        service_group = grp.getgrnam("research-ops")
        postgres = pwd.getpwnam("postgres")
        postgres_group = grp.getgrnam("postgres")
        if service.pw_gid != service_group.gr_gid:
            raise PreflightError("service_identity_group_mismatch")
        if web.pw_uid == service.pw_uid:
            raise PreflightError("web_worker_identity_not_separated")
        receipt_group = service_group.gr_gid
        _validate_receipt_contract(env, receipt_group)
        receipt_contract_valid = True
        _write_receipt(
            env,
            group_gid=receipt_group,
            status="FAIL",
            failure_code="preflight_in_progress",
        )
        _root_configuration_for_group(
            _absolute(env, "RESEARCH_OPS_ENV_FILE"),
            "runtime_environment",
            service_group.gr_gid,
        )
        _validate_owner_assignments(env)
        _validate_native_path_contracts(env)
        _validate_release_metadata(env)
        _validate_backup_policy(env, service.pw_uid, service_group.gr_gid)
        _validate_secret_files(env, service.pw_uid, service_group.gr_gid)
        _validate_pki(
            env,
            postgres.pw_uid,
            postgres_group.gr_gid,
        )
        _validate_native_tools()
        _validate_runtime_files(env)
        _write_receipt(
            env,
            group_gid=receipt_group,
            status="PASS",
            failure_code=None,
        )
    except (KeyError, PreflightError) as error:
        message = _stable_failure_code(error)
        if receipt_contract_valid and receipt_group is not None:
            try:
                _write_receipt(
                    env,
                    group_gid=receipt_group,
                    status="FAIL",
                    failure_code=message,
                )
            except PreflightError:
                message = "preflight_receipt_write_failed"
        print(f"research_operations_preflight_failed:{message}", file=sys.stderr)
        return 78
    print("research_operations_preflight_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
