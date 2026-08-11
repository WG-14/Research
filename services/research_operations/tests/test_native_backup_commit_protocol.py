from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

OPERATIONS_ROOT = Path(__file__).resolve().parents[1]
NATIVE_BACKUP = OPERATIONS_ROOT / "deploy" / "native" / "bin" / "native-backup.sh"
CREATE_BACKUP = OPERATIONS_ROOT / "scripts" / "create-backup.sh"
BACKUP_ID = "11111111-2222-4333-8444-555555555555"


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _protocol_fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    service_root = tmp_path / "service"
    native_bin = service_root / "deploy" / "native" / "bin"
    native_bin.mkdir(parents=True)
    wrapper = native_bin / "native-backup.sh"
    shutil.copy2(NATIVE_BACKUP, wrapper)

    backup_root = tmp_path / "backups"
    receipt_root = tmp_path / "receipts"
    runtime_root = tmp_path / "runtime"
    for root in (backup_root, receipt_root, runtime_root):
        root.mkdir(mode=0o700)
        root.chmod(0o700)

    _write_executable(
        service_root / "scripts" / "create-backup.sh",
        f"""#!/bin/sh
set -eu
test "${{BACKUP_DEFER_FINALIZATION:-}}" = true
backup_id=${{BACKUP_RESUME_ID:-{BACKUP_ID}}}
staging="$BACKUP_ROOT/.staging-$backup_id"
if test ! -e "$staging"; then
  mkdir -m 0750 "$staging"
  printf '%s\n' '{{"schema_version":1}}' >"$staging/manifest.json"
fi
printf '%s\n' "$staging"
""",
    )
    _write_executable(
        native_bin / "verify-offsite-receipt.py",
        """#!/usr/bin/python3
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--receipt", type=Path, required=True)
parser.add_argument("--backup-directory", type=Path, required=True)
parser.add_argument("--backup-id", required=True)
parser.add_argument("--allow-staging-directory", action="store_true")
parser.add_argument("--target-id")
parser.add_argument("--encryption")
parser.add_argument("--encryption-key-id")
parser.add_argument("--verification-public-key")
args = parser.parse_args()
expected = (
    f".staging-{args.backup_id}"
    if args.allow_staging_directory
    else args.backup_id
)
if args.backup_directory.name != expected or not args.receipt.is_file():
    raise SystemExit(65)
""",
    )
    _write_executable(
        native_bin / "backup-retention.py",
        """#!/usr/bin/python3
import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--backup-root", type=Path, required=True)
parser.add_argument("--receipt-root", type=Path, required=True)
parser.add_argument("--backup-verification-public-key")
parser.add_argument("--offsite-receipt-verification-public-key")
parser.add_argument("--target-id")
parser.add_argument("--encryption")
parser.add_argument("--encryption-key-id")
parser.add_argument("--retention-days")
parser.add_argument("--minimum-count")
parser.add_argument("--dry-run", action="store_true")
args = parser.parse_args()
backup_id = "11111111-2222-4333-8444-555555555555"
if not (args.backup_root / backup_id).is_dir():
    raise SystemExit(65)
if (args.backup_root / f".staging-{backup_id}").exists():
    raise SystemExit(65)
if not (args.receipt_root / f"{backup_id}.json").is_file():
    raise SystemExit(65)
print('{"mode":"dry-run"}')
""",
    )
    hook = tmp_path / "offsite-hook"
    _write_executable(
        hook,
        """#!/usr/bin/python3
import os
import sys
from pathlib import Path

if sys.argv[1] != "export":
    raise SystemExit(64)
arguments = dict(zip(sys.argv[2::2], sys.argv[3::2], strict=True))
backup = Path(arguments["--backup-directory"])
receipt = Path(arguments["--receipt"])
backup_id = backup.name.removeprefix(".staging-")
final = Path(os.environ["BACKUP_ROOT"]) / backup_id
if backup.name != f".staging-{backup_id}" or final.exists():
    raise SystemExit(65)
if not receipt.name.startswith(f".staging-{backup_id}-"):
    raise SystemExit(65)
descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    handle.write('{"attempt":true}\\n')
os.chmod(receipt, 0o640)
with Path(os.environ["HOOK_LOG"]).open("a", encoding="utf-8") as log:
    log.write("upload_saw_hidden_final\\n")
if os.environ.get("HOOK_MODE") == "fail":
    raise SystemExit(75)
""",
    )

    backup_key = tmp_path / "backup.pub"
    offsite_key = tmp_path / "offsite.pub"
    backup_key.write_text("test", encoding="utf-8")
    offsite_key.write_text("test", encoding="utf-8")
    environment = {
        "PATH": "/usr/bin:/bin",
        "BACKUP_ROOT": str(backup_root),
        "RESEARCH_OPS_OFFSITE_RECEIPT_ROOT": str(receipt_root),
        "RESEARCH_OPS_OFFSITE_EXPORT_HOOK": str(hook),
        "RESEARCH_OPS_OFFSITE_TARGET_ID": "approved-vault",
        "RESEARCH_OPS_BACKUP_ENCRYPTION": "kms-envelope",
        "RESEARCH_OPS_BACKUP_ENCRYPTION_KEY_ID": "kms-key-version-7",
        "RESEARCH_OPS_BACKUP_RETENTION_DAYS": "30",
        "RESEARCH_OPS_BACKUP_RETENTION_MINIMUM_COUNT": "2",
        "RESEARCH_OPS_BACKUP_VERIFICATION_KEY_FILE": str(backup_key),
        "RESEARCH_OPS_OFFSITE_RECEIPT_VERIFICATION_KEY_FILE": str(offsite_key),
        "RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY": str(runtime_root),
        "HOOK_LOG": str(tmp_path / "hook.log"),
    }
    return wrapper, environment


