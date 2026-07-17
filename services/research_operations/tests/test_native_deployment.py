from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from research_operations.backup import create_signed_backup_manifest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "deploy" / "native"
SYSTEMD = NATIVE / "systemd"


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


def test_native_systemd_is_the_single_official_deployment() -> None:
    assert (ROOT / "deploy" / "OFFICIAL_DEPLOYMENT").read_text().strip() == (
        "native-systemd"
    )
    compose = (ROOT / "deploy" / "compose.yaml").read_text()
    assert compose.startswith("# NON-OFFICIAL REFERENCE ONLY.")
    assert "research-operations-reference" in compose
    reference = (ROOT / "deploy" / "compose-reference.md").read_text()
    assert "not the supported production deployment" in reference


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
        ("research-operations-validator.service", "30s"),
    ],
)
def test_long_running_units_are_supervised_and_hardened(
    name: str, timeout: str
) -> None:
    unit = (SYSTEMD / name).read_text()
    expected_user = (
        "User=research-web"
        if name == "research-operations-web.service"
        else "User=research-ops"
    )
    for contract in (
        expected_user,
        "Group=research-ops",
        "EnvironmentFile=/etc/research-ops/runtime.env",
        "Requires=research-operations-preflight.service",
        "research-operations-migrate.service",
        "Restart=on-failure",
        "KillSignal=SIGTERM",
        "KillMode=mixed",
        f"TimeoutStopSec={timeout}",
        "NoNewPrivileges=true",
        "CapabilityBoundingSet=",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "RestrictNamespaces=true",
        "MemoryDenyWriteExecute=true",
        "TasksMax=",
        "MemoryMax=",
        "CPUQuota=",
        "StandardOutput=journal",
        "StandardError=journal",
    ):
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
    web = (SYSTEMD / "research-operations-web.service").read_text()
    assert "User=research-web" in web
    assert "Group=research-ops" in web
    assert "LoadCredential=" not in web
    assert "RuntimeDirectory=research-operations-web" in web
    assert (
        'worker_tmp_dir = "/run/research-operations-web"'
        in (NATIVE / "gunicorn-web.conf.py").read_text()
    )
    validator = (SYSTEMD / "research-operations-validator.service").read_text()
    assert "scripts/audit-validator-loop.sh" in validator
    assert "RESEARCH_OPS_DATABASE_ROLE=validator" in validator


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
    assert 'bind = "unix:/run/research-operations-web/web.sock"' in web
    assert 'bind = "unix:/run/research-operations-ops-api/ops-api.sock"' in operations
    assert "server unix:/run/research-operations-web/web.sock" in proxy
    assert "server unix:/run/research-operations-ops-api/ops-api.sock" in proxy
    assert "umask = 0o117" in web
    assert "umask = 0o117" in operations
    drop_in = (NATIVE / "nginx/nginx.service.d/research-operations.conf").read_text()
    assert "SupplementaryGroups=research-ops" in drop_in
    assert (
        "RuntimeDirectoryMode=0750"
        in (SYSTEMD / "research-operations-web.service").read_text()
    )
    assert (
        "RuntimeDirectoryMode=0750"
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
        "RESEARCH_OPS_WEB_USER",
        "RESEARCH_OPS_EXECUTION_CAPABILITY_KEY_SOURCE_FILE",
        "RESEARCH_OPS_PKI_MINIMUM_VALIDITY_SECONDS",
        "RESEARCH_OPS_OFFSITE_EXPORT_HOOK",
        "RESEARCH_OPS_OFFSITE_RECEIPT_VERIFICATION_KEY_FILE",
        "RESEARCH_OPS_BACKUP_RETENTION_DAYS",
        "RESEARCH_OPS_RPO_SECONDS",
        "RESEARCH_OPS_RTO_SECONDS",
        "RESEARCH_OPS_POSTGRESQL_DROP_IN",
        "RESEARCH_OPS_POSTGRESQL_HBA_FILE",
        "RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY",
    ):
        assert f"{key}=" in example
    assert "RESEARCH_OPS_OFFSITE_REQUIRED=true" in example
    assert "RESEARCH_OPS_LEGAL_HOLD_ENFORCEMENT=true" in example
    assert "RESEARCH_RUNTIME_PROFILE=operated" in example
    assert "RESEARCH_OPS_WEB_USER=research-web" in example
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
    assert plan["eligible_backup_ids"] == [identifiers[1]]
    assert plan["legal_hold_backup_ids"] == [identifiers[0]]
    assert all((backup_root / identifier).exists() for identifier in identifiers)

    (backup_root / identifiers[2] / "data.tar").write_text("tampered")
    rejected = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
    )
    assert rejected.returncode == 0, rejected.stderr
    rejected_plan = json.loads(rejected.stdout)
    assert identifiers[2] in rejected_plan["incomplete_backup_ids"]
    assert identifiers[2] not in rejected_plan["eligible_backup_ids"]


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
    assert "RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY:=/run/research-operations" in create
    assert 'receipt="$runtime_directory/backup-fence-$backup_id.json"' in create
    assert "backup-fence reconcile --receipt" in create
    assert 'mktemp "$runtime_directory/backup-output.XXXXXX"' in wrapper
    assert "RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY=/run/research-operations" in unit
    assert "RuntimeDirectoryMode=0700" in unit
    assert "--verification-public-key" in wrapper
    assert "--backup-verification-public-key" in wrapper
    retention = (SYSTEMD / "research-operations-retention-audit.service").read_text()
    assert "--offsite-receipt-verification-public-key" in retention


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
