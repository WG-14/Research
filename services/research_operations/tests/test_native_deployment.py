from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_operations.backup import create_signed_backup_manifest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "deploy" / "native"
SYSTEMD = NATIVE / "systemd"


def _unit_credentials(name: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for line in (SYSTEMD / name).read_text().splitlines():
        if not line.startswith("LoadCredential="):
            continue
        credential, separator, source = line.removeprefix("LoadCredential=").partition(
            ":"
        )
        assert separator and credential and source
        assignments[credential] = source
    return assignments


def _key_pair(
    directory: Path, name: str, *, algorithm: str = "RSA"
) -> tuple[Path, Path]:
    private_key = directory / f"{name}.key"
    public_key = directory / f"{name}.pub"
    command = ["/usr/bin/openssl", "genpkey", "-algorithm", algorithm]
    if algorithm == "RSA":
        command.extend(["-pkeyopt", "rsa_keygen_bits:2048"])
    subprocess.run(
        [*command, "-out", str(private_key)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-out",
            str(public_key),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return private_key, public_key


def _signed_offsite_receipt(
    directory: Path,
    *,
    private_key: Path,
    algorithm: str,
    backup_id: str,
    manifest_hash: str,
    uploaded_at: datetime | None = None,
) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "schema_version": 1,
        "status": "VERIFIED",
        "backup_id": backup_id,
        "target_id": "approved-vault",
        "encrypted": True,
        "encryption": "kms-envelope",
        "encryption_key_id": "kms-key-version-7",
        "manifest_hash": manifest_hash,
        "remote_object_digest": "sha256:" + "a" * 64,
        "remote_object_version": "immutable-version-1",
        "uploaded_at": (uploaded_at or datetime.now(UTC))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    payload = directory / f"{backup_id}.receipt.payload"
    signature = directory / f"{backup_id}.receipt.sig"
    payload.write_text(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )
    if algorithm == "ED25519":
        command = [
            "/usr/bin/openssl",
            "pkeyutl",
            "-sign",
            "-inkey",
            str(private_key),
            "-rawin",
            "-in",
            str(payload),
            "-out",
            str(signature),
        ]
    else:
        command = [
            "/usr/bin/openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(private_key),
            "-out",
            str(signature),
            str(payload),
        ]
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        **unsigned,
        "receipt_signature": "base64:"
        + base64.b64encode(signature.read_bytes()).decode("ascii"),
    }


def _load_preflight():
    path = NATIVE / "bin" / "preflight.py"
    spec = importlib.util.spec_from_file_location("native_preflight", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dataset_transformation_trust_fixture(
    directory: Path,
) -> tuple[dict[str, str], Path, Path, dict[str, object]]:
    key_root = directory / "dataset-transformation-keys"
    key_root.mkdir()
    key_path = key_root / "steward.ed25519.pub"
    key_path.write_bytes(b"ed25519:" + base64.b64encode(os.urandom(32)) + b"\n")
    key_hash = "sha256:" + hashlib.sha256(key_path.read_bytes()).hexdigest()
    current = datetime.now(UTC)
    payload: dict[str, object] = {
        "schema_version": 1,
        "artifact_type": "dataset_transformation_trust_store",
        "authority_id": "test-data-steward-authority",
        "issued_at": (current - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (current + timedelta(days=30))
        .isoformat()
        .replace("+00:00", "Z"),
        "keys": [
            {
                "key_id": "test-steward-key",
                "algorithm": "ed25519",
                "public_key_path": str(key_path),
                "public_key_content_hash": key_hash,
                "valid_from": (current - timedelta(days=2))
                .isoformat()
                .replace("+00:00", "Z"),
                "valid_until": (current + timedelta(days=20))
                .isoformat()
                .replace("+00:00", "Z"),
                "revoked_at": None,
                "revocation_reason": "",
            }
        ],
    }
    trust_path = directory / "dataset-transformation-trust.json"
    trust_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    env = {
        "RESEARCH_DATASET_TRANSFORMATION_TRUST_STORE_PATH": str(trust_path),
        "RESEARCH_DATASET_TRANSFORMATION_TRUST_STORE_HASH": (
            "sha256:" + hashlib.sha256(trust_path.read_bytes()).hexdigest()
        ),
    }
    return env, trust_path, key_root, payload


def _independent_verifier_trust_fixture(
    directory: Path,
) -> tuple[dict[str, str], Path, Path, dict[str, object]]:
    key_root = directory / "independent-verifier-keys"
    key_root.mkdir(parents=True)
    key_path = key_root / "verifier.ed25519.pub"
    key_path.write_bytes(b"ed25519:" + base64.b64encode(os.urandom(32)) + b"\n")
    key_hash = "sha256:" + hashlib.sha256(key_path.read_bytes()).hexdigest()
    current = datetime.now(UTC)
    payload: dict[str, object] = {
        "schema_version": 1,
        "artifact_type": "independent_verifier_trust_store",
        "authority_id": "test-independent-identity-authority",
        "issued_at": (current - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (current + timedelta(days=30))
        .isoformat()
        .replace("+00:00", "Z"),
        "keys": [
            {
                "key_id": "test-verifier-key",
                "algorithm": "ed25519",
                "public_key_path": str(key_path),
                "public_key_content_hash": key_hash,
                "valid_from": (current - timedelta(days=2))
                .isoformat()
                .replace("+00:00", "Z"),
                "valid_until": (current + timedelta(days=20))
                .isoformat()
                .replace("+00:00", "Z"),
                "revoked_at": None,
                "revocation_reason": "",
            }
        ],
    }
    trust_path = directory / "independent-verifier-trust.json"
    trust_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    env = {
        "RESEARCH_INDEPENDENT_VERIFIER_TRUST_STORE_PATH": str(trust_path),
        "RESEARCH_INDEPENDENT_VERIFIER_TRUST_STORE_HASH": (
            "sha256:" + hashlib.sha256(trust_path.read_bytes()).hexdigest()
        ),
    }
    return env, trust_path, key_root, payload


def test_native_systemd_is_the_single_official_deployment() -> None:
    assert (ROOT / "deploy" / "OFFICIAL_DEPLOYMENT").read_text().strip() == (
        "native-systemd"
    )
    compose = (ROOT / "deploy" / "compose.yaml").read_text()
    assert compose.startswith("# NON-OFFICIAL REFERENCE ONLY.")
    assert "research-operations-reference" in compose
    reference = (ROOT / "deploy" / "compose-reference.md").read_text()
    assert "not the supported production deployment" in reference


def test_preflight_requires_every_child_sandbox_runtime_tool() -> None:
    module = _load_preflight()
    assert {
        "/usr/bin/bwrap",
        "/usr/bin/prlimit",
        "/usr/bin/timeout",
    }.issubset(module._REQUIRED_NATIVE_TOOLS)


def test_native_unit_inventory_and_target_membership() -> None:
    expected = {
        "research-operations.target",
        "research-operations-preflight.service",
        "research-operations-preflight.timer",
        "research-operations-migrate.service",
        "research-operations-web.service",
        "research-operations-ops-api.service",
        "research-operations-outbox-worker@.service",
        "research-operations-job-worker.service",
        "research-operations-alert-worker.service",
        "research-operations-validator.service",
        "research-operations-backup.service",
        "research-operations-backup.timer",
        "research-operations-retention-audit.service",
        "research-operations-retention-audit.timer",
    }
    assert {path.name for path in SYSTEMD.iterdir()} == expected
    target = (SYSTEMD / "research-operations.target").read_text()
    for name in (
        "research-operations-web.service",
        "research-operations-ops-api.service",
        "research-operations-outbox-worker@1.service",
        "research-operations-outbox-worker@2.service",
        "research-operations-job-worker.service",
        "research-operations-alert-worker.service",
        "research-operations-validator.service",
    ):
        assert f"Requires={name}" in target
    for timer in (
        "research-operations-backup.timer",
        "research-operations-preflight.timer",
        "research-operations-retention-audit.timer",
    ):
        assert f"Wants={timer}" in target


@pytest.mark.parametrize(
    "name,timeout",
    [
        ("research-operations-web.service", "45s"),
        ("research-operations-ops-api.service", "20s"),
        ("research-operations-outbox-worker@.service", "45s"),
        ("research-operations-job-worker.service", "135s"),
        ("research-operations-alert-worker.service", "45s"),
        ("research-operations-validator.service", "30s"),
    ],
)
def test_long_running_units_are_supervised_and_hardened(
    name: str, timeout: str
) -> None:
    unit = (SYSTEMD / name).read_text()
    expected_user = {
        "research-operations-web.service": "User=research-web",
        "research-operations-ops-api.service": "User=research-diagnostics",
        "research-operations-outbox-worker@.service": "User=research-outbox",
        "research-operations-job-worker.service": "User=research-job",
        "research-operations-alert-worker.service": "User=research-alert",
        "research-operations-validator.service": "User=research-validator",
    }[name]
    expected_group = {
        "research-operations-web.service": "Group=research-web-proxy",
        "research-operations-ops-api.service": "Group=research-ops-proxy",
    }.get(name, "Group=research-ops")
    namespace_contract = (
        "RestrictNamespaces=mnt user ipc pid uts net"
        if name == "research-operations-job-worker.service"
        else "RestrictNamespaces=true"
    )
    tunables_contract = (
        "ProtectKernelTunables=false"
        if name == "research-operations-job-worker.service"
        else "ProtectKernelTunables=true"
    )
    contracts = (
        expected_user,
        expected_group,
        "EnvironmentFile=/etc/research-ops/runtime.env",
        "Requires=research-operations-preflight.service",
        "Restart=on-failure",
        "KillSignal=SIGTERM",
        "KillMode=mixed",
        f"TimeoutStopSec={timeout}",
        "NoNewPrivileges=true",
        "CapabilityBoundingSet=",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "ProtectProc=invisible",
        tunables_contract,
        "InaccessiblePaths=/etc/research-ops/secrets",
        namespace_contract,
        "MemoryDenyWriteExecute=true",
        "TasksMax=",
        "MemoryMax=",
        "CPUQuota=",
        "StandardOutput=journal",
        "StandardError=journal",
    )
    if name != "research-operations-alert-worker.service":
        contracts += ("research-operations-migrate.service",)
    for contract in contracts:
        assert contract in unit
    assert "/opt/research-platform/current/" in unit


def test_workers_and_validator_use_durable_process_contracts() -> None:
    outbox = (SYSTEMD / "research-operations-outbox-worker@.service").read_text()
    assert "--worker-id outbox:systemd-%i" in outbox
    assert "--lease-seconds 30" in outbox
    job = (SYSTEMD / "research-operations-job-worker.service").read_text()
    assert "research-job-worker" in job
    assert "--worker-id research-job:systemd-1" in job
    assert "TimeoutStopSec=135s" in job
    assert (
        "LoadCredential=operated-execution.key:"
        "/etc/research-ops/secrets/operated-execution.key"
    ) in job
    assert "RestrictNamespaces=mnt user ipc pid uts net" in job
    assert "RestrictNamespaces=true" not in job
    assert "ProtectKernelTunables=false" in job
    assert (
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK" in job
    )
    assert (
        "ReadWritePaths=/srv/research/registry/final_holdout_authority.jsonl "
        "/srv/research/registry/final_holdout_authority.jsonl.lock" in job
    )
    assert "ReadWritePaths=/srv/research/registry\n" not in job
    alert_worker = (SYSTEMD / "research-operations-alert-worker.service").read_text()
    assert "research-ops alert-worker" in alert_worker
    assert "RESEARCH_OPS_DATABASE_ROLE=runtime" in alert_worker
    assert (
        "LoadCredential=service-alert-endpoint-url:"
        "/etc/research-ops/secrets/service-alert-endpoint-url"
    ) in alert_worker
    assert (
        "RESEARCH_OPS_ALERT_ENDPOINT_URL_FILE=%d/service-alert-endpoint-url"
    ) in alert_worker
    assert "research-operations-migrate.service" not in alert_worker
    assert "After=research-operations-preflight.service" in alert_worker
    migrate = (SYSTEMD / "research-operations-migrate.service").read_text()
    assert "research-operations-alert-worker.service" not in migrate
    web = (SYSTEMD / "research-operations-web.service").read_text()
    assert "User=research-web" in web
    assert "Group=research-web-proxy" in web
    assert "SupplementaryGroups=research-ops" in web
    assert "RuntimeDirectory=research-operations-web" in web
    assert (
        'worker_tmp_dir = "/run/research-operations-web"'
        in (NATIVE / "gunicorn-web.conf.py").read_text()
    )
    validator = (SYSTEMD / "research-operations-validator.service").read_text()
    assert "scripts/audit-validator-loop.sh" in validator
    assert "RESEARCH_OPS_DATABASE_ROLE=validator" in validator


def test_systemd_projects_an_exact_per_unit_secret_allowlist() -> None:
    expected = {
        "research-operations-migrate.service": {
            "postgres-owner-password": (
                "/etc/research-ops/secrets/postgres-owner-password"
            ),
            "django-secret-key": "/etc/research-ops/secrets/django-secret-key",
        },
        "research-operations-web.service": {
            "postgres-runtime-password": (
                "/etc/research-ops/secrets/postgres-runtime-password"
            ),
            "django-secret-key": "/etc/research-ops/secrets/django-secret-key",
        },
        "research-operations-outbox-worker@.service": {
            "postgres-runtime-password": (
                "/etc/research-ops/secrets/postgres-runtime-password"
            ),
            "django-secret-key": "/etc/research-ops/secrets/django-secret-key",
        },
        "research-operations-job-worker.service": {
            "postgres-runtime-password": (
                "/etc/research-ops/secrets/postgres-runtime-password"
            ),
            "django-secret-key": "/etc/research-ops/secrets/django-secret-key",
            "operated-execution.key": (
                "/etc/research-ops/secrets/operated-execution.key"
            ),
        },
        "research-operations-alert-worker.service": {
            "postgres-runtime-password": (
                "/etc/research-ops/secrets/postgres-runtime-password"
            ),
            "service-alert-endpoint-url": (
                "/etc/research-ops/secrets/service-alert-endpoint-url"
            ),
        },
        "research-operations-validator.service": {
            "postgres-validator-password": (
                "/etc/research-ops/secrets/postgres-validator-password"
            ),
            "django-secret-key": "/etc/research-ops/secrets/django-secret-key",
        },
        "research-operations-backup.service": {
            "postgres-backup-password": (
                "/etc/research-ops/secrets/postgres-backup-password"
            ),
            "django-secret-key": "/etc/research-ops/secrets/django-secret-key",
            "backup-signing.key": ("/etc/research-ops/secrets/backup-signing.key"),
        },
        "research-operations-ops-api.service": {
            "postgres-diagnostics-password": (
                "/etc/research-ops/secrets/postgres-diagnostics-password"
            ),
        },
    }
    for name, credentials in expected.items():
        unit = (SYSTEMD / name).read_text()
        assert _unit_credentials(name) == credentials
        assert "InaccessiblePaths=/etc/research-ops/secrets" in unit
        for credential in credentials:
            if credential == "operated-execution.key":
                continue
            environment_name = {
                "backup-signing.key": "RESEARCH_OPS_BACKUP_SIGNING_KEY_FILE",
                "django-secret-key": "DJANGO_SECRET_KEY_FILE",
                "service-alert-endpoint-url": ("RESEARCH_OPS_ALERT_ENDPOINT_URL_FILE"),
            }.get(credential)
            if environment_name is None:
                environment_name = (
                    "POSTGRES_"
                    + credential.removeprefix("postgres-")
                    .removesuffix("-password")
                    .upper()
                    + "_PASSWORD_FILE"
                )
            assert f"Environment={environment_name}=%d/{credential}" in unit

    expected_users = {
        "research-operations-migrate.service": "research-migrate",
        "research-operations-web.service": "research-web",
        "research-operations-outbox-worker@.service": "research-outbox",
        "research-operations-job-worker.service": "research-job",
        "research-operations-alert-worker.service": "research-alert",
        "research-operations-validator.service": "research-validator",
        "research-operations-backup.service": "research-backup",
        "research-operations-ops-api.service": "research-diagnostics",
    }
    assert len(set(expected_users.values())) == len(expected_users)
    runtime_directories: set[str] = set()
    for name, user in expected_users.items():
        unit = (SYSTEMD / name).read_text()
        assert f"User={user}" in unit
        assert "ProtectProc=invisible" in unit
        runtime_directory = next(
            line for line in unit.splitlines() if line.startswith("RuntimeDirectory=")
        )
        runtime_directories.add(runtime_directory)
    assert len(runtime_directories) == len(expected_users)
    backup = (SYSTEMD / "research-operations-backup.service").read_text()
    assert "Group=research-backup" in backup
    assert "SupplementaryGroups=research-ops" in backup
    retention = (SYSTEMD / "research-operations-retention-audit.service").read_text()
    assert "User=research-retention" in retention
    assert "Group=research-retention" in retention
    assert "SupplementaryGroups=research-backup" in retention
    assert "ProtectProc=invisible" in retention

    web = (SYSTEMD / "research-operations-web.service").read_text()
    assert set(_unit_credentials("research-operations-web.service")) == {
        "postgres-runtime-password",
        "django-secret-key",
    }
    for forbidden in (
        "postgres-owner-password",
        "postgres-validator-password",
        "postgres-backup-password",
        "postgres-diagnostics-password",
        "backup-signing.key",
        "service-alert-endpoint-url",
        "operated-execution.key",
        "control-database-url",
        "ops.htpasswd",
    ):
        assert forbidden not in web


def test_sysusers_declares_separate_non_login_trust_tiers() -> None:
    declaration = NATIVE / "sysusers.d/research-operations.conf"
    users = {
        line.split()[1]
        for line in declaration.read_text().splitlines()
        if line.startswith("u ")
    }
    assert users == {
        "research-migrate",
        "research-web",
        "research-outbox",
        "research-job",
        "research-alert",
        "research-validator",
        "research-backup",
        "research-diagnostics",
        "research-retention",
        "research-proxy",
    }
    assert all(user != "research-ops" for user in users)
    groups = {
        line.split()[1]
        for line in declaration.read_text().splitlines()
        if line.startswith("g ")
    }
    assert groups == {
        "research-ops",
        "research-backup",
        "research-web-proxy",
        "research-ops-proxy",
    }
    memberships = {
        tuple(line.split()[1:3])
        for line in declaration.read_text().splitlines()
        if line.startswith("m ")
    }
    assert memberships == {
        ("research-proxy", "research-web-proxy"),
        ("research-proxy", "research-ops-proxy"),
    }
    parsed = subprocess.run(
        ["systemd-sysusers", "--dry-run", str(declaration)],
        check=False,
        text=True,
        capture_output=True,
    )
    assert parsed.returncode == 0, parsed.stderr


def test_native_artifact_writers_publish_group_readable_for_backup() -> None:
    writers = {
        "research-operations-migrate.service",
        "research-operations-web.service",
        "research-operations-job-worker.service",
    }
    setting = "Environment=MARKET_RESEARCH_ATOMIC_PUBLICATION_MODE=0640"
    for unit_path in SYSTEMD.glob("*.service"):
        unit = unit_path.read_text()
        if unit_path.name in writers:
            assert setting in unit
        else:
            assert setting not in unit


def test_native_writer_mounts_separate_evidence_integrity_domains() -> None:
    migrate = (SYSTEMD / "research-operations-migrate.service").read_text()
    web = (SYSTEMD / "research-operations-web.service").read_text()
    job = (SYSTEMD / "research-operations-job-worker.service").read_text()
    outbox = (SYSTEMD / "research-operations-outbox-worker@.service").read_text()
    validator = (SYSTEMD / "research-operations-validator.service").read_text()

    assert "ReadWritePaths=/srv/research/artifacts/_internal_web/static" in migrate
    assert "ReadWritePaths=/srv/research/artifacts " not in migrate
    assert "ReadOnlyPaths=/srv/research/artifacts/_internal_web/static" in web
    for protected in (
        "/srv/research/artifacts/_internal_web",
        "/srv/research/artifacts/reports/research/_registry",
        "/srv/research/artifacts/derived/research/projects",
        "/srv/research/reports/_internal_web",
        "/srv/research/cache/research/projects",
    ):
        assert f"ReadOnlyPaths={protected}" in job
    assert "ReadWritePaths=" not in outbox
    assert "ReadWritePaths=" not in validator


def test_static_projection_is_complete_public_and_read_only() -> None:
    migration = (ROOT / "scripts/apply-migrations.sh").read_text()
    drop_in = (NATIVE / "nginx/nginx.service.d/research-operations.conf").read_text()

    assert "django-admin collectstatic --noinput --clear" in migration
    assert "-type l -print -quit" in migration
    assert "! -type d ! -type f -print -quit" in migration
    assert "-type f -links +1" in migration
    assert "-type d -exec chmod 0755" in migration
    assert "-type f -exec chmod 0644" in migration
    assert 'sync -f "$INTERNAL_WEB_STATIC_ROOT"' in migration
    assert "BindReadOnlyPaths=/srv/research/artifacts/_internal_web/static:" in drop_in


def test_preflight_rejects_numeric_aliases_and_host_supplementary_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_preflight()
    identity_contract = dict(module._SERVICE_IDENTITY_CONTRACT)
    group_contract = dict(module._SERVICE_GROUP_CONTRACT)
    group_ids = {
        "research-ops": 3000,
        "research-backup": 3001,
        "research-web-proxy": 3002,
        "research-ops-proxy": 3003,
        **{
            name: 4000 + index
            for index, name in enumerate(identity_contract.values())
            if name != "research-backup"
        },
    }
    users = {
        name: SimpleNamespace(
            pw_name=name,
            pw_uid=2000 + index,
            pw_gid=group_ids[name],
            pw_shell="/usr/sbin/nologin",
        )
        for index, name in enumerate(identity_contract.values())
    }
    groups = {
        name: SimpleNamespace(
            gr_name=name,
            gr_gid=gid,
            gr_mem=(
                ["research-proxy"]
                if name in {"research-web-proxy", "research-ops-proxy"}
                else []
            ),
        )
        for name, gid in group_ids.items()
    }
    monkeypatch.setattr(module.pwd, "getpwnam", users.__getitem__)
    monkeypatch.setattr(module.pwd, "getpwall", lambda: list(users.values()))
    monkeypatch.setattr(module.grp, "getgrnam", groups.__getitem__)
    monkeypatch.setattr(module.grp, "getgrall", lambda: list(groups.values()))
    env = {
        **identity_contract,
        **group_contract,
    }
    identities, validated_groups = module._validate_service_identities(
        env,
        protected_uids={900, 901},
        protected_gids={900, 901},
        protected_member_names=set(),
    )
    assert len(identities) == 10
    assert (
        validated_groups["research-ops"].gr_gid
        != validated_groups["research-backup"].gr_gid
    )

    alert_uid = users["research-alert"].pw_uid
    users["research-alert"].pw_uid = users["research-job"].pw_uid
    with pytest.raises(module.PreflightError, match="service_identity_not_separated"):
        module._validate_service_identities(
            env,
            protected_uids={900, 901},
            protected_gids={900, 901},
            protected_member_names=set(),
        )
    users["research-alert"].pw_uid = alert_uid

    users["research-alert"].pw_uid = 900
    with pytest.raises(module.PreflightError, match="service_identity_not_separated"):
        module._validate_service_identities(
            env,
            protected_uids={900, 901},
            protected_gids={900, 901},
            protected_member_names=set(),
        )
    users["research-alert"].pw_uid = alert_uid

    users["alias-account"] = SimpleNamespace(
        pw_name="alias-account",
        pw_uid=alert_uid,
        pw_gid=9998,
        pw_shell="/usr/sbin/nologin",
    )
    with pytest.raises(module.PreflightError, match="service_uid_alias_detected"):
        module._validate_service_identities(
            env,
            protected_uids={900, 901},
            protected_gids={900, 901},
            protected_member_names=set(),
        )
    del users["alias-account"]

    users["primary-gid-intruder"] = SimpleNamespace(
        # A same-name account for a group-only authority must not be mistaken
        # for a legitimate service identity.
        pw_name="research-ops",
        pw_uid=9997,
        pw_gid=groups["research-ops"].gr_gid,
        pw_shell="/usr/sbin/nologin",
    )
    with pytest.raises(
        module.PreflightError,
        match="service_primary_gid_member_forbidden",
    ):
        module._validate_service_identities(
            env,
            protected_uids={900, 901},
            protected_gids={900, 901},
            protected_member_names=set(),
        )
    del users["primary-gid-intruder"]

    service_gid = groups["research-ops"].gr_gid
    groups["research-ops"].gr_gid = 900
    with pytest.raises(module.PreflightError, match="service_group_not_separated"):
        module._validate_service_identities(
            env,
            protected_uids={900, 901},
            protected_gids={900, 901},
            protected_member_names=set(),
        )
    groups["research-ops"].gr_gid = service_gid

    groups["research-ops-alias"] = SimpleNamespace(
        gr_name="research-ops-alias",
        gr_gid=groups["research-ops"].gr_gid,
        gr_mem=[],
    )
    with pytest.raises(module.PreflightError, match="service_gid_alias_detected"):
        module._validate_service_identities(
            env,
            protected_uids={900, 901},
            protected_gids={900, 901},
            protected_member_names=set(),
        )
    del groups["research-ops-alias"]

    groups["unexpected"] = SimpleNamespace(
        gr_name="unexpected",
        gr_gid=9999,
        gr_mem=["research-web"],
    )
    with pytest.raises(
        module.PreflightError,
        match="service_supplementary_group_forbidden",
    ):
        module._validate_service_identities(
            env,
            protected_uids={900, 901},
            protected_gids={900, 901},
            protected_member_names=set(),
        )
    del groups["unexpected"]

    groups["research-web-proxy"].gr_mem = []
    with pytest.raises(
        module.PreflightError,
        match="controlled_group_membership_invalid",
    ):
        module._validate_service_identities(
            env,
            protected_uids={900, 901},
            protected_gids={900, 901},
            protected_member_names=set(),
        )
    groups["research-web-proxy"].gr_mem = ["research-proxy"]

    groups["research-web-proxy"].gr_mem = ["research-proxy", "rogue"]
    with pytest.raises(
        module.PreflightError,
        match="controlled_group_membership_invalid",
    ):
        module._validate_service_identities(
            env,
            protected_uids={900, 901},
            protected_gids={900, 901},
            protected_member_names=set(),
        )
    groups["research-web-proxy"].gr_mem = ["research-proxy"]

    groups["research-ops"].gr_mem = ["www-data"]
    with pytest.raises(
        module.PreflightError,
        match="protected_identity_group_forbidden",
    ):
        module._validate_service_identities(
            env,
            protected_uids={900, 901},
            protected_gids={900, 901},
            protected_member_names={"www-data"},
        )


def test_web_writes_only_declared_adapter_namespaces() -> None:
    web = (SYSTEMD / "research-operations-web.service").read_text()
    writable = {
        line.removeprefix("ReadWritePaths=")
        for line in web.splitlines()
        if line.startswith("ReadWritePaths=")
    }
    assert writable == {
        "/srv/research/data/_internal_web/manifests",
        "/srv/research/artifacts/_internal_web",
        "/srv/research/artifacts/reports/research/_registry",
        "/srv/research/artifacts/derived/research/projects",
        "/srv/research/reports/_internal_web",
        "/srv/research/cache/research/projects",
    }
    assert "/srv/research/artifacts" not in writable
    assert "/srv/research/reports" not in writable
    assert "/srv/research/cache" not in writable
    assert "/srv/research/registry" not in writable


def test_backup_and_maintenance_are_persistent_timers() -> None:
    backup = (SYSTEMD / "research-operations-backup.service").read_text()
    assert "RESEARCH_OPS_DATABASE_ROLE=backup" in backup
    assert "native-backup.sh" in backup
    assert "KillMode=control-group" in backup
    assert "ReadWritePaths=/srv/research-backups" in backup
    for name in (
        "research-operations-backup.timer",
        "research-operations-preflight.timer",
        "research-operations-retention-audit.timer",
    ):
        timer = (SYSTEMD / name).read_text()
        assert "Persistent=true" in timer
        assert "[Install]" in timer
        assert "WantedBy=timers.target" in timer
    retention = (SYSTEMD / "research-operations-retention-audit.service").read_text()
    assert "backup-retention.py --dry-run" in retention


def test_native_network_endpoints_use_separate_local_unix_sockets() -> None:
    web = (NATIVE / "gunicorn-web.conf.py").read_text()
    operations = (NATIVE / "gunicorn-ops.conf.py").read_text()
    proxy = (NATIVE / "nginx" / "research-operations.conf.template").read_text()
    main = (NATIVE / "nginx" / "nginx.conf").read_text()
    assert 'bind = "unix:/run/research-operations-web/web.sock"' in web
    assert 'bind = "unix:/run/research-operations-ops-api/ops-api.sock"' in operations
    assert "server unix:/run/research-operations-web/web.sock" in proxy
    assert "server unix:/run/research-operations-ops-api/ops-api.sock" in proxy
    assert "umask = 0o117" in web
    assert "umask = 0o117" in operations
    drop_in = (NATIVE / "nginx/nginx.service.d/research-operations.conf").read_text()
    assert "Group=research-proxy" in drop_in
    assert "SupplementaryGroups=" not in drop_in
    assert "user research-proxy research-proxy;" in main
    assert "include /etc/nginx/conf.d/research-operations.conf;" in main
    assert "sites-enabled" not in main
    assert "modules-enabled" not in main
    assert "/etc/nginx/mime.types" not in main
    assert "research-ops" not in main
    assert (
        "BindReadOnlyPaths=/srv/research/artifacts/_internal_web/static:"
        "/run/research-operations-nginx-static" in drop_in
    )
    assert "alias /run/research-operations-nginx-static/;" in proxy
    assert "alias /srv/research" not in proxy
    assert (
        "LoadCredential=ops.htpasswd:/etc/research-ops/secrets/ops.htpasswd" in drop_in
    )
    assert (
        "ExecStartPre=/usr/bin/install -o root -g research-proxy -m 0640 "
        "%d/ops.htpasswd "
        "/run/research-operations-nginx-credentials/ops.htpasswd"
    ) in drop_in
    assert (
        "auth_basic_user_file /run/research-operations-nginx-credentials/ops.htpasswd"
    ) in proxy
    assert (
        "RuntimeDirectoryMode=0750"
        in (SYSTEMD / "research-operations-web.service").read_text()
    )
    assert (
        "Group=research-web-proxy\nSupplementaryGroups=research-ops"
        in (SYSTEMD / "research-operations-web.service").read_text()
    )
    assert (
        "RuntimeDirectoryMode=0750"
        in (SYSTEMD / "research-operations-ops-api.service").read_text()
    )
    assert (
        "Group=research-ops-proxy\nSupplementaryGroups=research-ops"
        in (SYSTEMD / "research-operations-ops-api.service").read_text()
    )
    assert "listen 127.0.0.1:9443 ssl http2" in proxy
    assert "location ^~ /__ops { return 404; }" in proxy
    assert "ssl_verify_client on" in proxy


def test_preflight_assignments_enforce_required_separation() -> None:
    module = _load_preflight()
    env = {
        key: f"directory:{index}"
        for index, key in enumerate(module._OWNER_KEYS, start=1)
    }
    module._validate_owner_assignments(env)
    env["RESEARCH_OPS_RECOVERY_APPROVER"] = env["RESEARCH_OPS_BACKUP_OWNER"]
    with pytest.raises(module.PreflightError, match="duties_not_separated"):
        module._validate_owner_assignments(env)


@pytest.mark.parametrize(
    ("uid", "gid", "mode"),
    [
        (1, 0, 0o600),
        (0, 1, 0o600),
        (0, 0, 0o640),
        (0, 0, 0o400),
    ],
)
def test_preflight_rejects_noncanonical_secret_sources(
    monkeypatch: pytest.MonkeyPatch,
    uid: int,
    gid: int,
    mode: int,
) -> None:
    module = _load_preflight()
    monkeypatch.setattr(
        module,
        "_regular_file",
        lambda _path, _code: SimpleNamespace(
            st_uid=uid,
            st_gid=gid,
            st_mode=stat.S_IFREG | mode,
        ),
    )
    with pytest.raises(module.PreflightError, match="root_only_file_invalid"):
        module._root_only_file(Path("/root-only"), "test")


def test_preflight_accepts_exact_root_only_secret_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_preflight()
    monkeypatch.setattr(
        module,
        "_regular_file",
        lambda _path, _code: SimpleNamespace(
            st_uid=0,
            st_gid=0,
            st_mode=stat.S_IFREG | 0o600,
        ),
    )
    module._root_only_file(Path("/root-only"), "test")


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o664])
def test_preflight_rejects_unreadable_or_writable_public_verification_key(
    monkeypatch: pytest.MonkeyPatch,
    mode: int,
) -> None:
    module = _load_preflight()
    monkeypatch.setattr(
        module,
        "_regular_file",
        lambda _path, _code: SimpleNamespace(
            st_uid=0,
            st_gid=0,
            st_mode=stat.S_IFREG | mode,
        ),
    )
    with pytest.raises(module.PreflightError):
        module._root_public_file(Path("/root-public"), "test")


def test_preflight_accepts_exact_root_public_verification_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_preflight()
    monkeypatch.setattr(
        module,
        "_regular_file",
        lambda _path, _code: SimpleNamespace(
            st_uid=0,
            st_gid=0,
            st_mode=stat.S_IFREG | 0o644,
        ),
    )
    module._root_public_file(Path("/root-public"), "test")


def test_preflight_dataset_transformation_trust_is_fixed_and_byte_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_preflight()
    env, trust_path, key_root, _payload = _dataset_transformation_trust_fixture(
        tmp_path
    )
    with pytest.raises(
        module.PreflightError,
        match="dataset_transformation_trust_path_invalid",
    ):
        module._validate_dataset_transformation_trust(env)

    monkeypatch.setattr(module, "_DATASET_TRANSFORMATION_TRUST_STORE", trust_path)
    monkeypatch.setattr(module, "_DATASET_TRANSFORMATION_KEY_ROOT", key_root)
    monkeypatch.setattr(
        module,
        "_root_public_file",
        lambda path, _code: path.stat(),
    )
    monkeypatch.setattr(module, "_exact_directory", lambda *_args, **_kwargs: None)
    module._validate_dataset_transformation_trust(env)

    env["RESEARCH_DATASET_TRANSFORMATION_TRUST_STORE_HASH"] = "sha256:" + "0" * 64
    with pytest.raises(
        module.PreflightError,
        match="dataset_transformation_trust_store_hash_mismatch",
    ):
        module._validate_dataset_transformation_trust(env)


def test_preflight_dataset_transformation_trust_rejects_revoked_only_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_preflight()
    env, trust_path, key_root, payload = _dataset_transformation_trust_fixture(
        tmp_path
    )
    current = datetime.now(UTC)
    payload["keys"][0]["revoked_at"] = (
        (current - timedelta(minutes=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    payload["keys"][0]["revocation_reason"] = "test compromise"
    trust_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    env["RESEARCH_DATASET_TRANSFORMATION_TRUST_STORE_HASH"] = (
        "sha256:" + hashlib.sha256(trust_path.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(module, "_DATASET_TRANSFORMATION_TRUST_STORE", trust_path)
    monkeypatch.setattr(module, "_DATASET_TRANSFORMATION_KEY_ROOT", key_root)
    monkeypatch.setattr(
        module,
        "_root_public_file",
        lambda path, _code: path.stat(),
    )
    monkeypatch.setattr(module, "_exact_directory", lambda *_args, **_kwargs: None)
    with pytest.raises(
        module.PreflightError,
        match="dataset_transformation_trust_active_key_required",
    ):
        module._validate_dataset_transformation_trust(env)


def test_preflight_dataset_transformation_trust_rejects_key_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_preflight()
    env, trust_path, key_root, payload = _dataset_transformation_trust_fixture(
        tmp_path
    )
    key_path = Path(payload["keys"][0]["public_key_path"])
    key_path.write_bytes(b"ed25519:" + base64.b64encode(os.urandom(32)) + b"\n")
    monkeypatch.setattr(module, "_DATASET_TRANSFORMATION_TRUST_STORE", trust_path)
    monkeypatch.setattr(module, "_DATASET_TRANSFORMATION_KEY_ROOT", key_root)
    monkeypatch.setattr(
        module,
        "_root_public_file",
        lambda path, _code: path.stat(),
    )
    monkeypatch.setattr(module, "_exact_directory", lambda *_args, **_kwargs: None)
    with pytest.raises(
        module.PreflightError,
        match="dataset_transformation_trust_key_hash_mismatch",
    ):
        module._validate_dataset_transformation_trust(env)


def test_preflight_independent_verifier_trust_is_fixed_and_byte_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_preflight()
    env, trust_path, key_root, _payload = _independent_verifier_trust_fixture(
        tmp_path
    )
    with pytest.raises(
        module.PreflightError,
        match="independent_verifier_trust_path_invalid",
    ):
        module._validate_independent_verifier_trust(env)

    monkeypatch.setattr(module, "_INDEPENDENT_VERIFIER_TRUST_STORE", trust_path)
    monkeypatch.setattr(module, "_INDEPENDENT_VERIFIER_KEY_ROOT", key_root)
    monkeypatch.setattr(
        module,
        "_root_public_file",
        lambda path, _code: path.stat(),
    )
    monkeypatch.setattr(module, "_exact_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "_root_owned_nonwritable_parent_chain",
        lambda *_args, **_kwargs: None,
    )
    module._validate_independent_verifier_trust(env)

    env["RESEARCH_INDEPENDENT_VERIFIER_TRUST_STORE_HASH"] = "sha256:" + "0" * 64
    with pytest.raises(
        module.PreflightError,
        match="independent_verifier_trust_store_hash_mismatch",
    ):
        module._validate_independent_verifier_trust(env)


def test_preflight_independent_verifier_trust_rejects_revoked_only_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_preflight()
    env, trust_path, key_root, payload = _independent_verifier_trust_fixture(tmp_path)
    current = datetime.now(UTC)
    payload["keys"][0]["revoked_at"] = (
        (current - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    )
    payload["keys"][0]["revocation_reason"] = "issuer compromise"
    trust_path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    env["RESEARCH_INDEPENDENT_VERIFIER_TRUST_STORE_HASH"] = (
        "sha256:" + hashlib.sha256(trust_path.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(module, "_INDEPENDENT_VERIFIER_TRUST_STORE", trust_path)
    monkeypatch.setattr(module, "_INDEPENDENT_VERIFIER_KEY_ROOT", key_root)
    monkeypatch.setattr(
        module,
        "_root_public_file",
        lambda path, _code: path.stat(),
    )
    monkeypatch.setattr(module, "_exact_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "_root_owned_nonwritable_parent_chain",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(
        module.PreflightError,
        match="independent_verifier_trust_active_key_required",
    ):
        module._validate_independent_verifier_trust(env)


def test_preflight_independent_verifier_trust_rejects_key_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_preflight()
    env, trust_path, key_root, payload = _independent_verifier_trust_fixture(tmp_path)
    key_path = Path(payload["keys"][0]["public_key_path"])
    key_path.write_bytes(b"ed25519:" + base64.b64encode(os.urandom(32)) + b"\n")
    monkeypatch.setattr(module, "_INDEPENDENT_VERIFIER_TRUST_STORE", trust_path)
    monkeypatch.setattr(module, "_INDEPENDENT_VERIFIER_KEY_ROOT", key_root)
    monkeypatch.setattr(
        module,
        "_root_public_file",
        lambda path, _code: path.stat(),
    )
    monkeypatch.setattr(module, "_exact_directory", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        module,
        "_root_owned_nonwritable_parent_chain",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(
        module.PreflightError,
        match="independent_verifier_trust_key_hash_mismatch",
    ):
        module._validate_independent_verifier_trust(env)


def test_preflight_requires_setgid_shared_research_root(
    tmp_path: Path,
) -> None:
    module = _load_preflight()
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o2770)
    module._exact_directory(
        shared,
        "shared",
        owner_uid=os.getuid(),
        group_gid=os.getgid(),
        mode=0o2770,
    )
    shared.chmod(0o770)
    with pytest.raises(module.PreflightError, match="directory_contract_invalid"):
        module._exact_directory(
            shared,
            "shared",
            owner_uid=os.getuid(),
            group_gid=os.getgid(),
            mode=0o2770,
        )


def test_preflight_requires_exact_shared_holdout_authority_inodes(
    tmp_path: Path,
) -> None:
    module = _load_preflight()
    target = tmp_path / "final_holdout_authority.jsonl"
    target.write_bytes(b"")
    target.chmod(0o660)

    module._exact_shared_authority_file(
        target,
        "authority",
        owner_uid=os.getuid(),
        group_gid=os.getgid(),
    )

    target.chmod(0o640)
    with pytest.raises(
        module.PreflightError,
        match="authority_file_contract_invalid",
    ):
        module._exact_shared_authority_file(
            target,
            "authority",
            owner_uid=os.getuid(),
            group_gid=os.getgid(),
        )


def test_preflight_requires_kernel_append_only_holdout_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_preflight()
    target = tmp_path / "final_holdout_authority.jsonl"
    target.write_bytes(b"")
    target.chmod(0o660)

    monkeypatch.setattr(module, "_linux_file_flags", lambda _fd, _code: 0)
    with pytest.raises(
        module.PreflightError,
        match="authority_file_append_only_missing:final_holdout_authority",
    ):
        module._exact_shared_authority_file(
            target,
            "final_holdout_authority",
            owner_uid=os.getuid(),
            group_gid=os.getgid(),
            require_append_only=True,
        )

    monkeypatch.setattr(
        module,
        "_linux_file_flags",
        lambda _fd, _code: module._LINUX_FS_APPEND_FL,
    )
    module._exact_shared_authority_file(
        target,
        "final_holdout_authority",
        owner_uid=os.getuid(),
        group_gid=os.getgid(),
        require_append_only=True,
    )


def test_preflight_rejects_symlinked_or_hardlinked_holdout_authority(
    tmp_path: Path,
) -> None:
    module = _load_preflight()
    target = tmp_path / "authority-target.jsonl"
    target.write_bytes(b"")
    target.chmod(0o660)
    symlink = tmp_path / "authority-symlink.jsonl"
    symlink.symlink_to(target)

    with pytest.raises(
        module.PreflightError,
        match="authority_file_contract_invalid",
    ):
        module._exact_shared_authority_file(
            symlink,
            "authority",
            owner_uid=os.getuid(),
            group_gid=os.getgid(),
        )

    hardlink = tmp_path / "authority-hardlink.jsonl"
    hardlink.hardlink_to(target)
    with pytest.raises(
        module.PreflightError,
        match="authority_file_contract_invalid",
    ):
        module._exact_shared_authority_file(
            target,
            "authority",
            owner_uid=os.getuid(),
            group_gid=os.getgid(),
        )


def test_preflight_requires_complete_canonical_release_shape() -> None:
    module = _load_preflight()
    digest = "sha256:" + "a" * 64
    manifest = {
        "schema_version": 1,
        "release_id": "platform-test",
        "git_sha": "b" * 40,
        "components": {
            label: {"distribution": f"distribution-{label}", "version": "0.1.0"}
            for label in ("core", "web", "operations")
        },
        "migrations": {
            label: {"count": 1, "latest": "0001_initial", "digest": digest}
            for label in ("web", "operations")
        },
        "migration_digest": digest,
        "lock_digest": digest,
        "deployment_digest": digest,
        "artifacts": {
            label: {
                "filename": (
                    f"{label}.whl" if label.endswith("-wheel") else f"{label}.tar.gz"
                ),
                "sha256": digest,
                "size_bytes": 1,
            }
            for label in module._ARTIFACT_LABELS
        },
        "build_digest": digest,
        "release_bundle_digest": digest,
    }
    module._validate_release_manifest_shape(manifest)
    manifest["unexpected"] = True
    with pytest.raises(module.PreflightError, match="top_level"):
        module._validate_release_manifest_shape(manifest)


def test_preflight_receipt_is_release_bound_and_health_readable() -> None:
    module = _load_preflight()
    env = {
        "RESEARCH_OPS_GIT_SHA": "a" * 40,
        "RESEARCH_OPS_RELEASE_ID": "platform-test",
        "RESEARCH_OPS_BUILD_DIGEST": "sha256:" + "b" * 64,
        "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST": "sha256:" + "c" * 64,
    }
    payload = module._receipt_payload(env, status="PASS", failure_code=None)
    assert set(payload) == {
        "schema_version",
        "status",
        "checked_at",
        "git_sha",
        "release_id",
        "build_digest",
        "release_bundle_digest",
        "failure_code",
    }
    assert payload["status"] == "PASS"
    assert payload["failure_code"] is None
    assert payload["git_sha"] == env["RESEARCH_OPS_GIT_SHA"]
    assert payload["release_bundle_digest"] == env["RESEARCH_OPS_RELEASE_BUNDLE_DIGEST"]
    datetime.fromisoformat(str(payload["checked_at"]).replace("Z", "+00:00"))
    unit = (SYSTEMD / "research-operations-preflight.service").read_text()
    assert "Group=research-ops" in unit
    assert "RuntimeDirectory=research-operations-preflight" in unit
    assert "RuntimeDirectoryMode=0750" in unit


def test_preflight_exits_fail_closed_without_configuration() -> None:
    result = subprocess.run(
        [sys.executable, str(NATIVE / "bin" / "preflight.py")],
        check=False,
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 78
    assert result.stdout == ""
    assert result.stderr.startswith("research_operations_preflight_failed:")
    assert "Traceback" not in result.stderr


def test_runtime_example_requires_owners_release_pki_and_offsite_policy() -> None:
    example = (NATIVE / "runtime.env.example").read_text()
    assert "chown root:root, chmod 0600" in example
    for key in (
        "RESEARCH_OPS_SERVICE_OWNER",
        "RESEARCH_OPS_SECURITY_OWNER",
        "RESEARCH_OPS_DATA_OWNER",
        "RESEARCH_OPS_ON_CALL_OWNER",
        "RESEARCH_OPS_INCIDENT_COMMANDER",
        "RESEARCH_OPS_BACKUP_OWNER",
        "RESEARCH_OPS_RECOVERY_APPROVER",
        "RESEARCH_OPS_GIT_SHA",
        "RESEARCH_OPS_BUILD_DIGEST",
        "RESEARCH_OPS_LOCK_DIGEST",
        "RESEARCH_OPS_DEPLOYMENT_DIGEST",
        "RESEARCH_OPS_RELEASE_BUNDLE_DIGEST",
        "RESEARCH_OPS_RELEASE_MANIFEST",
        "RESEARCH_OPS_PREFLIGHT_RECEIPT",
        "RESEARCH_OPS_PREFLIGHT_MAX_AGE_SECONDS",
        "RESEARCH_OPS_ENV_FILE",
        "RESEARCH_OPS_MIGRATION_USER",
        "RESEARCH_OPS_WEB_USER",
        "RESEARCH_OPS_WEB_PROXY_GROUP",
        "RESEARCH_OPS_OUTBOX_USER",
        "RESEARCH_OPS_JOB_USER",
        "RESEARCH_OPS_ALERT_USER",
        "RESEARCH_OPS_VALIDATOR_USER",
        "RESEARCH_OPS_BACKUP_USER",
        "RESEARCH_OPS_BACKUP_GROUP",
        "RESEARCH_OPS_DIAGNOSTICS_PROXY_GROUP",
        "RESEARCH_OPS_DIAGNOSTICS_USER",
        "RESEARCH_OPS_RETENTION_USER",
        "RESEARCH_OPS_PROXY_USER",
        "RESEARCH_OPS_NGINX_USER",
        "RESEARCH_OPS_NGINX_GROUP",
        "RESEARCH_OPS_EXECUTION_CAPABILITY_KEY_SOURCE_FILE",
        "RESEARCH_OPS_PKI_MINIMUM_VALIDITY_SECONDS",
        "RESEARCH_OPS_OFFSITE_EXPORT_HOOK",
        "RESEARCH_OPS_OFFSITE_RECEIPT_VERIFICATION_KEY_FILE",
        "RESEARCH_OPS_BACKUP_RETENTION_DAYS",
        "RESEARCH_OPS_RPO_SECONDS",
        "RESEARCH_OPS_RTO_SECONDS",
        "RESEARCH_OPS_POSTGRESQL_DROP_IN",
        "RESEARCH_OPS_POSTGRESQL_HBA_FILE",
        "RESEARCH_OPS_NGINX_SYSTEMD_DROP_IN",
        "RESEARCH_OPS_NGINX_MAIN_CONFIG_FILE",
        "RESEARCH_OPS_SYSTEMD_UNIT_ROOT",
        "RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY",
        "BACKUP_VERIFICATION_KEY_FILE",
        "RESEARCH_OPS_BACKUP_VERIFICATION_KEY_FILE",
        "RESEARCH_OPS_ALERT_ENDPOINT_URL_FILE",
        "RESEARCH_OPS_ALERT_WORKER_ID",
        "RESEARCH_OPS_ALERT_PRIMARY_ENDPOINT_ID",
        "RESEARCH_OPS_ALERT_ESCALATION_ENDPOINT_ID",
        "RESEARCH_OPS_ALERT_SOURCE_ACTOR_ID",
        "RESEARCH_OPS_ALERT_ESCALATION_ACTOR_ID",
        "RESEARCH_OPS_ALERT_POLL_INTERVAL_SECONDS",
        "RESEARCH_OPS_ALERT_TRANSPORT_TIMEOUT_SECONDS",
        "RESEARCH_OPS_ALERT_LEASE_SECONDS",
        "RESEARCH_OPS_ALERT_MAX_ATTEMPTS",
        "RESEARCH_OPS_ALERT_RETRY_DELAY_SECONDS",
        "RESEARCH_OPS_ALERT_ACKNOWLEDGMENT_TIMEOUT_SECONDS",
        "RESEARCH_OPS_ALERT_ESCALATION_REPEAT_SECONDS",
        "RESEARCH_OPS_ALERT_MAXIMUM_LEVEL",
        "RESEARCH_OPS_ALERT_MAXIMUM_EVALUATED_PER_CYCLE",
        "RESEARCH_OPS_ALERT_MAXIMUM_DELIVERIES_PER_CYCLE",
        "RESEARCH_OPS_ALERT_MAXIMUM_ESCALATIONS_PER_CYCLE",
        "RESEARCH_FINAL_HOLDOUT_REGISTRY_PATH",
        "RESEARCH_INDEPENDENT_VERIFIER_TRUST_STORE_PATH",
        "RESEARCH_INDEPENDENT_VERIFIER_TRUST_STORE_HASH",
        "RESEARCH_DATASET_TRANSFORMATION_TRUST_STORE_PATH",
        "RESEARCH_DATASET_TRANSFORMATION_TRUST_STORE_HASH",
    ):
        assert f"{key}=" in example
    assert "RESEARCH_OPS_OFFSITE_REQUIRED=true" in example
    assert "RESEARCH_OPS_LEGAL_HOLD_ENFORCEMENT=true" in example
    assert "RESEARCH_RUNTIME_PROFILE=operated" in example
    assert "RESEARCH_OPS_WEB_USER=research-web" in example
    assert "RESEARCH_OPS_PROXY_USER=research-proxy" in example
    assert "RESEARCH_OPS_NGINX_USER=research-proxy" in example
    assert "RESEARCH_OPS_NGINX_GROUP=research-proxy" in example
    assert "www-data" not in example
    assert "RESEARCH_OPS_SERVICE_USER=" not in example
    assert (
        "BACKUP_VERIFICATION_KEY_FILE=/etc/research-ops/backup-signing.pub" in example
    )
    assert "/etc/research-ops/secrets/backup-signing.pub" not in example
    assert (
        "RESEARCH_OPS_EXECUTION_CAPABILITY_KEY_SOURCE_FILE="
        "/etc/research-ops/secrets/operated-execution.key"
    ) in example
    env = {
        key: value
        for line in example.splitlines()
        if line and not line.startswith("#") and "=" in line
        for key, value in (line.split("=", 1),)
    }
    module = _load_preflight()
    module._validate_native_path_contracts(env)
    env["RESEARCH_DATA_ROOT"] = "/tmp/unqualified"
    with pytest.raises(module.PreflightError, match="native_path_contract_invalid"):
        module._validate_native_path_contracts(env)


def test_preflight_byte_attests_dedicated_nginx_main_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_preflight()
    source_root = tmp_path / "release"
    native = source_root / "services/research_operations/deploy/native"
    source_postgresql = native / "postgresql"
    source_nginx = native / "nginx"
    source_drop_in = source_nginx / "nginx.service.d"
    source_systemd = native / "systemd"
    for directory in (
        source_postgresql,
        source_drop_in,
        source_systemd,
    ):
        directory.mkdir(parents=True)
    source_files = {
        source_postgresql / "90-research-operations.conf": "postgresql\n",
        source_postgresql / "pg_hba.conf": "hba\n",
        source_nginx / "nginx.conf": "user research-proxy research-proxy;\n",
        source_drop_in / "research-operations.conf": "[Service]\n",
        source_systemd / "research-operations.target": "[Unit]\n",
    }
    for path, content in source_files.items():
        path.write_text(content, encoding="utf-8")

    installed = tmp_path / "installed"
    installed.mkdir()
    installed_postgresql = installed / "postgresql.conf"
    installed_hba = installed / "pg_hba.conf"
    installed_main = installed / "nginx.conf"
    installed_drop_in = installed / "nginx-drop-in.conf"
    unit_root = installed / "systemd"
    unit_root.mkdir()
    installed_postgresql.write_text("postgresql\n", encoding="utf-8")
    installed_hba.write_text("hba\n", encoding="utf-8")
    installed_main.write_text(
        "user research-proxy research-proxy;\n",
        encoding="utf-8",
    )
    installed_drop_in.write_text("[Service]\n", encoding="utf-8")
    (unit_root / "research-operations.target").write_text(
        "[Unit]\n",
        encoding="utf-8",
    )
    qualification = installed / "qualification.json"
    qualification.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "PASS",
                "scope": "single-host",
                "roots": [
                    {"role": role}
                    for role in (
                        "data",
                        "artifact",
                        "report",
                        "cache",
                        "identity_registry",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    env = {
        "RESEARCH_OPS_SOURCE_ROOT": str(source_root),
        "RESEARCH_OPS_POSTGRESQL_DROP_IN": str(installed_postgresql),
        "RESEARCH_OPS_POSTGRESQL_HBA_FILE": str(installed_hba),
        "RESEARCH_OPS_NGINX_MAIN_CONFIG_FILE": str(installed_main),
        "RESEARCH_OPS_NGINX_SYSTEMD_DROP_IN": str(installed_drop_in),
        "RESEARCH_OPS_SYSTEMD_UNIT_ROOT": str(unit_root),
        "RESEARCH_OPS_FILESYSTEM_QUALIFICATION_RECEIPT": str(qualification),
        "RESEARCH_OPS_NGINX_CONFIG_FILE": (
            "/etc/nginx/conf.d/research-operations.conf"
        ),
    }
    monkeypatch.setattr(
        module,
        "_public_file",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        module,
        "_root_public_file",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    observed_commands: list[list[str]] = []

    def run(arguments, **_kwargs):
        observed_commands.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(module.subprocess, "run", run)

    module._validate_runtime_files(env)
    assert observed_commands[-1] == [
        "/usr/sbin/nginx",
        "-t",
        "-q",
        "-c",
        str(installed_main),
    ]

    installed_main.write_text("user unreviewed;\n", encoding="utf-8")
    with pytest.raises(
        module.PreflightError,
        match="systemd_native_configuration_drift",
    ):
        module._validate_runtime_files(env)


def test_nginx_renderer_is_atomic_and_rejects_example_dns(tmp_path: Path) -> None:
    script = NATIVE / "bin" / "render-nginx.py"
    template = NATIVE / "nginx" / "research-operations.conf.template"
    output = tmp_path / "research-operations.conf"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--template",
            str(template),
            "--output",
            str(output),
            "--server-name",
            "research.internal.corp",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    rendered = output.read_text()
    assert "@@EMPLOYEE_SERVER_NAME@@" not in rendered
    assert rendered.count("research.internal.corp") == 4
    rejected = subprocess.run(
        [
            sys.executable,
            str(script),
            "--template",
            str(template),
            "--output",
            str(output),
            "--server-name",
            "research.internal.example",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert rejected.returncode != 0


@pytest.mark.parametrize("algorithm", ["RSA", "ED25519"])
def test_offsite_receipt_binds_signed_remote_export_to_manifest(
    tmp_path: Path,
    algorithm: str,
) -> None:
    backup_id = "11111111-2222-4333-8444-555555555555"
    backup = tmp_path / backup_id
    backup.mkdir()
    manifest = backup / "manifest.json"
    manifest.write_text('{"schema_version":1}\n')
    manifest_hash = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    private_key, public_key = _key_pair(
        tmp_path,
        "offsite",
        algorithm=algorithm,
    )
    receipt = tmp_path / "receipt.json"
    receipt.write_text(
        json.dumps(
            _signed_offsite_receipt(
                tmp_path,
                private_key=private_key,
                algorithm=algorithm,
                backup_id=backup_id,
                manifest_hash=manifest_hash,
            ),
            sort_keys=True,
        )
    )
    receipt.chmod(0o600)
    command = [
        sys.executable,
        str(NATIVE / "bin" / "verify-offsite-receipt.py"),
        "--receipt",
        str(receipt),
        "--backup-directory",
        str(backup),
        "--backup-id",
        backup_id,
        "--target-id",
        "approved-vault",
        "--encryption",
        "kms-envelope",
        "--encryption-key-id",
        "kms-key-version-7",
        "--verification-public-key",
        str(public_key),
    ]
    passed = subprocess.run(command, check=False, text=True, capture_output=True)
    assert passed.returncode == 0, passed.stderr
    staging_backup = tmp_path / f".staging-{backup_id}"
    staging_backup.mkdir()
    (staging_backup / "manifest.json").write_bytes(manifest.read_bytes())
    staging_command = list(command)
    staging_command[staging_command.index("--backup-directory") + 1] = str(
        staging_backup
    )
    staging_command.append("--allow-staging-directory")
    staging_passed = subprocess.run(
        staging_command,
        check=False,
        text=True,
        capture_output=True,
    )
    assert staging_passed.returncode == 0, staging_passed.stderr
    document = json.loads(receipt.read_text())
    document["remote_object_version"] = "attacker-replaced-version"
    receipt.write_text(json.dumps(document, sort_keys=True))
    signature_failed = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
    )
    assert signature_failed.returncode != 0
    assert "signature" in signature_failed.stderr
    receipt.write_text(
        json.dumps(
            _signed_offsite_receipt(
                tmp_path,
                private_key=private_key,
                algorithm=algorithm,
                backup_id=backup_id,
                manifest_hash=manifest_hash,
            ),
            sort_keys=True,
        )
    )
    manifest.write_text("tampered\n")
    failed = subprocess.run(command, check=False, text=True, capture_output=True)
    assert failed.returncode != 0
    assert "manifest_binding" in failed.stderr


def test_retention_is_dry_run_and_respects_minimum_and_legal_hold(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    receipt_root = tmp_path / "receipts"
    backup_root.mkdir()
    receipt_root.mkdir()
    backup_private_key, backup_public_key = _key_pair(tmp_path, "backup")
    offsite_private_key, offsite_public_key = _key_pair(
        tmp_path,
        "offsite",
        algorithm="ED25519",
    )
    identifiers = [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
        "00000000-0000-4000-8000-000000000003",
        "00000000-0000-4000-8000-000000000004",
    ]
    old = datetime(2020, 1, 1, tzinfo=UTC)
    for index, identifier in enumerate(identifiers):
        backup = backup_root / identifier
        backup.mkdir()
        files = {
            "postgresql": "postgresql.dump",
            "data": "data.tar",
            "manifest": "manifest.tar",
            "artifact": "artifact.tar",
            "report": "report.tar",
            "identity_registry": "identity.tar",
        }
        for relative in files.values():
            (backup / relative).write_bytes(f"{identifier}:{relative}".encode())
        verified = create_signed_backup_manifest(
            backup_directory=backup,
            files=files,
            signing_private_key=backup_private_key,
            verification_public_key=backup_public_key,
            backup_id=identifier,
            fence_token=identifier,
            fence_generation=index + 1,
            git_sha="1" * 40,
            release_id="retention-test",
            build_digest="sha256:" + "b" * 64,
            release_bundle_digest="sha256:" + "c" * 64,
            postgresql_major=16,
            audit_row_count=0,
            audit_terminal_hash="",
            created_at=old + timedelta(seconds=index),
        )
        (backup / "verification.json").write_text(
            json.dumps(verified.as_dict(), sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="ascii",
        )
        offsite_receipt = receipt_root / f"{identifier}.json"
        offsite_receipt.write_text(
            json.dumps(
                _signed_offsite_receipt(
                    tmp_path,
                    private_key=offsite_private_key,
                    algorithm="ED25519",
                    backup_id=identifier,
                    manifest_hash=verified.manifest_hash,
                    uploaded_at=old + timedelta(seconds=index),
                ),
                sort_keys=True,
            )
        )
        offsite_receipt.chmod(0o600)
    (backup_root / identifiers[0] / "LEGAL_HOLD").touch()
    active_staging = backup_root / f".staging-{identifiers[0]}"
    active_staging.mkdir()
    (active_staging / "partial-upload").write_text("not-final")
    command = [
        sys.executable,
        str(NATIVE / "bin" / "backup-retention.py"),
        "--dry-run",
        "--backup-root",
        str(backup_root),
        "--receipt-root",
        str(receipt_root),
        "--backup-verification-public-key",
        str(backup_public_key),
        "--offsite-receipt-verification-public-key",
        str(offsite_public_key),
        "--target-id",
        "approved-vault",
        "--encryption",
        "kms-envelope",
        "--encryption-key-id",
        "kms-key-version-7",
        "--retention-days",
        "7",
        "--minimum-count",
        "2",
    ]
    result = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["mode"] == "dry-run"
    assert plan["complete_count"] == len(identifiers)
    assert plan["eligible_backup_ids"] == [identifiers[1]]
    assert plan["legal_hold_backup_ids"] == [identifiers[0]]
    assert all((backup_root / identifier).exists() for identifier in identifiers)
    assert active_staging.exists()

    (backup_root / identifiers[2] / "data.tar").write_text("tampered")
    rejected = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
    )
    assert rejected.returncode == 2, rejected.stderr
    rejected_plan = json.loads(rejected.stdout)
    assert identifiers[2] in rejected_plan["incomplete_backup_ids"]
    assert identifiers[2] not in rejected_plan["eligible_backup_ids"]

    (backup_root / identifiers[2] / "data.tar").write_bytes(
        f"{identifiers[2]}:data.tar".encode()
    )
    (backup_root / identifiers[0] / "data.tar").write_text("tampered-held")
    held_corrupt = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
    )
    assert held_corrupt.returncode == 2, held_corrupt.stderr
    held_corrupt_plan = json.loads(held_corrupt.stdout)
    assert identifiers[0] in held_corrupt_plan["legal_hold_backup_ids"]
    assert identifiers[0] in held_corrupt_plan["incomplete_backup_ids"]
    assert identifiers[0] not in held_corrupt_plan["eligible_backup_ids"]


def test_systemd_units_and_shell_are_syntactically_valid() -> None:
    verified = subprocess.run(
        ["systemd-analyze", "verify", *map(str, sorted(SYSTEMD.iterdir()))],
        check=False,
        text=True,
        capture_output=True,
    )
    assert verified.returncode == 0, verified.stderr
    shell = subprocess.run(
        [
            "/bin/sh",
            "-n",
            str(NATIVE / "bin" / "native-backup.sh"),
            str(NATIVE / "bin" / "bootstrap-postgresql.sh"),
            str(ROOT / "scripts" / "create-backup.sh"),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert shell.returncode == 0, shell.stderr


@pytest.mark.parametrize("unsafe", ["symlink", "permissive"])
def test_backup_runtime_directory_contract_fails_before_fencing(
    tmp_path: Path, unsafe: str
) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o700)
    actual = tmp_path / "actual-runtime"
    actual.mkdir(mode=0o700)
    runtime = tmp_path / "runtime"
    if unsafe == "symlink":
        runtime.symlink_to(actual, target_is_directory=True)
    else:
        runtime.mkdir(mode=0o755)
        runtime.chmod(0o755)
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "scripts/create-backup.sh")],
        check=False,
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "BACKUP_ROOT": str(backup_root),
            "BACKUP_OPERATOR_ID": "test-operator",
            "POSTGRES_MAJOR": "16",
            "RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY": str(runtime),
        },
    )
    assert result.returncode == 65
    assert not list(backup_root.iterdir())


def test_native_backup_uses_one_owner_only_runtime_receipt_contract() -> None:
    create = (ROOT / "scripts/create-backup.sh").read_text()
    wrapper = (NATIVE / "bin/native-backup.sh").read_text()
    unit = (SYSTEMD / "research-operations-backup.service").read_text()
    assert (
        "RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY:="
        "/run/research-operations-backup" in create
    )
    assert 'receipt="$runtime_directory/backup-fence-$backup_id.json"' in create
    assert "umask 027" in create
    assert 'mkdir -m 0750 "$staging"' in create
    assert "backup-fence reconcile --receipt" in create
    assert 'mktemp "$runtime_directory/backup-output.XXXXXX"' in wrapper
    assert "umask 027" in wrapper
    assert "stat -c '%a' -- \"$candidate\"" in wrapper
    assert '" = 640 || exit 65' in wrapper
    assert "BACKUP_DEFER_FINALIZATION=true" in wrapper
    assert 'receipt_attempt="$receipt_root/.staging-' in wrapper
    assert 'ln -- "$receipt_attempt" "$receipt"' in wrapper
    assert 'mv -n -T -- "$backup" "$final"' in wrapper
    assert (
        wrapper.index('"$RESEARCH_OPS_OFFSITE_EXPORT_HOOK" export')
        < wrapper.index('ln -- "$receipt_attempt" "$receipt"')
        < wrapper.index('mv -n -T -- "$backup" "$final"')
    )
    assert ': "${BACKUP_DEFER_FINALIZATION:=false}"' in create
    assert create.index("backup-fence reopen") < create.rindex(
        'if test "$BACKUP_DEFER_FINALIZATION" = true'
    )
    assert (
        "RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY=/run/research-operations-backup" in unit
    )
    assert "RuntimeDirectoryMode=0700" in unit
    assert "UMask=0027" in unit
    assert "--verification-public-key" in wrapper
    assert "--backup-verification-public-key" in wrapper
    retention = (SYSTEMD / "research-operations-retention-audit.service").read_text()
    assert "--offsite-receipt-verification-public-key" in retention
    assert "SupplementaryGroups=research-backup" in retention


def test_native_postgresql_bootstrap_is_tls_scram_and_idempotent() -> None:
    drop_in = (NATIVE / "postgresql/90-research-operations.conf").read_text()
    hba = (NATIVE / "postgresql/pg_hba.conf").read_text()
    bootstrap = (NATIVE / "bin/bootstrap-postgresql.sh").read_text()
    assert "listen_addresses = '127.0.0.1,::1'" in drop_in
    assert "ssl = on" in drop_in
    assert "ssl_cert_file = '/etc/research-ops/pki/postgres.crt'" in drop_in
    assert "ssl_key_file = '/etc/research-ops/pki/postgres.key'" in drop_in
    assert "hba_file = '/etc/research-ops/postgresql/pg_hba.conf'" in drop_in
    assert "trust" not in hba
    assert hba.count("scram-sha-256") == 10
    assert "host    all       all                  0.0.0.0/0" in hba
    assert "CREATE ROLE" in bootstrap and "WHERE NOT EXISTS" in bootstrap
    assert "ALTER ROLE" in bootstrap
    assert "CREATE DATABASE" in bootstrap
    assert "REVOKE ALL ON DATABASE" in bootstrap
    assert "PGSSLMODE=verify-full" in bootstrap
    assert "\\getenv runtime_password" in bootstrap
    assert "--set=runtime_password" not in bootstrap