def _run_wrapper(
    wrapper: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(wrapper)],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )


def test_create_backup_defers_uuid_publication_and_resumes_open_fence(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    state = tmp_path / "fence-state"
    _write_executable(
        fake_bin / "research-ops",
        r"""#!/usr/bin/python3
import json
import os
import sys
from pathlib import Path

arguments = sys.argv[1:]
state = Path(os.environ["FAKE_FENCE_STATE"])

def option(name: str) -> str:
    return arguments[arguments.index(name) + 1]

if arguments[:2] == ["backup-fence", "begin"]:
    receipt = Path(option("--receipt"))
    receipt.write_text("{}\n", encoding="utf-8")
    receipt.chmod(0o600)
    state.write_text("DRAINING", encoding="ascii")
elif arguments[:2] == ["backup-fence", "status"]:
    print(json.dumps({"phase": state.read_text(), "counts": {"jobs": 0}}))
elif arguments[:2] == ["backup-fence", "seal"]:
    state.write_text("SEALED", encoding="ascii")
elif arguments[:2] == ["backup-fence", "reopen"]:
    state.write_text("OPEN", encoding="ascii")
elif arguments[:2] == ["backup-fence", "reconcile"]:
    pass
elif arguments[:1] == ["audit-validate"]:
    print('{"status":"PASS"}')
elif arguments[:1] == ["backup-manifest-create"]:
    backup = Path(option("--backup-directory"))
    backup_id = option("--backup-id")
    (backup / "manifest.json").write_text("{}\n", encoding="utf-8")
    (backup / "manifest.sig").write_text("signature\n", encoding="utf-8")
    print(json.dumps({
        "status": "PASS",
        "backup_id": backup_id,
        "manifest_hash": "sha256:" + "a" * 64,
    }))
elif arguments[:1] == ["backup-verify"]:
    backup = Path(option("--backup-directory"))
    if not (backup / "manifest.json").is_file():
        raise SystemExit(65)
else:
    raise SystemExit(64)
""",
    )
    _write_executable(
        fake_bin / "pg_dump",
        r"""#!/usr/bin/python3
import sys
from pathlib import Path

arguments = sys.argv[1:]
target = Path(arguments[arguments.index("--file") + 1])
target.write_bytes(b"postgresql-dump\n")
""",
    )
    _write_executable(
        fake_bin / "tar",
        r"""#!/usr/bin/python3
import sys
from pathlib import Path

arguments = sys.argv[1:]
target = Path(arguments[arguments.index("--file") + 1])
target.write_bytes(b"archive\n")
""",
    )

    backup_root = tmp_path / "backups"
    runtime_root = tmp_path / "runtime"
    data_root = tmp_path / "data"
    artifact_root = tmp_path / "artifacts"
    report_root = tmp_path / "reports"
    registry_root = tmp_path / "registry"
    for root in (
        backup_root,
        runtime_root,
        data_root / "_internal_web" / "manifests",
        artifact_root,
        report_root,
        registry_root,
    ):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_root.chmod(0o700)
    environment = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "BACKUP_ROOT": str(backup_root),
        "BACKUP_OPERATOR_ID": "test-operator",
        "POSTGRES_MAJOR": "16",
        "RESEARCH_OPS_BACKUP_RUNTIME_DIRECTORY": str(runtime_root),
        "RESEARCH_DATA_ROOT": str(data_root),
        "RESEARCH_ARTIFACT_ROOT": str(artifact_root),
        "RESEARCH_REPORT_ROOT": str(report_root),
        "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH": str(
            registry_root / "identity.jsonl"
        ),
        "FAKE_FENCE_STATE": str(state),
        "BACKUP_DEFER_FINALIZATION": "true",
    }

    created = subprocess.run(
        ["/bin/sh", str(CREATE_BACKUP)],
        check=False,
        text=True,
        capture_output=True,
        env=environment,
    )

    assert created.returncode == 0, created.stderr
    staging = Path(created.stdout.splitlines()[-1])
    assert staging.parent == backup_root
    assert staging.name.startswith(".staging-")
    backup_id = staging.name.removeprefix(".staging-")
    final = backup_root / backup_id
    assert staging.is_dir()
    assert not final.exists()
    assert state.read_text() == "OPEN"

    resumed_hidden = subprocess.run(
        ["/bin/sh", str(CREATE_BACKUP)],
        check=False,
        text=True,
        capture_output=True,
        env={**environment, "BACKUP_RESUME_ID": backup_id},
    )
    assert resumed_hidden.returncode == 0, resumed_hidden.stderr
    assert Path(resumed_hidden.stdout.splitlines()[-1]) == staging
    assert not final.exists()

    finalized = subprocess.run(
        ["/bin/sh", str(CREATE_BACKUP)],
        check=False,
        text=True,
        capture_output=True,
        env={
            **environment,
            "BACKUP_RESUME_ID": backup_id,
            "BACKUP_DEFER_FINALIZATION": "false",
        },
    )
    assert finalized.returncode == 0, finalized.stderr
    assert Path(finalized.stdout.splitlines()[-1]) == final
    assert final.is_dir()
    assert not staging.exists()


