#!/usr/bin/env python3
"""Create the canonical Internal Research Platform release manifest."""

from __future__ import annotations

import argparse
import base64
import csv
import configparser
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA_VERSION = 1
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MIGRATION = re.compile(r"^[0-9]{4}_[A-Za-z0-9_]+\.(?:py|sql)$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
_PROVENANCE_FILENAME = "_build_provenance.json"
_PROVENANCE_FIELDS = {
    "schema_version",
    "distribution",
    "version",
    "git_sha",
    "source_digest",
    "platform_source_digest",
}

_COMPONENTS = {
    "core": Path("pyproject.toml"),
    "web": Path("apps/internal_web/pyproject.toml"),
    "operations": Path("services/research_operations/pyproject.toml"),
}
_MIGRATION_ROOTS = {
    "web": Path("apps/internal_web/src/portal/migrations"),
    "operations": Path(
        "services/research_operations/src/research_operations/migrations"
    ),
}
_COMPONENT_SOURCES = {
    "core": {
        "source_root": Path("src"),
        "packages": ("market_research",),
        "provenance_package": "market_research",
    },
    "web": {
        "source_root": Path("apps/internal_web/src"),
        "packages": ("market_research_web", "portal"),
        "provenance_package": "market_research_web",
    },
    "operations": {
        "source_root": Path("services/research_operations/src"),
        "packages": ("research_operations",),
        "provenance_package": "research_operations",
    },
}
_COMPONENT_PROJECT_ROOTS = {
    "core": Path("."),
    "web": Path("apps/internal_web"),
    "operations": Path("services/research_operations"),
}
_OPERATIONS_PROJECT_ROOT = Path("services/research_operations")
_DEPLOYMENT_MARKER = Path("deploy/OFFICIAL_DEPLOYMENT")
_DEPLOYMENT_TREES = (Path("deploy/native"), Path("scripts"))
_GENERATED_EGG_INFO_FILES = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "requires.txt",
    "top_level.txt",
}


class ReleaseManifestError(ValueError):
    """Raised when a reproducible platform release cannot be described."""


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _file_record(path: Path) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise ReleaseManifestError(f"release_input_not_regular_file:{path}")
    payload = path.read_bytes()
    return {"sha256": _sha256(payload), "size_bytes": len(payload)}


def _records_digest(records: object) -> str:
    return _sha256(_canonical(records))


def _normalized_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _source_payloads(root: Path, component: str) -> dict[str, bytes]:
    configuration = _COMPONENT_SOURCES[component]
    source_root = root / configuration["source_root"]
    payloads: dict[str, bytes] = {}
    for package in configuration["packages"]:
        package_root = source_root / package
        if not package_root.is_dir() or package_root.is_symlink():
            raise ReleaseManifestError(
                f"component_source_missing:{component}:{package}"
            )
        for path in sorted(package_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if (
                "__pycache__" in path.parts
                or path.suffix in {".pyc", ".pyo"}
                or path.name == _PROVENANCE_FILENAME
            ):
                continue
            logical_path = path.relative_to(source_root).as_posix()
            payloads[logical_path] = path.read_bytes()
    if not payloads:
        raise ReleaseManifestError(f"component_source_empty:{component}")
    return payloads


def _payload_records(payloads: Mapping[str, bytes]) -> list[dict[str, object]]:
    return [
        {"path": path, "sha256": _sha256(payload), "size_bytes": len(payload)}
        for path, payload in sorted(payloads.items())
    ]


def component_source_digests(root: Path) -> dict[str, str]:
    """Return canonical digests of the package payload for each distribution."""

    return {
        component: _records_digest(_payload_records(_source_payloads(root, component)))
        for component in _COMPONENTS
    }


def _platform_source_digest(root: Path, source_digests: Mapping[str, str]) -> str:
    inputs = {
        "components": dict(sorted(source_digests.items())),
        "projects": {
            label: _file_record(root / path)["sha256"]
            for label, path in sorted(_COMPONENTS.items())
        },
        "lock_digest": _file_record(root / "uv.lock")["sha256"],
        "migrations": _migration_metadata(root),
        "deployment_digest": _deployment_digest(root),
    }
    return _records_digest(inputs)


def expected_build_provenance(
    root: Path,
    git_sha: str,
) -> dict[str, dict[str, object]]:
    """Create the exact provenance payload embedded in all release artifacts."""

    if not _GIT_SHA.fullmatch(git_sha):
        raise ReleaseManifestError("release_git_sha_invalid")
    components = _component_metadata(root)
    source_digests = component_source_digests(root)
    platform_digest = _platform_source_digest(root, source_digests)
    return {
        label: {
            "schema_version": 1,
            "distribution": metadata["distribution"],
            "version": metadata["version"],
            "git_sha": git_sha,
            "source_digest": source_digests[label],
            "platform_source_digest": platform_digest,
        }
        for label, metadata in components.items()
    }


def _component_metadata(root: Path) -> dict[str, dict[str, str]]:
    components: dict[str, dict[str, str]] = {}
    for label, relative_path in _COMPONENTS.items():
        payload = tomllib.loads((root / relative_path).read_text(encoding="utf-8"))
        project = payload.get("project")
        if not isinstance(project, dict):
            raise ReleaseManifestError(f"component_project_missing:{label}")
        name = str(project.get("name", "")).strip()
        version = str(project.get("version", "")).strip()
        if not name or not version:
            raise ReleaseManifestError(f"component_identity_missing:{label}")
        components[label] = {"distribution": name, "version": version}
    return components


def _migration_metadata(root: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for label, relative_root in _MIGRATION_ROOTS.items():
        migration_root = root / relative_root
        records = []
        for path in sorted(migration_root.iterdir()):
            if not _MIGRATION.fullmatch(path.name):
                continue
            record = {"name": path.stem, **_file_record(path)}
            records.append(record)
        if not records:
            raise ReleaseManifestError(f"migration_set_empty:{label}")
        result[label] = {
            "count": len(records),
            "latest": records[-1]["name"],
            "digest": _records_digest(records),
        }
    return result


def _deployment_file_record(root: Path, path: Path) -> dict[str, object]:
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode):
            raise OSError
        payload = path.read_bytes()
    except OSError as error:
        raise ReleaseManifestError("release_deployment_invalid") from error
    if len(payload) != status.st_size:
        raise ReleaseManifestError("release_deployment_invalid")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _sha256(payload),
        "size_bytes": len(payload),
        "mode": stat.S_IMODE(status.st_mode),
    }


