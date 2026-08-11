from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable


_MAX_ATOMIC_JSON_BYTES = 16 * 1024 * 1024
_DEFAULT_ATOMIC_PUBLICATION_MODE = 0o600
_SHARED_ATOMIC_PUBLICATION_MODE = 0o640
ATOMIC_PUBLICATION_MODE_ENV = "MARKET_RESEARCH_ATOMIC_PUBLICATION_MODE"


def _ensure_parent(path: Path) -> None:
    ensure_directory(path.parent)


def ensure_directory(
    path: Path,
    *,
    require_shared_mode: bool = False,
    trusted_root: Path | None = None,
) -> None:
    """Create a directory without weakening the private library default.

    In the qualified shared-publication profile, newly created directories are
    made exact setgid ``2770`` only after their inherited group is verified.
    Existing boundary directories are never silently re-permissioned. Callers
    that expose a cross-principal workspace may require the leaf itself to
    already satisfy that exact contract.
    """

    expected_group_id: int | None = None
    boundary: Path | None = None
    if trusted_root is not None:
        boundary = trusted_root.absolute()
        target = path.absolute()
        try:
            target.relative_to(boundary)
        except ValueError as exc:
            raise ValueError("atomic_publication_directory_outside_root") from exc
        try:
            boundary_status = boundary.lstat()
        except OSError as exc:
            raise ValueError("atomic_publication_directory_invalid") from exc
        if stat.S_ISLNK(boundary_status.st_mode) or not stat.S_ISDIR(
            boundary_status.st_mode
        ):
            raise ValueError("atomic_publication_directory_invalid")
        expected_group_id = boundary_status.st_gid

    publication_mode = _atomic_publication_mode(None)
    if publication_mode == _DEFAULT_ATOMIC_PUBLICATION_MODE and trusted_root is None:
        path.mkdir(parents=True, exist_ok=True)
        return
    shared_profile = publication_mode == _SHARED_ATOMIC_PUBLICATION_MODE

    missing: list[Path] = []
    current = path
    while True:
        try:
            status = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            parent = current.parent
            if parent == current:
                raise ValueError("atomic_publication_directory_invalid")
            current = parent
            continue
        except OSError as exc:
            raise ValueError("atomic_publication_directory_invalid") from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise ValueError("atomic_publication_directory_invalid")
        break

    # Do not accept a safe-looking leaf below a symlinked ancestor.  When the
    # caller supplies a configured root, also bind every component to that
    # root's group so a writer cannot redirect a shared namespace into a
    # different service group.
    ancestor = current
    while True:
        try:
            ancestor_status = ancestor.lstat()
        except OSError as exc:
            raise ValueError("atomic_publication_directory_invalid") from exc
        if stat.S_ISLNK(ancestor_status.st_mode) or not stat.S_ISDIR(
            ancestor_status.st_mode
        ):
            raise ValueError("atomic_publication_directory_invalid")
        if (
            expected_group_id is not None
            and ancestor_status.st_gid != expected_group_id
        ):
            raise ValueError("atomic_publication_directory_group_invalid")
        if boundary is not None and ancestor == boundary:
            break
        parent = ancestor.parent
        if parent == ancestor:
            if boundary is not None:
                raise ValueError("atomic_publication_directory_outside_root")
            break
        ancestor = parent

    for candidate in reversed(missing):
        created = False
        try:
            os.mkdir(candidate, 0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise ValueError("atomic_publication_directory_invalid") from exc
        try:
            status = candidate.lstat()
            parent_status = candidate.parent.stat()
        except OSError as exc:
            raise ValueError("atomic_publication_directory_invalid") from exc
        if (
            stat.S_ISLNK(status.st_mode)
            or not stat.S_ISDIR(status.st_mode)
            or status.st_gid != parent_status.st_gid
            or (expected_group_id is not None and status.st_gid != expected_group_id)
        ):
            raise ValueError("atomic_publication_directory_invalid")
        if created:
            os.chmod(
                candidate,
                0o2770 if shared_profile else 0o700,
                follow_symlinks=False,
            )
            _fsync_directory(candidate)
            _fsync_parent_directory(candidate)
        elif shared_profile and stat.S_IMODE(status.st_mode) != 0o2770:
            raise ValueError("atomic_publication_directory_invalid")

    if require_shared_mode and shared_profile:
        try:
            leaf = path.lstat()
        except OSError as exc:
            raise ValueError("atomic_publication_directory_invalid") from exc
        if (
            stat.S_ISLNK(leaf.st_mode)
            or not stat.S_ISDIR(leaf.st_mode)
            or stat.S_IMODE(leaf.st_mode) != 0o2770
            or (expected_group_id is not None and leaf.st_gid != expected_group_id)
        ):
            raise ValueError("atomic_publication_directory_invalid")


def finalize_file_publication(
    descriptor: int,
    *,
    mode: int | None = None,
) -> None:
    """Make a fully written regular file safe for final-name publication."""

    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode):
        raise ValueError("atomic_publication_file_invalid")
    _set_completed_publication_mode(descriptor, _atomic_publication_mode(mode))


