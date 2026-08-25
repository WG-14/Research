"""Linux process sandbox used by independent research batch jobs.

The strategy itself remains unaware of the host.  The parent owns the process
group, timeout, address-space/output/process limits, network namespace, and
the small set of writable research roots.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from typing import Mapping, Sequence

from market_research.storage_io import write_text_atomic


class IsolatedProcessError(RuntimeError):
    pass


_OPERATED_SANDBOX_TOOLS = {
    "bwrap": Path("/usr/bin/bwrap"),
    "prlimit": Path("/usr/bin/prlimit"),
    "timeout": Path("/usr/bin/timeout"),
}


@dataclass(frozen=True, slots=True)
class IsolatedProcessPolicy:
    wall_timeout_seconds: float
    memory_limit_mb: float
    output_limit_bytes: int
    process_limit: int = 128
    file_descriptor_limit: int = 1024
    network_access: bool = False

    def __post_init__(self) -> None:
        if self.wall_timeout_seconds <= 0:
            raise ValueError("isolated_process_wall_timeout_invalid")
        if self.memory_limit_mb <= 0:
            raise ValueError("isolated_process_memory_limit_invalid")
        if self.output_limit_bytes <= 0:
            raise ValueError("isolated_process_output_limit_invalid")
        if self.process_limit <= 0 or self.file_descriptor_limit <= 0:
            raise ValueError("isolated_process_count_limit_invalid")


@dataclass(frozen=True, slots=True)
class IsolatedProcessResult:
    returncode: int
    status: str
    failure_reason: str | None
    output: str
    isolation: Mapping[str, object]


def run_isolated_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    readable_roots: Sequence[Path],
    writable_roots: Sequence[Path],
    policy: IsolatedProcessPolicy,
    output_path: Path,
    poll_callback: Callable[[], None] | None = None,
    cancellation_requested: Callable[[], bool] | None = None,
) -> IsolatedProcessResult:
    """Run one command with fail-closed limits and bounded captured output."""
    if not command or any(not str(item) for item in command):
        raise IsolatedProcessError("isolated_process_command_invalid")
    tools = _sandbox_tools()
    resolved_cwd = cwd.expanduser().resolve(strict=True)
    resolved_readable_roots = _resolved_readable_roots(readable_roots)
    resolved_roots = _resolved_writable_roots(writable_roots)
    visible_roots = (*resolved_readable_roots, *resolved_roots)
    if not any(_is_within(resolved_cwd, root) for root in visible_roots):
        raise IsolatedProcessError("isolated_process_cwd_not_visible")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not any(_is_within(output_path, root) for root in resolved_roots):
        raise IsolatedProcessError("isolated_process_output_not_writable")
    memory_bytes = max(1, int(policy.memory_limit_mb * 1024 * 1024))
    cpu_seconds = max(1, int(math.ceil(policy.wall_timeout_seconds)))
    wall = max(0.001, float(policy.wall_timeout_seconds))
    sandbox_command = [
        str(tools["timeout"]),
        "--signal=TERM",
        "--kill-after=5s",
        f"{wall:.6f}s",
        str(tools["bwrap"]),
        "--unshare-user",
        # The parent worker may create this one user namespace, but research
        # code must not create nested namespaces and recover namespace-local
        # root capabilities inside its sandbox.
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
    ]
    for key, value in sorted(env.items()):
        sandbox_command.extend(("--setenv", key, value))
    if not policy.network_access:
        sandbox_command.append("--unshare-net")
    readonly_mounts = _system_runtime_roots() + resolved_readable_roots
    for directory in _mount_parent_directories((*readonly_mounts, *resolved_roots)):
        sandbox_command.extend(("--dir", str(directory)))
    for root in readonly_mounts:
        sandbox_command.extend(("--ro-bind", str(root), str(root)))
    # A file bind needs an existing destination inode. Seed file-shaped
    # destinations read-only while the namespace skeleton is still mutable;
    # the declared file is over-mounted writable only after the skeleton is
    # frozen. Directories already have destinations from ``--dir`` above.
    for root in resolved_roots:
        if root.is_file():
            sandbox_command.extend(("--ro-bind", str(root), str(root)))
    # Freeze the namespace skeleton and read-only inputs before adding the
    # declared writable mounts. This also makes undeclared sibling paths
    # unavailable for writes in the private tmpfs.
    sandbox_command.extend(("--remount-ro", "/", "--remount-ro", "/tmp"))
    for root in resolved_roots:
        sandbox_command.extend(("--bind", str(root), str(root)))
    sandbox_command.extend(
        (
            "--chdir",
            str(resolved_cwd),
            "--",
            str(tools["prlimit"]),
            f"--as={memory_bytes}:{memory_bytes}",
            f"--fsize={policy.output_limit_bytes}:{policy.output_limit_bytes}",
            f"--nproc={policy.process_limit}:{policy.process_limit}",
            f"--nofile={policy.file_descriptor_limit}:{policy.file_descriptor_limit}",
            f"--cpu={cpu_seconds}:{cpu_seconds}",
            "--",
            *(str(item) for item in command),
        )
    )
    partial_name: str | None = None
    returncode: int
    outer_timeout = False
    cancelled = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w+b",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".partial",
            delete=False,
        ) as stream:
            partial_name = stream.name
            process = subprocess.Popen(
                sandbox_command,
                cwd=str(resolved_cwd),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + wall + 10.0
                while process.poll() is None:
                    if poll_callback is not None:
                        poll_callback()
                    if cancellation_requested is not None and cancellation_requested():
                        cancelled = True
                        _terminate_process_group(process)
                        break
                    if time.monotonic() >= deadline:
                        outer_timeout = True
                        _terminate_process_group(process)
                        break
                    time.sleep(0.1)
                returncode = process.wait(timeout=6.0)
            except BaseException:
                _terminate_process_group(process)
                raise
        output_bytes = Path(partial_name).read_bytes()
    finally:
        if partial_name is not None:
            Path(partial_name).unlink(missing_ok=True)
    truncated = len(output_bytes) > policy.output_limit_bytes or (
        returncode != 0 and len(output_bytes) >= policy.output_limit_bytes
    )
    bounded = output_bytes[: policy.output_limit_bytes].decode(
        "utf-8", errors="replace"
    )
    if truncated:
        bounded += "\n[isolated output truncated at declared byte limit]\n"
    status, failure_reason = _classify_result(
        returncode,
        outer_timeout=outer_timeout,
        cancelled=cancelled,
        truncated=truncated,
        output=bounded,
    )
    isolation = {
        "schema_version": 1,
        "process_model": "bubblewrap_process_namespace",
        "filesystem_root": "read_only",
        "readable_roots": [str(path) for path in resolved_readable_roots],
        "writable_roots": [str(path) for path in resolved_roots],
        "network_access": "allowed" if policy.network_access else "denied_namespace",
        "wall_timeout_seconds": wall,
        "memory_limit_mb": policy.memory_limit_mb,
        "output_limit_bytes": policy.output_limit_bytes,
        "process_limit": policy.process_limit,
        "file_descriptor_limit": policy.file_descriptor_limit,
    }
    write_text_atomic(
        output_path,
        "ISOLATION " + repr(isolation) + "\n\nOUTPUT\n" + bounded,
    )
    return IsolatedProcessResult(
        returncode=returncode,
        status=status,
        failure_reason=failure_reason,
        output=bounded,
        isolation=isolation,
    )


def _sandbox_tools() -> dict[str, str]:
    if os.getenv("RESEARCH_RUNTIME_PROFILE") == "operated":
        resolved: dict[str, str] = {}
        missing_names: list[str] = []
        for name, candidate in _OPERATED_SANDBOX_TOOLS.items():
            try:
                status = candidate.lstat()
                actual = candidate.resolve(strict=True)
            except OSError:
                missing_names.append(name)
                continue
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or not os.access(candidate, os.X_OK)
                or actual != candidate
                or status.st_nlink < 1
            ):
                raise IsolatedProcessError(
                    f"isolated_process_operated_runtime_invalid:{name}"
                )
            resolved[name] = str(candidate)
        if missing_names:
            raise IsolatedProcessError(
                "isolated_process_runtime_missing:"
                + ",".join(sorted(missing_names))
            )
        return resolved
    tools = {name: shutil.which(name) for name in _OPERATED_SANDBOX_TOOLS}
    if any(value is None for value in tools.values()):
        missing_text = ",".join(
            name for name, value in tools.items() if value is None
        )
        raise IsolatedProcessError(
            f"isolated_process_runtime_missing:{missing_text}"
        )
    return {name: str(value) for name, value in tools.items() if value is not None}


def _resolved_writable_roots(values: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    for value in values:
        candidate = value.expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = Path(os.path.abspath(candidate))
        if candidate == Path("/"):
            raise IsolatedProcessError("isolated_process_writable_root_too_broad")
        _reject_writable_root_symlinks(candidate)
        try:
            status = candidate.lstat()
        except FileNotFoundError:
            candidate.mkdir(parents=True, exist_ok=True)
            _reject_writable_root_symlinks(candidate)
            status = candidate.lstat()
        except OSError as exc:
            raise IsolatedProcessError(
                "isolated_process_writable_root_invalid"
            ) from exc
        if not (stat.S_ISDIR(status.st_mode) or stat.S_ISREG(status.st_mode)):
            raise IsolatedProcessError("isolated_process_writable_root_invalid")
        if stat.S_IMODE(status.st_mode) & 0o002:
            raise IsolatedProcessError(
                "isolated_process_writable_root_world_writable"
            )
        if stat.S_ISREG(status.st_mode) and status.st_nlink != 1:
            raise IsolatedProcessError("isolated_process_writable_root_aliased")
        path = candidate.resolve(strict=True)
        if path not in result:
            result.append(path)
    return tuple(sorted(result, key=str))


def _reject_writable_root_symlinks(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            status = current.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise IsolatedProcessError(
                "isolated_process_writable_root_invalid"
            ) from exc
        if stat.S_ISLNK(status.st_mode):
            raise IsolatedProcessError("isolated_process_writable_root_symlink")


def _resolved_readable_roots(values: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    for value in values:
        path = value.expanduser().resolve(strict=True)
        if path == Path("/"):
            raise IsolatedProcessError("isolated_process_readable_root_too_broad")
        if path not in result:
            result.append(path)
    return tuple(sorted(result, key=str))


def _system_runtime_roots() -> tuple[Path, ...]:
    candidates = (
        Path("/usr"),
        Path("/bin"),
        Path("/lib"),
        Path("/lib64"),
        Path("/etc/ld.so.cache"),
        Path("/etc/localtime"),
    )
    return tuple(path for path in candidates if path.exists())


def _mount_parent_directories(values: Sequence[Path]) -> tuple[Path, ...]:
    parents: set[Path] = set()
    for value in values:
        current = value if value.is_dir() else value.parent
        while current != Path("/"):
            parents.add(current)
            current = current.parent
    return tuple(sorted(parents, key=lambda path: (len(path.parts), str(path))))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5.0)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _classify_result(
    returncode: int,
    *,
    outer_timeout: bool,
    cancelled: bool,
    truncated: bool,
    output: str,
) -> tuple[str, str | None]:
    if cancelled:
        return "cancelled", "cancellation_requested"
    if outer_timeout or returncode == 124:
        return "timed_out", "wall_timeout_exceeded"
    if returncode != 0 and (
        "bwrap:" in output
        or "Creating new namespace failed" in output
        or "No permissions to creating new namespace" in output
    ):
        return "sandbox_unavailable", "sandbox_initialization_failed"
    if (
        truncated
        or returncode in {128 + signal.SIGXFSZ, -signal.SIGXFSZ}
        or "File too large" in output
        or "Errno 27" in output
    ):
        return "quarantined", "output_limit_exceeded"
    if returncode in {
        128 + signal.SIGKILL,
        -signal.SIGKILL,
        128 + signal.SIGXCPU,
        -signal.SIGXCPU,
    }:
        return "resource_exhausted", "process_resource_limit_exceeded"
    if returncode != 0 and "MemoryError" in output:
        return "resource_exhausted", "memory_limit_exceeded"
    if returncode != 0:
        return "failed", "subprocess_exit_nonzero"
    return "succeeded", None


__all__ = [
    "IsolatedProcessError",
    "IsolatedProcessPolicy",
    "IsolatedProcessResult",
    "run_isolated_command",
]