def _deployment_digest(root: Path) -> str:
    operations_root = root / _OPERATIONS_PROJECT_ROOT
    marker = operations_root / _DEPLOYMENT_MARKER
    records = [_deployment_file_record(root, marker)]

    for relative_tree in _DEPLOYMENT_TREES:
        tree = operations_root / relative_tree
        try:
            tree_status = tree.lstat()
            if not stat.S_ISDIR(tree_status.st_mode):
                raise OSError
            paths = sorted(tree.rglob("*"))
        except OSError as error:
            raise ReleaseManifestError("release_deployment_invalid") from error

        regular_file_count = 0
        for path in paths:
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            try:
                status = path.lstat()
            except OSError as error:
                raise ReleaseManifestError("release_deployment_invalid") from error
            if stat.S_ISLNK(status.st_mode):
                raise ReleaseManifestError("release_deployment_invalid")
            if stat.S_ISDIR(status.st_mode):
                continue
            if not stat.S_ISREG(status.st_mode):
                raise ReleaseManifestError("release_deployment_invalid")
            records.append(_deployment_file_record(root, path))
            regular_file_count += 1
        if regular_file_count == 0:
            raise ReleaseManifestError("release_deployment_invalid")

    records.sort(key=lambda record: str(record["path"]))
    return _records_digest(records)


