"""Deterministic source surface bound to the canonical research audit."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


AUDIT_SURFACE_SCHEMA_VERSION = 2
_EXCLUDED_PARTS = {
    ".git",
    ".hypothesis",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
_EXCLUDED_FILES = {
    "docs/investment-research-platform-audit.json",
    "docs/investment-research-platform-audit-report.md",
    "docs/investment-research-platform-audit-result.json",
}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
_EXCLUDED_DIRECTORY_SUFFIXES = {".egg-info"}


def audit_surface(root: Path) -> dict[str, object]:
    """Hash every owned source, test, policy, example, and documentation file.

    Generated audit outputs are excluded to avoid a recursive hash. Runtime
    caches, build products, and virtual environments are outside the selected
    roots or explicitly rejected.
    """

    resolved = root.resolve()
    paths: set[Path] = set()
    for directory, directory_names, file_names in os.walk(
        resolved, topdown=True, followlinks=False
    ):
        current = Path(directory)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = current / name
            local = candidate.relative_to(resolved)
            if name in _EXCLUDED_PARTS or any(
                name.endswith(suffix) for suffix in _EXCLUDED_DIRECTORY_SUFFIXES
            ):
                continue
            if candidate.is_symlink():
                raise ValueError(f"audit_surface_symlink_forbidden:{local.as_posix()}")
            retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in sorted(file_names):
            candidate = current / name
            local = candidate.relative_to(resolved)
            if local.as_posix() in _EXCLUDED_FILES:
                continue
            if candidate.is_symlink():
                raise ValueError(f"audit_surface_symlink_forbidden:{local.as_posix()}")
            if candidate.suffix in _EXCLUDED_SUFFIXES:
                continue
            if not candidate.is_file():
                raise ValueError(
                    f"audit_surface_non_regular_file_forbidden:{local.as_posix()}"
                )
            paths.add(candidate)
    digest = hashlib.sha256()
    for candidate in sorted(
        paths, key=lambda item: item.relative_to(resolved).as_posix()
    ):
        relative = candidate.relative_to(resolved).as_posix()
        content_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
        file_mode = candidate.stat(follow_symlinks=False).st_mode & 0o7777
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{file_mode:04o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(content_hash.encode("ascii"))
        digest.update(b"\0")
    return {
        "schema_version": AUDIT_SURFACE_SCHEMA_VERSION,
        "file_count": len(paths),
        "sha256": digest.hexdigest(),
        "exclusions": sorted(
            [f"directory:{name}" for name in _EXCLUDED_PARTS]
            + [f"directory_suffix:*{suffix}" for suffix in _EXCLUDED_DIRECTORY_SUFFIXES]
            + [f"file:{name}" for name in _EXCLUDED_FILES]
            + [f"file_suffix:*{suffix}" for suffix in _EXCLUDED_SUFFIXES]
        ),
    }


__all__ = ["AUDIT_SURFACE_SCHEMA_VERSION", "audit_surface"]
