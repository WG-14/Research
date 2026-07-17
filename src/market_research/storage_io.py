from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


_MAX_ATOMIC_JSON_BYTES = 16 * 1024 * 1024


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    _ensure_parent(path)
    created = not path.exists()
    line = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        _fsync_parent_directory(path)


def write_text_atomic(path: Path, text: str) -> None:
    _ensure_parent(path)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_parent_directory(path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    )
    write_text_atomic(path, serialized + "\n")


def write_json_atomic_create_or_verify(
    path: Path,
    payload: dict[str, Any],
) -> bool:
    """Create an immutable JSON target or verify an identical prior publish.

    Returns ``True`` for a new publication and ``False`` for an identical
    existing target. Existing different or malformed content is never replaced.
    """

    _ensure_parent(path)
    serialized = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    serialized_bytes = serialized.encode("utf-8")
    if len(serialized_bytes) > _MAX_ATOMIC_JSON_BYTES:
        raise ValueError("atomic_json_target_too_large")
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            _verify_json_target(path, serialized_bytes)
            return False
        _fsync_parent_directory(path)
        return True
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _verify_json_target(path: Path, expected: bytes) -> None:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("atomic_json_no_follow_unavailable")
    try:
        fd = os.open(path, os.O_RDONLY | no_follow)
    except OSError as exc:
        raise ValueError("atomic_json_target_conflict") from exc
    try:
        size = os.fstat(fd).st_size
        if size != len(expected) or size > _MAX_ATOMIC_JSON_BYTES:
            raise ValueError("atomic_json_target_conflict")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise ValueError("atomic_json_target_conflict")
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    if b"".join(chunks) != expected:
        raise ValueError("atomic_json_target_conflict")


def write_jsonl_atomic(path: Path, records: Iterable[dict[str, Any]]) -> None:
    """Publish one complete JSONL snapshot with old-or-new crash semantics."""

    serialized = "".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for record in records
    )
    write_text_atomic(path, serialized)


def _fsync_parent_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
