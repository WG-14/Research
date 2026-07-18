from __future__ import annotations

from pathlib import Path
import sys

import pytest

from market_research.research.isolated_process import (
    IsolatedProcessError,
    IsolatedProcessPolicy,
    run_isolated_command,
)


pytestmark = pytest.mark.skipif(
    not Path("/usr/bin/bwrap").exists(), reason="Linux bubblewrap runtime required"
)


def _run(tmp_path: Path, source: str, *, timeout: float = 2.0, output: int = 4096):
    return run_isolated_command(
        (sys.executable, "-c", source),
        cwd=Path.cwd(),
        env={"PATH": str(Path(sys.executable).parent), "PYTHONHASHSEED": "0"},
        readable_roots=(Path.cwd(), Path(sys.prefix)),
        writable_roots=(tmp_path,),
        policy=IsolatedProcessPolicy(
            wall_timeout_seconds=timeout,
            memory_limit_mb=192,
            output_limit_bytes=output,
            process_limit=16,
        ),
        output_path=tmp_path / f"run-{len(tuple(tmp_path.iterdir()))}.log",
    )


def test_infinite_loop_times_out_without_poisoning_next_strategy(
    tmp_path: Path,
) -> None:
    timed_out = _run(tmp_path, "while True: pass", timeout=0.2)
    succeeded = _run(tmp_path, "print('independent-success')")

    assert timed_out.status == "timed_out"
    assert timed_out.failure_reason == "wall_timeout_exceeded"
    assert succeeded.status == "succeeded"
    assert "independent-success" in succeeded.output


def test_network_namespace_and_read_only_repository_are_enforced(
    tmp_path: Path,
) -> None:
    source = """
from pathlib import Path
import socket
network_denied = False
try:
    socket.create_connection(('1.1.1.1', 80), timeout=0.1)
except OSError:
    network_denied = True
repo_denied = False
try:
    Path('sandbox-write-probe').write_text('forbidden')
except OSError:
    repo_denied = True
raise SystemExit(0 if network_denied and repo_denied else 9)
"""
    result = _run(tmp_path, source)

    assert result.status == "succeeded"
    assert result.isolation["network_access"] == "denied_namespace"
    assert not Path("sandbox-write-probe").exists()


def test_output_and_memory_failures_are_quarantined_or_classified(
    tmp_path: Path,
) -> None:
    oversized = _run(tmp_path, "print('x' * 100000)", output=1024)
    exhausted = _run(tmp_path, "bytearray(512 * 1024 * 1024)")

    assert oversized.status == "quarantined"
    assert oversized.failure_reason == "output_limit_exceeded"
    assert exhausted.status == "resource_exhausted"
    assert exhausted.failure_reason == "memory_limit_exceeded"


def test_host_files_and_parent_secrets_are_not_visible(
    tmp_path: Path,
) -> None:
    host_secret = tmp_path.parent / f"{tmp_path.name}-host-secret.txt"
    host_secret.write_text("must-not-be-visible", encoding="utf-8")
    forbidden_write = tmp_path.parent / f"{tmp_path.name}-escaped-write.txt"
    source = f"""
from pathlib import Path
import os
failures = 0
try:
    Path({str(host_secret)!r}).read_text()
    failures += 1
except OSError:
    pass
try:
    Path({str(forbidden_write)!r}).write_text('escape')
    failures += 1
except OSError:
    pass
if os.environ.get('OPERATIONS_DATABASE_PASSWORD') is not None:
    failures += 1
raise SystemExit(failures)
"""
    result = _run(tmp_path, source)

    assert result.status == "succeeded"
    assert not forbidden_write.exists()


def test_missing_sandbox_runtime_fails_before_strategy_code(
    tmp_path: Path,
    monkeypatch,
) -> None:
    sentinel = tmp_path / "must-not-run"
    from market_research.research import isolated_process

    real_which = isolated_process.shutil.which
    monkeypatch.setattr(
        isolated_process.shutil,
        "which",
        lambda name: None if name == "bwrap" else real_which(name),
    )
    with pytest.raises(IsolatedProcessError, match="runtime_missing:bwrap"):
        _run(tmp_path, f"open({str(sentinel)!r}, 'w').write('ran')")
    assert not sentinel.exists()