def discover_artifacts(
    root: Path,
    artifacts_dir: Path,
    components: Mapping[str, Mapping[str, str]],
) -> dict[str, Path]:
    del root
    if not artifacts_dir.is_absolute():
        raise ReleaseManifestError("artifacts_directory_must_be_absolute")
    discovered: dict[str, Path] = {}
    for component, metadata in components.items():
        stem = re.sub(r"[-_.]+", "_", metadata["distribution"])
        version = metadata["version"]
        wheels = sorted(artifacts_dir.glob(f"{stem}-{version}-*.whl"))
        sdists = sorted(artifacts_dir.glob(f"{stem}-{version}.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise ReleaseManifestError(
                f"release_artifact_set_invalid:{component}:"
                f"wheels={len(wheels)}:sdists={len(sdists)}"
            )
        discovered[f"{component}-wheel"] = wheels[0]
        discovered[f"{component}-sdist"] = sdists[0]
    return discovered


def _metadata_identity(payload: bytes, label: str) -> tuple[str, str]:
    try:
        metadata = BytesParser(policy=email_policy).parsebytes(payload)
    except Exception as error:
        raise ReleaseManifestError(f"artifact_metadata_invalid:{label}") from error
    names = metadata.get_all("Name", [])
    versions = metadata.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise ReleaseManifestError(f"artifact_metadata_invalid:{label}")
    return str(names[0]).strip(), str(versions[0]).strip()


def _normalized_requirement(value: str) -> str:
    value = re.sub(r"\s+", "", value).replace("'", '"').lower()
    requirement, separator, marker = value.partition(";")
    match = re.fullmatch(r"([a-z0-9._-]+)(\[[a-z0-9,._-]+\])?(.*)", requirement)
    if match is None:
        return value
    name = _normalized_distribution(match.group(1))
    extras = match.group(2) or ""
    if extras:
        extras = "[" + ",".join(sorted(extras[1:-1].split(","))) + "]"
    specifiers = match.group(3)
    if specifiers:
        specifiers = ",".join(sorted(specifiers.split(",")))
    normalized = name + extras + specifiers
    return normalized + (separator + marker if separator else "")


def _project_configuration(root: Path, component: str) -> dict[str, object]:
    payload = tomllib.loads((root / _COMPONENTS[component]).read_text(encoding="utf-8"))
    project = payload.get("project")
    if not isinstance(project, dict):
        raise ReleaseManifestError(f"component_project_missing:{component}")
    return project


def _validate_project_metadata(
    payload: bytes,
    *,
    root: Path,
    component: str,
    label: str,
) -> None:
    metadata = BytesParser(policy=email_policy).parsebytes(payload)
    project = _project_configuration(root, component)
    expected_requires = [str(item) for item in project.get("dependencies", [])]
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ReleaseManifestError(
            f"component_optional_dependencies_invalid:{component}"
        )
    for extra, requirements in optional.items():
        expected_requires.extend(
            f'{requirement}; extra == "{extra}"' for requirement in requirements
        )
    actual_requires = metadata.get_all("Requires-Dist", [])
    if sorted(map(_normalized_requirement, actual_requires)) != sorted(
        map(_normalized_requirement, expected_requires)
    ):
        raise ReleaseManifestError(f"artifact_dependencies_mismatch:{label}")
    if sorted(metadata.get_all("Provides-Extra", [])) != sorted(optional):
        raise ReleaseManifestError(f"artifact_extras_mismatch:{label}")
    requires_python = str(project.get("requires-python", "")).strip()
    if str(metadata.get("Requires-Python", "")).strip() != requires_python:
        raise ReleaseManifestError(f"artifact_python_requirement_mismatch:{label}")


def _validate_wheel_control_metadata(
    archive: zipfile.ZipFile,
    names: set[str],
    *,
    root: Path,
    component: str,
    dist_info: str,
    label: str,
) -> None:
    wheel_metadata = BytesParser(policy=email_policy).parsebytes(
        archive.read(f"{dist_info}/WHEEL")
    )
    if (
        wheel_metadata.get("Wheel-Version") != "1.0"
        or wheel_metadata.get("Root-Is-Purelib", "").lower() != "true"
        or "py3-none-any" not in wheel_metadata.get_all("Tag", [])
    ):
        raise ReleaseManifestError(f"artifact_wheel_metadata_invalid:{label}")

    project = _project_configuration(root, component)
    scripts = project.get("scripts", {})
    if not isinstance(scripts, dict):
        raise ReleaseManifestError(f"component_scripts_invalid:{component}")
    entry_points_name = f"{dist_info}/entry_points.txt"
    if scripts:
        if entry_points_name not in names:
            raise ReleaseManifestError(f"artifact_entry_points_mismatch:{label}")
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            parser.read_string(archive.read(entry_points_name).decode("utf-8"))
        except (UnicodeDecodeError, configparser.Error) as error:
            raise ReleaseManifestError(
                f"artifact_entry_points_mismatch:{label}"
            ) from error
        if parser.sections() != ["console_scripts"] or {
            key: value.strip() for key, value in parser["console_scripts"].items()
        } != {str(key): str(value) for key, value in scripts.items()}:
            raise ReleaseManifestError(f"artifact_entry_points_mismatch:{label}")
    elif entry_points_name in names:
        raise ReleaseManifestError(f"artifact_entry_points_mismatch:{label}")

    top_level_name = f"{dist_info}/top_level.txt"
    if top_level_name in names:
        actual_packages = {
            line.strip()
            for line in archive.read(top_level_name).decode("utf-8").splitlines()
            if line.strip()
        }
        if actual_packages != set(_COMPONENT_SOURCES[component]["packages"]):
            raise ReleaseManifestError(f"artifact_top_level_mismatch:{label}")


def _validate_archive_limits(
    label: str,
    members: Sequence[tuple[str, int]],
) -> None:
    names = [name for name, _size in members]
    for name in names:
        logical = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or "\x00" in name
            or logical.is_absolute()
            or ".." in logical.parts
        ):
            raise ReleaseManifestError(f"artifact_archive_invalid:{label}")
    if len(names) > _MAX_ARCHIVE_MEMBERS or len(names) != len(set(names)):
        raise ReleaseManifestError(f"artifact_archive_invalid:{label}")
    if any(size < 0 for _name, size in members):
        raise ReleaseManifestError(f"artifact_archive_invalid:{label}")
    if sum(size for _name, size in members) > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ReleaseManifestError(f"artifact_archive_too_large:{label}")


