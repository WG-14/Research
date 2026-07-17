from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from .hashing import sha256_prefixed


CODE_PROVENANCE_SCHEMA_VERSION = 1
_ROOT_FILES = ("pyproject.toml", "uv.lock")


def collect_code_provenance(project_root: str | Path) -> dict[str, Any]:
    """Fingerprint the executable source tree, Git state, and dependency lock."""
    root = Path(project_root).resolve()
    source_files = sorted(
        [path for path in (root / "src").rglob("*.py") if path.is_file()],
        key=lambda path: path.relative_to(root).as_posix(),
    )
    source_rows = [_file_evidence(root, path) for path in source_files]
    lock_rows = [
        _file_evidence(root, root / name)
        for name in _ROOT_FILES
        if (root / name).is_file()
    ]
    commit = _git_text(root, "rev-parse", "HEAD")
    status = _git_text(root, "status", "--porcelain=v1", "--untracked-files=all")
    diff = _git_bytes(root, "diff", "--binary", "HEAD", "--")
    payload: dict[str, Any] = {
        "schema_version": CODE_PROVENANCE_SCHEMA_VERSION,
        "git_commit": commit or "unknown",
        "git_available": commit is not None and status is not None and diff is not None,
        "git_dirty": bool(status) if status is not None else None,
        "git_status_hash": _bytes_hash(status.encode("utf-8"))
        if status is not None
        else None,
        "git_diff_hash": _bytes_hash(diff) if diff is not None else None,
        "source_tree_hash": sha256_prefixed(
            source_rows, label="repository_source_tree"
        ),
        "source_file_count": len(source_rows),
        "dependency_contract_hash": sha256_prefixed(
            lock_rows, label="repository_dependency_contract"
        ),
        "dependency_contract_files": [row["path"] for row in lock_rows],
    }
    payload["code_provenance_hash"] = sha256_prefixed(payload, label="code_provenance")
    return payload


def _file_evidence(root: Path, path: Path) -> dict[str, str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "content_hash": _bytes_hash(path.read_bytes()),
    }


def _git_text(root: Path, *args: str) -> str | None:
    raw = _git_bytes(root, *args)
    return raw.decode("utf-8", errors="strict").strip() if raw is not None else None


def _git_bytes(root: Path, *args: str) -> bytes | None:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=root,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None


def _bytes_hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
