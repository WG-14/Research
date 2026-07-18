"""Immutable, content-addressed source archives for calculation recovery."""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from market_research.paths import ResearchPathManager

from .hashing import sha256_prefixed


SOURCE_ARCHIVE_SCHEMA_VERSION = 1
_ARCHIVE_ROOT_FILES = ("pyproject.toml", "uv.lock")
_FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


class SourceArchiveError(RuntimeError):
    """A source archive cannot be published or safely restored."""


def publish_source_archive(
    *,
    manager: ResearchPathManager,
    strategy_name: str,
    strategy_registry: Any,
) -> dict[str, object]:
    """Publish exact executable checkout bytes outside the repository.

    The archive includes tracked and untracked files under the executable Core
    package, plus the dependency lock contract. ZIP metadata is normalized so
    identical bytes produce an identical content address.
    """

    project_root = manager.project_root.resolve()
    rows = _source_rows(project_root)
    if not rows:
        raise SourceArchiveError("source_archive_has_no_executable_files")

    archive_dir = manager.artifact_path("_source_archives")
    archive_dir.mkdir(parents=True, exist_ok=True)
    fd, staging_name = tempfile.mkstemp(
        prefix=".source-archive-", suffix=".zip", dir=str(archive_dir)
    )
    os.close(fd)
    staging = Path(staging_name)
    try:
        with zipfile.ZipFile(
            staging, "w", compression=zipfile.ZIP_STORED
        ) as archive:
            for logical_path, source_path in rows:
                info = zipfile.ZipInfo(logical_path, date_time=_FIXED_ZIP_DATE)
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = 0o100644 << 16
                info.create_system = 3
                archive.writestr(info, source_path.read_bytes())
        with staging.open("rb") as handle:
            os.fsync(handle.fileno())
        archive_hash = _file_hash(staging)
        digest_hex = archive_hash.removeprefix("sha256:")
        target = archive_dir / f"{digest_hex}.zip"
        try:
            os.link(staging, target)
            _fsync_directory(archive_dir)
        except FileExistsError:
            if _file_hash(target) != archive_hash:
                raise SourceArchiveError("source_archive_content_address_conflict")

        plugin = strategy_registry.resolve(strategy_name)
        plugin_contract_hash = str(plugin.contract_hash())
        sidecar_digest = getattr(plugin, "package_manifest_hash", None)
        package_digest = sha256_prefixed(
            {
                "strategy_name": strategy_name,
                "plugin_contract_hash": plugin_contract_hash,
                "sidecar_manifest_digest": sidecar_digest,
                "source_archive_digest": archive_hash,
            },
            label="strategy_package_archive_binding",
        )
        return {
            "schema_version": SOURCE_ARCHIVE_SCHEMA_VERSION,
            "format": "deterministic_zip_v1",
            "digest": archive_hash,
            "path": str(target.resolve()),
            "size_bytes": target.stat().st_size,
            "file_count": len(rows),
            "strategy_name": strategy_name,
            "strategy_plugin_contract_hash": plugin_contract_hash,
            "sidecar_manifest_digest": sidecar_digest,
            "strategy_package_digest": package_digest,
        }
    finally:
        staging.unlink(missing_ok=True)


def restore_source_archive(
    *, archive_path: str | Path, expected_digest: str, destination: str | Path
) -> Path:
    """Verify and safely extract a published source archive."""

    source = Path(archive_path).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not source.is_file() or _file_hash(source) != expected_digest:
        raise SourceArchiveError("source_archive_digest_mismatch")
    if target.exists() and any(target.iterdir()):
        raise SourceArchiveError("source_archive_restore_destination_not_empty")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "r") as archive:
        for member in archive.infolist():
            logical = PurePosixPath(member.filename)
            if logical.is_absolute() or ".." in logical.parts or member.is_dir():
                raise SourceArchiveError("source_archive_unsafe_member")
            output = (target / Path(*logical.parts)).resolve()
            try:
                output.relative_to(target)
            except ValueError as exc:
                raise SourceArchiveError("source_archive_unsafe_member") from exc
            output.parent.mkdir(parents=True, exist_ok=True)
            data = archive.read(member)
            fd, staging_name = tempfile.mkstemp(
                prefix=f".{output.name}.", suffix=".tmp", dir=str(output.parent)
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(staging_name, output)
            finally:
                if os.path.exists(staging_name):
                    os.unlink(staging_name)
    return target


def _source_rows(project_root: Path) -> list[tuple[str, Path]]:
    candidates: list[Path] = []
    package_root = project_root / "src" / "market_research"
    if package_root.is_dir():
        candidates.extend(
            path
            for path in package_root.rglob("*")
            if path.is_file() and path.suffix in {".py", ".json"}
        )
    else:
        installed_root = Path(__file__).resolve().parents[1]
        candidates.extend(
            path
            for path in installed_root.rglob("*")
            if path.is_file() and path.suffix in {".py", ".json"}
        )
        project_root = installed_root.parent
    candidates.extend(
        project_root / name
        for name in _ARCHIVE_ROOT_FILES
        if (project_root / name).is_file()
    )
    unique = sorted(set(candidates), key=lambda path: path.relative_to(project_root).as_posix())
    return [(path.relative_to(project_root).as_posix(), path) for path in unique]


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