def _validate_wheel_record(
    archive: zipfile.ZipFile,
    names: set[str],
    record_name: str,
    label: str,
) -> None:
    try:
        rows = list(csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))))
    except (UnicodeDecodeError, csv.Error) as error:
        raise ReleaseManifestError(f"artifact_wheel_record_invalid:{label}") from error
    if any(len(row) != 3 for row in rows) or {row[0] for row in rows} != names:
        raise ReleaseManifestError(f"artifact_wheel_record_invalid:{label}")
    for name, encoded_digest, encoded_size in rows:
        if name == record_name:
            if encoded_digest or encoded_size:
                raise ReleaseManifestError(f"artifact_wheel_record_invalid:{label}")
            continue
        payload = archive.read(name)
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        if encoded_digest != "sha256=" + digest.decode("ascii"):
            raise ReleaseManifestError(f"artifact_wheel_record_invalid:{label}")
        if encoded_size != str(len(payload)):
            raise ReleaseManifestError(f"artifact_wheel_record_invalid:{label}")


def _validate_provenance(
    payload: bytes,
    expected: Mapping[str, object],
    label: str,
) -> None:
    try:
        provenance = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError(f"artifact_provenance_invalid:{label}") from error
    if (
        not isinstance(provenance, dict)
        or set(provenance) != _PROVENANCE_FIELDS
        or provenance != expected
        or payload != _canonical(provenance) + b"\n"
        or provenance.get("schema_version") != 1
        or not _GIT_SHA.fullmatch(str(provenance.get("git_sha", "")))
        or not _DIGEST.fullmatch(str(provenance.get("source_digest", "")))
        or not _DIGEST.fullmatch(str(provenance.get("platform_source_digest", "")))
    ):
        raise ReleaseManifestError(f"artifact_provenance_invalid:{label}")


def _validate_source_payloads(
    actual: Mapping[str, bytes],
    expected: Mapping[str, bytes],
    label: str,
) -> None:
    if set(actual) != set(expected):
        raise ReleaseManifestError(f"artifact_source_set_mismatch:{label}")
    actual_digest = _records_digest(_payload_records(actual))
    expected_digest = _records_digest(_payload_records(expected))
    if actual_digest != expected_digest:
        raise ReleaseManifestError(f"artifact_source_digest_mismatch:{label}")


