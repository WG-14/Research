from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from market_research.application.process_sandbox import (
    IsolatedProcessPolicy,
    run_isolated_command,
)

pytestmark = pytest.mark.skipif(
    not Path("/usr/bin/bwrap").exists(), reason="Linux bubblewrap runtime required"
)


def test_operations_policy_denies_host_network_secrets_and_fork_growth(
    tmp_path: Path,
) -> None:
    secret = tmp_path.parent / f"{tmp_path.name}-operations-secret"
    secret.write_text("host-only", encoding="utf-8")
    source = f"""
from pathlib import Path
import os, socket
failures = 0
try:
    Path({str(secret)!r}).read_text()
    failures += 1
except OSError:
    pass
try:
    socket.create_connection(('1.1.1.1', 80), timeout=0.1)
    failures += 1
except OSError:
    pass
if os.environ.get('RESEARCH_OPS_DATABASE_URL') is not None:
    failures += 1
children = []
try:
    for _ in range(32):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        children.append(pid)
except OSError:
    pass
for pid in children:
    os.waitpid(pid, 0)
if len(children) >= 32:
    failures += 1
raise SystemExit(failures)
"""
    result = run_isolated_command(
        (sys.executable, "-c", source),
        cwd=Path.cwd(),
        env={
            "PATH": str(Path(sys.executable).parent),
            "PYTHONHASHSEED": "0",
            "TMPDIR": "/tmp",
        },
        readable_roots=(Path.cwd(), Path(sys.prefix)),
        writable_roots=(tmp_path,),
        policy=IsolatedProcessPolicy(
            wall_timeout_seconds=5,
            memory_limit_mb=256,
            output_limit_bytes=4096,
            process_limit=8,
            file_descriptor_limit=64,
            network_access=False,
        ),
        output_path=tmp_path / "operations-sandbox.log",
    )

    assert result.status == "succeeded", result.output
    assert result.isolation["network_access"] == "denied_namespace"


def test_operations_sandbox_exposes_only_dedicated_holdout_authority_root(
    tmp_path: Path,
) -> None:
    sandbox_root = tmp_path / "job"
    authority_root = tmp_path / "holdout-authority"
    sibling_registry_root = tmp_path / "other-registries"
    for root in (sandbox_root, authority_root, sibling_registry_root):
        root.mkdir()
    authority = authority_root / "final_holdout_authority.jsonl"
    authority_lock = authority_root / "final_holdout_authority.jsonl.lock"
    authority.write_text("reserved\n", encoding="utf-8")
    authority_lock.write_bytes(b"")
    source = f"""
from pathlib import Path
job = Path({str(sandbox_root)!r})
authority = Path({str(authority)!r})
authority_lock = Path({str(authority_lock)!r})
authority_root = authority.parent
sibling = Path({str(sibling_registry_root)!r})
(job / 'result').write_text('ok')
with authority.open('a') as handle:
    handle.write('activated\\n')
with authority_lock.open('a') as handle:
    handle.write('lock-used\\n')
try:
    (authority_root / 'bypass').write_text('must-fail')
except OSError:
    pass
else:
    raise SystemExit(96)
try:
    authority.unlink()
except OSError:
    pass
else:
    raise SystemExit(97)
try:
    (sibling / 'bypass').write_text('must-fail')
except OSError:
    raise SystemExit(0)
raise SystemExit(97)
"""

    result = run_isolated_command(
        (sys.executable, "-c", source),
        cwd=sandbox_root,
        env={
            "PATH": str(Path(sys.executable).parent),
            "PYTHONHASHSEED": "0",
            "TMPDIR": "/tmp",
        },
        readable_roots=(Path(sys.prefix),),
        writable_roots=(sandbox_root, authority, authority_lock),
        policy=IsolatedProcessPolicy(
            wall_timeout_seconds=5,
            memory_limit_mb=256,
            output_limit_bytes=4096,
            process_limit=8,
            file_descriptor_limit=64,
            network_access=False,
        ),
        output_path=sandbox_root / "operations-sandbox.log",
    )

    assert result.status == "succeeded", result.output
    assert (sandbox_root / "result").read_text() == "ok"
    assert authority.read_text() == "reserved\nactivated\n"
    assert authority_lock.read_text() == "lock-used\n"
    assert not (authority_root / "bypass").exists()
    assert not (sibling_registry_root / "bypass").exists()
    assert str(authority.resolve()) in result.isolation["writable_roots"]
    assert str(authority_lock.resolve()) in result.isolation["writable_roots"]
    assert str(authority_root.resolve()) not in result.isolation["writable_roots"]
    assert str(tmp_path.resolve()) not in result.isolation["writable_roots"]


def test_effective_systemd_contract_starts_bwrap_and_denies_nested_userns() -> None:
    probe = subprocess.run(
        ("systemd-run", "--user", "--wait", "--pipe", "/usr/bin/true"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip("systemd user manager is unavailable")

    command = (
        "systemd-run",
        "--user",
        "--wait",
        "--pipe",
        "-p",
        "NoNewPrivileges=yes",
        "-p",
        "CapabilityBoundingSet=",
        "-p",
        "PrivateTmp=yes",
        "-p",
        "PrivateDevices=yes",
        "-p",
        "ProtectSystem=strict",
        "-p",
        "ProtectHome=yes",
        "-p",
        "ProtectProc=invisible",
        "-p",
        "ProtectKernelTunables=no",
        "-p",
        "ProtectKernelModules=yes",
        "-p",
        "ProtectKernelLogs=yes",
        "-p",
        "ProtectControlGroups=yes",
        "-p",
        "ProtectClock=yes",
        "-p",
        "RestrictNamespaces=mnt user ipc pid uts net",
        "-p",
        "RestrictSUIDSGID=yes",
        "-p",
        "LockPersonality=yes",
        "-p",
        "MemoryDenyWriteExecute=yes",
        "-p",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "-p",
        "SystemCallArchitectures=native",
        "/usr/bin/bwrap",
        "--unshare-user",
        "--disable-userns",
        "--assert-userns-disabled",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--unshare-net",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "/bin/sh",
        "-c",
        (
            "if /usr/bin/unshare --user --map-root-user /usr/bin/id -u; "
            "then exit 97; fi; exit 0"
        ),
    )
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Finished with result: success" in completed.stdout + completed.stderr
