from __future__ import annotations

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