def _validate_wheel(
    path: Path,
    *,
    root: Path,
    component: str,
    metadata: Mapping[str, str],
    provenance: Mapping[str, object],
    source_payloads: Mapping[str, bytes],
) -> None:
    label = f"{component}-wheel"
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            _validate_archive_limits(
                label,
                [(info.filename, info.file_size) for info in infos],
            )
            if any(
                stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK for info in infos
            ):
                raise ReleaseManifestError(f"artifact_archive_invalid:{label}")
            names = {info.filename for info in infos if not info.is_dir()}
            distribution_stem = re.sub(r"[-_.]+", "_", metadata["distribution"])
            dist_info = f"{distribution_stem}-{metadata['version']}.dist-info"
            metadata_name = f"{dist_info}/METADATA"
            wheel_name = f"{dist_info}/WHEEL"
            record_name = f"{dist_info}/RECORD"
            if not {metadata_name, wheel_name, record_name} <= names:
                raise ReleaseManifestError(f"artifact_metadata_invalid:{label}")
            actual_name, actual_version = _metadata_identity(
                archive.read(metadata_name),
                label,
            )
            if (
                _normalized_distribution(actual_name)
                != _normalized_distribution(metadata["distribution"])
                or actual_version != metadata["version"]
            ):
                raise ReleaseManifestError(f"artifact_identity_mismatch:{label}")
            _validate_project_metadata(
                archive.read(metadata_name),
                root=root,
                component=component,
                label=label,
            )
            _validate_wheel_control_metadata(
                archive,
                names,
                root=root,
                component=component,
                dist_info=dist_info,
                label=label,
            )

            provenance_name = (
                f"{_COMPONENT_SOURCES[component]['provenance_package']}/"
                f"{_PROVENANCE_FILENAME}"
            )
            if provenance_name not in names:
                raise ReleaseManifestError(f"artifact_provenance_missing:{label}")
            _validate_provenance(archive.read(provenance_name), provenance, label)

            package_prefixes = tuple(
                f"{package}/" for package in _COMPONENT_SOURCES[component]["packages"]
            )
            if any(
                not name.startswith(package_prefixes)
                and not name.startswith(f"{dist_info}/")
                for name in names
            ):
                raise ReleaseManifestError(f"artifact_wheel_content_invalid:{label}")
            _validate_wheel_record(archive, names, record_name, label)
            actual_sources = {
                name: archive.read(name)
                for name in names
                if name.startswith(package_prefixes)
                and name != provenance_name
                and not name.endswith((".pyc", ".pyo"))
                and "/__pycache__/" not in name
            }
            _validate_source_payloads(actual_sources, source_payloads, label)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ReleaseManifestError(f"artifact_archive_invalid:{label}") from error


def _validate_sdist(
    path: Path,
    *,
    root: Path,
    component: str,
    metadata: Mapping[str, str],
    provenance: Mapping[str, object],
    source_payloads: Mapping[str, bytes],
) -> None:
    label = f"{component}-sdist"
    prefix = (
        re.sub(r"[-_.]+", "_", metadata["distribution"]) + "-" + metadata["version"]
    )
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            _validate_archive_limits(
                label,
                [(member.name, member.size) for member in members],
            )
            if any(
                member.name != prefix and not member.name.startswith(f"{prefix}/")
                for member in members
            ):
                raise ReleaseManifestError(f"artifact_archive_invalid:{label}")
            if any(member.issym() or member.islnk() for member in members):
                raise ReleaseManifestError(f"artifact_archive_invalid:{label}")
            if any(not member.isfile() and not member.isdir() for member in members):
                raise ReleaseManifestError(f"artifact_archive_invalid:{label}")
            files = {member.name: member for member in members if member.isfile()}
            metadata_name = f"{prefix}/PKG-INFO"
            if metadata_name not in files:
                raise ReleaseManifestError(f"artifact_metadata_invalid:{label}")

            def read(name: str) -> bytes:
                handle = archive.extractfile(files[name])
                if handle is None:
                    raise ReleaseManifestError(f"artifact_archive_invalid:{label}")
                return handle.read()

            actual_name, actual_version = _metadata_identity(read(metadata_name), label)
            if (
                _normalized_distribution(actual_name)
                != _normalized_distribution(metadata["distribution"])
                or actual_version != metadata["version"]
            ):
                raise ReleaseManifestError(f"artifact_identity_mismatch:{label}")
            _validate_project_metadata(
                read(metadata_name),
                root=root,
                component=component,
                label=label,
            )

            provenance_name = (
                f"{prefix}/src/"
                f"{_COMPONENT_SOURCES[component]['provenance_package']}/"
                f"{_PROVENANCE_FILENAME}"
            )
            if provenance_name not in files:
                raise ReleaseManifestError(f"artifact_provenance_missing:{label}")
            _validate_provenance(read(provenance_name), provenance, label)

            pyproject_name = f"{prefix}/pyproject.toml"
            project_root = root / _COMPONENT_PROJECT_ROOTS[component]
            if (
                pyproject_name not in files
                or read(pyproject_name)
                != (project_root / "pyproject.toml").read_bytes()
            ):
                raise ReleaseManifestError(f"artifact_sdist_project_mismatch:{label}")

            for name in files:
                relative = name.removeprefix(f"{prefix}/")
                if relative in {"PKG-INFO", "setup.cfg"}:
                    if relative == "setup.cfg" and read(name) not in {
                        b"[egg_info]\ntag_build = \ntag_date = 0\n\n",
                        b"[egg_info]\r\ntag_build = \r\ntag_date = 0\r\n\r\n",
                    }:
                        raise ReleaseManifestError(
                            f"artifact_sdist_generated_metadata_invalid:{label}"
                        )
                    continue
                relative_parts = PurePosixPath(relative).parts
                if (
                    len(relative_parts) >= 3
                    and relative_parts[0] == "src"
                    and relative_parts[1].endswith(".egg-info")
                    and relative_parts[-1] in _GENERATED_EGG_INFO_FILES
                ):
                    continue
                if name == provenance_name:
                    continue
                source = project_root / relative
                if (
                    source.is_symlink()
                    or not source.is_file()
                    or read(name) != source.read_bytes()
                ):
                    raise ReleaseManifestError(
                        f"artifact_sdist_content_mismatch:{label}:{relative}"
                    )

            package_prefixes = tuple(
                f"{prefix}/src/{package}/"
                for package in _COMPONENT_SOURCES[component]["packages"]
            )
            actual_sources = {
                name.removeprefix(f"{prefix}/src/"): read(name)
                for name in files
                if name.startswith(package_prefixes)
                and name != provenance_name
                and not name.endswith((".pyc", ".pyo"))
                and "/__pycache__/" not in name
            }
            _validate_source_payloads(actual_sources, source_payloads, label)
    except (OSError, tarfile.TarError) as error:
        raise ReleaseManifestError(f"artifact_archive_invalid:{label}") from error