def open_lock_file(path: Path) -> int:
    """Open a durable process-lock inode with a native cross-UID contract.

    Lock files are private ``0600`` by default and exact ``0660`` in the
    qualified shared profile.  A new inode is fully permissioned before its
    final name becomes visible, avoiding a transient owner-only lock that a
    second service UID could not open.
    """

    _ensure_parent(path)
    publication_mode = _atomic_publication_mode(None)
    lock_mode = 0o600 if publication_mode == 0o600 else 0o660
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    published = False
    try:
        os.fchmod(descriptor, lock_mode)
        os.fsync(descriptor)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            os.close(descriptor)
            descriptor = -1
        else:
            _fsync_parent_directory(path)
            published = True
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        if descriptor >= 0 and not published:
            # Ownership transfers to the caller only on the successful return
            # above.  All exceptional paths close our temporary descriptor.
            os.close(descriptor)

    if published:
        return descriptor

    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        existing = os.open(path, flags)
    except OSError as exc:
        raise ValueError("process_lock_access_invalid") from exc
    try:
        status = os.fstat(existing)
        parent_status = path.parent.stat()
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_IMODE(status.st_mode) != lock_mode
            or (
                publication_mode == _SHARED_ATOMIC_PUBLICATION_MODE
                and status.st_gid != parent_status.st_gid
            )
        ):
            raise ValueError("process_lock_access_invalid")
    except BaseException:
        os.close(existing)
        raise
    return existing


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    _ensure_parent(path)
    publication_mode = _atomic_publication_mode(None)
    line = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        descriptor = os.open(path, flags)
        created = False
    with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode) or (
            not created and stat.S_IMODE(status.st_mode) != publication_mode
        ):
            raise ValueError("append_jsonl_access_mode_invalid")
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        if created:
            _set_completed_publication_mode(handle.fileno(), publication_mode)
    if created:
        _fsync_parent_directory(path)


def write_text_atomic(
    path: Path,
    text: str,
    *,
    mode: int | None = None,
) -> None:
    """Atomically publish text with an explicit, fail-closed access mode.

    Private ``0600`` publication is the library default.  A qualified
    multi-principal deployment may opt in to ``0640`` through ``mode`` or
    :data:`ATOMIC_PUBLICATION_MODE_ENV`; no other mode is accepted.  The
    temporary file remains owner-only until its complete contents have been
    flushed, so group readers can never observe a partially written file.
    """

    _ensure_parent(path)
    publication_mode = _atomic_publication_mode(mode)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            _set_completed_publication_mode(handle.fileno(), publication_mode)
        os.replace(temp_path, path)
        _fsync_parent_directory(path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def write_json_atomic(
    path: Path,
    payload: dict[str, Any],
    *,
    mode: int | None = None,
) -> None:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
    )
    write_text_atomic(path, serialized + "\n", mode=mode)


def write_json_atomic_create_or_verify(
    path: Path,
    payload: dict[str, Any],
    *,
    mode: int | None = None,
) -> bool:
    """Create an immutable JSON target or verify an identical prior publish.

    Returns ``True`` for a new publication and ``False`` for an identical
    existing target. Existing different or malformed content is never replaced.
    """

    _ensure_parent(path)
    publication_mode = _atomic_publication_mode(mode)
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
            _set_completed_publication_mode(handle.fileno(), publication_mode)
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


def write_jsonl_atomic(
    path: Path,
    records: Iterable[dict[str, Any]],
    *,
    mode: int | None = None,
) -> None:
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
    write_text_atomic(path, serialized, mode=mode)


def _atomic_publication_mode(mode: int | None) -> int:
    if mode is not None:
        selected = mode
    else:
        configured = os.environ.get(ATOMIC_PUBLICATION_MODE_ENV)
        if configured is None or configured == "0600":
            selected = _DEFAULT_ATOMIC_PUBLICATION_MODE
        elif configured == "0640":
            selected = _SHARED_ATOMIC_PUBLICATION_MODE
        else:
            raise ValueError("atomic_publication_mode_invalid")
    if selected not in {
        _DEFAULT_ATOMIC_PUBLICATION_MODE,
        _SHARED_ATOMIC_PUBLICATION_MODE,
    }:
        raise ValueError("atomic_publication_mode_invalid")
    return selected


def _set_completed_publication_mode(descriptor: int, mode: int) -> None:
    os.fchmod(descriptor, mode)
    # Persist the mode transition as well as the completed payload before the
    # directory entry becomes visible at its final name.
    os.fsync(descriptor)


def _fsync_parent_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
