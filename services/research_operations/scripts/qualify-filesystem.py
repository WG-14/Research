#!/usr/bin/env python3
"""Create a path-redacted single-host POSIX qualification receipt."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

SUPPORTED_LOCAL_FILESYSTEMS = frozenset({"ext4", "xfs", "btrfs"})


def _fingerprint(role: str, path: Path) -> str:
    material = f"research-operations-root-v1\0{role}\0{path.resolve()}".encode()
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _mount_identity(path: Path) -> tuple[int, str, str]:
    resolved = path.resolve()
    matches: list[tuple[int, int, str, str]] = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        before, separator, after = line.partition(" - ")
        fields = before.split()
        after_fields = after.split()
        if not separator or len(fields) < 6 or not after_fields:
            continue
        mount_point = Path(
            fields[4]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\134", "\\")
        )
        try:
            resolved.relative_to(mount_point)
        except ValueError:
            continue
        matches.append(
            (len(mount_point.parts), int(fields[0]), fields[2], after_fields[0])
        )
    if not matches:
        raise RuntimeError("qualification_mount_unavailable")
    _depth, mount_id, device_id, filesystem_type = max(matches)
    if filesystem_type not in SUPPORTED_LOCAL_FILESYSTEMS:
        raise RuntimeError("qualification_filesystem_unsupported")
    return mount_id, device_id, filesystem_type


def _boot_id_hash() -> str:
    boot_id = (
        Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    )
    return "sha256:" + hashlib.sha256(boot_id.encode("ascii")).hexdigest()


def _filesystem_identity(role: str, path: Path) -> str:
    target = path if path.is_dir() else path.parent
    stat = target.stat()
    filesystem = os.statvfs(target)
    material = (
        f"research-operations-filesystem-v1\0{role}\0{stat.st_dev}\0"
        f"{getattr(filesystem, 'f_fsid', 0)}\0{filesystem.f_bsize}"
    ).encode()
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _qualify(role: str, configured: Path) -> dict[str, object]:
    target = configured if configured.is_dir() else configured.parent
    if not configured.is_absolute() or not target.is_dir() or target.is_symlink():
        raise RuntimeError("qualification_root_invalid")
    work = target / ".research-ops-qualification" / uuid.uuid4().hex
    work.mkdir(parents=True, exist_ok=False, mode=0o700)

    durable = work / "durable"
    with durable.open("xb") as handle:
        handle.write(b"durable-v1\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

    replacement = work / "replacement"
    with replacement.open("xb") as handle:
        handle.write(b"replacement-v1\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(replacement, durable)
    if durable.read_bytes() != b"replacement-v1\n":
        raise RuntimeError("atomic_replace_failed")

    lock_path = work / "lock"
    with lock_path.open("xb+") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        contender = os.fork()
        if contender == 0:
            try:
                with lock_path.open("rb") as child:
                    fcntl.flock(child, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os._exit(0)
            os._exit(1)
        _, status = os.waitpid(contender, 0)
        if status != 0:
            raise RuntimeError("process_lock_failed")

    append_path = work / "append.jsonl"
    append_lock = work / "append.lock"
    children: list[int] = []
    for worker in range(4):
        child = os.fork()
        if child == 0:
            for sequence in range(25):
                row = (
                    json.dumps(
                        {"worker": worker, "sequence": sequence},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                    + b"\n"
                )
                with append_lock.open("a+b") as lock_handle:
                    fcntl.flock(lock_handle, fcntl.LOCK_EX)
                    with append_path.open("ab") as stream:
                        stream.write(row)
                        stream.flush()
                        os.fsync(stream.fileno())
            os._exit(0)
        children.append(child)
    if any(os.waitpid(child, 0)[1] != 0 for child in children):
        raise RuntimeError("concurrent_append_failed")
    lines = append_path.read_bytes().splitlines()
    if len(lines) != 100 or len(set(lines)) != 100:
        raise RuntimeError("concurrent_append_failed")

    mount_id, device_id, filesystem_type = _mount_identity(target)
    return {
        "role": role,
        "root_fingerprint": _fingerprint(role, configured),
        "filesystem_identity": _filesystem_identity(role, configured),
        "status": "PASS",
        "atomic_replace": "PASS",
        "durable_fsync": "PASS",
        "process_lock": "PASS",
        "concurrent_append": "PASS",
        "mount_id": mount_id,
        "device_id": device_id,
        "filesystem_type": filesystem_type,
        "evidence_id": work.name,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", required=True, metavar="ROLE=/ABS")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    roots = []
    for value in args.root:
        role, separator, raw_path = value.partition("=")
        if not separator or not role or not raw_path:
            raise SystemExit("root_argument_invalid")
        roots.append(_qualify(role, Path(raw_path)))
    output = args.output.expanduser()
    if not output.is_absolute() or output.exists() or not output.parent.is_dir():
        raise SystemExit("output_path_invalid")
    receipt = {
        "schema_version": 1,
        "status": "PASS",
        "scope": "single-host",
        "cross_host_status": "NOT_RUN",
        "qualified_at": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "boot_id_hash": _boot_id_hash(),
        "roots": sorted(roots, key=lambda item: str(item["role"])),
    }
    payload = (
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