def validate_release_artifacts(
    *,
    root: Path,
    git_sha: str,
    artifacts: Mapping[str, Path],
    components: Mapping[str, Mapping[str, str]] | None = None,
) -> None:
    """Validate archive structure, metadata, provenance, and source identity."""

    component_metadata = _component_metadata(root) if components is None else components
    provenance = expected_build_provenance(root, git_sha)
    platform_digests = {
        str(value["platform_source_digest"]) for value in provenance.values()
    }
    if len(platform_digests) != 1:
        raise ReleaseManifestError("artifact_platform_source_mismatch")
    for component, metadata in component_metadata.items():
        sources = _source_payloads(root, component)
        _validate_wheel(
            artifacts[f"{component}-wheel"],
            root=root,
            component=component,
            metadata=metadata,
            provenance=provenance[component],
            source_payloads=sources,
        )
        _validate_sdist(
            artifacts[f"{component}-sdist"],
            root=root,
            component=component,
            metadata=metadata,
            provenance=provenance[component],
            source_payloads=sources,
        )


def build_release_manifest(
    *,
    root: Path,
    release_id: str,
    git_sha: str,
    artifacts: Mapping[str, Path],
) -> dict[str, Any]:
    root = root.resolve()
    if not _RELEASE_ID.fullmatch(release_id):
        raise ReleaseManifestError("release_id_invalid")
    if not _GIT_SHA.fullmatch(git_sha):
        raise ReleaseManifestError("release_git_sha_invalid")

    components = _component_metadata(root)
    expected_labels = {
        f"{component}-{kind}" for component in components for kind in ("wheel", "sdist")
    }
    if set(artifacts) != expected_labels:
        raise ReleaseManifestError("release_artifact_labels_invalid")
    validate_release_artifacts(
        root=root,
        git_sha=git_sha,
        artifacts=artifacts,
        components=components,
    )
    artifact_records = {
        label: {"filename": path.name, **_file_record(path)}
        for label, path in sorted(artifacts.items())
    }
    migrations = _migration_metadata(root)
    migration_digest = _records_digest(migrations)
    build_digest = _records_digest(artifact_records)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "git_sha": git_sha,
        "components": components,
        "migrations": migrations,
        "migration_digest": migration_digest,
        "lock_digest": _file_record(root / "uv.lock")["sha256"],
        "deployment_digest": _deployment_digest(root),
        "artifacts": artifact_records,
        "build_digest": build_digest,
    }
    payload["release_bundle_digest"] = _records_digest(payload)
    return payload


def _git(root: Path, arguments: Sequence[str]) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def ensure_clean_checkout(root: Path) -> str:
    git_sha = _git(root, ["rev-parse", "--verify", "HEAD"])
    if not _GIT_SHA.fullmatch(git_sha):
        raise ReleaseManifestError("release_git_sha_invalid")
    status = _git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    if status:
        raise ReleaseManifestError("release_checkout_not_clean")
    return git_sha


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--artifacts-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    git_sha = ensure_clean_checkout(root)
    components = _component_metadata(root)
    artifacts = discover_artifacts(root, args.artifacts_dir, components)
    manifest = build_release_manifest(
        root=root,
        release_id=args.release_id,
        git_sha=git_sha,
        artifacts=artifacts,
    )
    _atomic_write(args.output.resolve(), _canonical(manifest))
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
