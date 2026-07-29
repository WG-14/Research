"""Deterministic, bounded source archive for cold multi-asset replay."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path


PORTABLE_ENGINE_SOURCE_SCHEMA_VERSION = 1
_MAX_SOURCE_FILES = 2_000
_MAX_SOURCE_BYTES = 32 * 1024 * 1024
_ALLOWED_SUFFIXES = frozenset({".py", ".json", ".toml"})


class PortableSourceError(ValueError):
    """The engine source snapshot is unsafe, incomplete, or too large."""


def _sha256(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _canonical_file(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o444) << 16
    return info


def build_portable_engine_source_archive(project_root: Path) -> bytes:
    """Archive the pure-Python research engine with stable bytes and hashes."""

    root = project_root.expanduser().resolve(strict=True)
    source_root = (root / "src" / "market_research").resolve(strict=True)
    try:
        source_root.relative_to(root)
    except ValueError as exc:  # pragma: no cover - defensive boundary.
        raise PortableSourceError("portable_source.outside_project") from exc
    candidates = tuple(
        sorted(
            path
            for path in source_root.rglob("*")
            if path.is_file() and path.suffix in _ALLOWED_SUFFIXES
        )
    )
    if not candidates or len(candidates) > _MAX_SOURCE_FILES:
        raise PortableSourceError("portable_source.file_count_invalid")
    records: list[dict[str, object]] = []
    payloads: list[tuple[str, bytes]] = []
    total_bytes = 0
    for path in candidates:
        if path.is_symlink():
            raise PortableSourceError("portable_source.symlink_forbidden")
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(root / "src").as_posix()
        except ValueError as exc:
            raise PortableSourceError("portable_source.path_escape") from exc
        raw = resolved.read_bytes()
        total_bytes += len(raw)
        if total_bytes > _MAX_SOURCE_BYTES:
            raise PortableSourceError("portable_source.byte_limit_exceeded")
        records.append(
            {
                "relative_path": relative,
                "content_hash": _sha256(raw),
                "byte_length": len(raw),
            }
        )
        payloads.append((relative, raw))
    manifest_identity = {
        "schema_version": PORTABLE_ENGINE_SOURCE_SCHEMA_VERSION,
        "archive_type": "MARKET_RESEARCH_ENGINE_SOURCE",
        "files": records,
    }
    manifest = {
        **manifest_identity,
        "content_hash": _sha256(_canonical_file(manifest_identity)),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        archive.writestr(_zip_info("SOURCE_MANIFEST.json"), _canonical_file(manifest))
        for relative, raw in payloads:
            archive.writestr(_zip_info(relative), raw)
    result = buffer.getvalue()
    if not result or len(result) > _MAX_SOURCE_BYTES:
        raise PortableSourceError("portable_source.archive_size_invalid")
    return result


__all__ = [
    "PORTABLE_ENGINE_SOURCE_SCHEMA_VERSION",
    "PortableSourceError",
    "build_portable_engine_source_archive",
]