def test_failed_upload_remains_hidden_and_exact_resume_can_commit(
    tmp_path: Path,
) -> None:
    wrapper, environment = _protocol_fixture(tmp_path)
    backup_root = Path(environment["BACKUP_ROOT"])
    receipt_root = Path(environment["RESEARCH_OPS_OFFSITE_RECEIPT_ROOT"])
    staging = backup_root / f".staging-{BACKUP_ID}"
    final = backup_root / BACKUP_ID
    receipt = receipt_root / f"{BACKUP_ID}.json"

    failed = _run_wrapper(wrapper, {**environment, "HOOK_MODE": "fail"})

    assert failed.returncode == 75, failed.stderr
    assert staging.is_dir()
    assert not final.exists()
    assert not receipt.exists()
    failed_attempts = list(receipt_root.glob(f".staging-{BACKUP_ID}-*.json"))
    assert len(failed_attempts) == 1
    assert stat.S_IMODE(failed_attempts[0].stat().st_mode) == 0o640

    resumed = _run_wrapper(
        wrapper,
        {
            **environment,
            "BACKUP_RESUME_ID": BACKUP_ID,
            "HOOK_MODE": "success",
        },
    )

    assert resumed.returncode == 0, resumed.stderr
    assert final.is_dir()
    assert not staging.exists()
    assert receipt.is_file()
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o640
    assert Path(environment["HOOK_LOG"]).read_text().splitlines() == [
        "upload_saw_hidden_final",
        "upload_saw_hidden_final",
    ]


def test_resume_after_receipt_publish_does_not_repeat_upload(tmp_path: Path) -> None:
    wrapper, environment = _protocol_fixture(tmp_path)
    backup_root = Path(environment["BACKUP_ROOT"])
    receipt_root = Path(environment["RESEARCH_OPS_OFFSITE_RECEIPT_ROOT"])
    staging = backup_root / f".staging-{BACKUP_ID}"
    staging.mkdir(mode=0o750)
    (staging / "manifest.json").write_text("{}\n", encoding="utf-8")
    receipt = receipt_root / f"{BACKUP_ID}.json"
    receipt.write_text("{}\n", encoding="utf-8")
    receipt.chmod(0o640)

    resumed = _run_wrapper(
        wrapper,
        {
            **environment,
            "BACKUP_RESUME_ID": BACKUP_ID,
            "HOOK_MODE": "fail",
        },
    )

    assert resumed.returncode == 0, resumed.stderr
    assert (backup_root / BACKUP_ID).is_dir()
    assert not staging.exists()
    assert not Path(environment["HOOK_LOG"]).exists()
