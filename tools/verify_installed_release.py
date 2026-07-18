#!/usr/bin/env python3
"""Verify that this interpreter runs the exact wheel-bound platform release."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_COMPONENTS = {
    "core": ("market-research", ("market_research",), "market_research"),
    "web": (
        "market-research-internal-web",
        ("market_research_web", "portal"),
        "market_research_web",
    ),
    "operations": (
        "research-operations",
        ("research_operations",),
        "research_operations",
    ),
}
_PROVENANCE_FIELDS = {
    "schema_version",
    "distribution",
    "version",
    "git_sha",
    "source_digest",
    "platform_source_digest",
}


class InstalledReleaseError(RuntimeError):
    """Raised when installed distributions do not match release evidence."""


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _object_digest(payload: object) -> str:
    return _sha256(_canonical(payload))


def _inside_environment(path: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(Path(sys.prefix).resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _load_manifest(path: Path) -> dict[str, object]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise InstalledReleaseError("release_manifest_not_regular_absolute_file")
    raw = path.read_bytes()
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InstalledReleaseError("release_manifest_invalid") from error
    if not isinstance(manifest, dict) or raw != _canonical(manifest) + b"\n":
        raise InstalledReleaseError("release_manifest_not_canonical")
    if manifest.get("schema_version") != 1:
        raise InstalledReleaseError("release_manifest_schema_invalid")
    git_sha = manifest.get("git_sha")
    if not isinstance(git_sha, str) or not _GIT_SHA.fullmatch(git_sha):
        raise InstalledReleaseError("release_git_sha_invalid")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise InstalledReleaseError("release_artifacts_invalid")
    if _object_digest(artifacts) != manifest.get("build_digest"):
        raise InstalledReleaseError("release_build_digest_invalid")
    unsigned = dict(manifest)
    unsigned.pop("release_bundle_digest", None)
    if _object_digest(unsigned) != manifest.get("release_bundle_digest"):
        raise InstalledReleaseError("release_bundle_digest_invalid")
    return manifest


def _installed_payload_digest(
    distribution: importlib.metadata.Distribution,
    package_prefixes: tuple[str, ...],
) -> str:
    records = []
    files = distribution.files
    if files is None:
        raise InstalledReleaseError("installed_file_record_missing")
    for package_path in sorted(files, key=lambda item: str(item)):
        logical = package_path.as_posix()
        if not logical.startswith(tuple(f"{prefix}/" for prefix in package_prefixes)):
            continue
        if (
            logical.endswith("/_build_provenance.json")
            or logical.endswith((".pyc", ".pyo"))
            or "/__pycache__/" in logical
        ):
            continue
        path = Path(distribution.locate_file(package_path))
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise InstalledReleaseError("installed_payload_missing") from error
        if path.is_symlink() or not resolved.is_file() or not _inside_environment(path):
            raise InstalledReleaseError("installed_payload_not_regular")
        payload = resolved.read_bytes()
        records.append(
            {"path": logical, "sha256": _sha256(payload), "size_bytes": len(payload)}
        )
    if not records:
        raise InstalledReleaseError("installed_payload_empty")
    return _object_digest(records)


def _direct_wheel_digest(distribution: importlib.metadata.Distribution) -> str:
    raw = distribution.read_text("direct_url.json")
    if raw is None:
        raise InstalledReleaseError("installed_distribution_not_direct_wheel")
    try:
        direct_url = json.loads(raw)
    except json.JSONDecodeError as error:
        raise InstalledReleaseError("installed_direct_url_invalid") from error
    url = direct_url.get("url") if isinstance(direct_url, dict) else None
    archive_info = (
        direct_url.get("archive_info") if isinstance(direct_url, dict) else None
    )
    if (
        not isinstance(url, str)
        or not url.lower().split("?", 1)[0].endswith(".whl")
        or not isinstance(archive_info, dict)
    ):
        raise InstalledReleaseError("installed_distribution_not_direct_wheel")
    digest = archive_info.get("hash")
    if not isinstance(digest, str) or not digest.startswith("sha256="):
        hashes = archive_info.get("hashes")
        digest = hashes.get("sha256") if isinstance(hashes, dict) else None
        if isinstance(digest, str):
            digest = "sha256=" + digest
    if isinstance(digest, str) and re.fullmatch(r"sha256=[0-9a-f]{64}", digest):
        return "sha256:" + digest.removeprefix("sha256=")

    parsed = urlsplit(url)
    wheel_path = Path(unquote(parsed.path))
    if (
        parsed.scheme != "file"
        or parsed.netloc not in {"", "localhost"}
        or not wheel_path.is_absolute()
        or wheel_path.is_symlink()
        or not wheel_path.is_file()
    ):
        raise InstalledReleaseError("installed_wheel_digest_missing")
    return _sha256(wheel_path.read_bytes())


def verify_installed_release(manifest_path: Path) -> dict[str, object]:
    manifest = _load_manifest(manifest_path)
    manifest_components = manifest.get("components")
    artifacts = manifest["artifacts"]
    if not isinstance(manifest_components, dict) or not isinstance(artifacts, dict):
        raise InstalledReleaseError("release_components_invalid")
    git_sha = str(manifest["git_sha"])
    platform_digests = set()

    for component, (
        distribution_name,
        packages,
        provenance_package,
    ) in _COMPONENTS.items():
        expected_component = manifest_components.get(component)
        if not isinstance(expected_component, dict):
            raise InstalledReleaseError(f"release_component_invalid:{component}")
        if expected_component.get("distribution") != distribution_name:
            raise InstalledReleaseError(f"release_component_invalid:{component}")
        expected_version = expected_component.get("version")
        distribution = importlib.metadata.distribution(distribution_name)
        if distribution.version != expected_version:
            raise InstalledReleaseError(f"installed_version_mismatch:{component}")

        provenance_path = Path(
            distribution.locate_file(f"{provenance_package}/_build_provenance.json")
        )
        if (
            provenance_path.is_symlink()
            or not provenance_path.is_file()
            or not _inside_environment(provenance_path)
        ):
            raise InstalledReleaseError(f"installed_provenance_missing:{component}")
        raw = provenance_path.read_bytes()
        try:
            provenance = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InstalledReleaseError(
                f"installed_provenance_invalid:{component}"
            ) from error
        if (
            not isinstance(provenance, dict)
            or set(provenance) != _PROVENANCE_FIELDS
            or raw != _canonical(provenance) + b"\n"
            or provenance.get("schema_version") != 1
            or provenance.get("distribution") != distribution_name
            or provenance.get("version") != expected_version
            or provenance.get("git_sha") != git_sha
            or not _DIGEST.fullmatch(str(provenance.get("source_digest", "")))
            or not _DIGEST.fullmatch(str(provenance.get("platform_source_digest", "")))
        ):
            raise InstalledReleaseError(f"installed_provenance_invalid:{component}")
        if (
            _installed_payload_digest(distribution, packages)
            != provenance["source_digest"]
        ):
            raise InstalledReleaseError(f"installed_source_digest_mismatch:{component}")
        platform_digests.add(provenance["platform_source_digest"])

        wheel_record = artifacts.get(f"{component}-wheel")
        if not isinstance(wheel_record, dict):
            raise InstalledReleaseError(f"release_wheel_invalid:{component}")
        if _direct_wheel_digest(distribution) != wheel_record.get("sha256"):
            raise InstalledReleaseError(f"installed_wheel_digest_mismatch:{component}")

    if len(platform_digests) != 1:
        raise InstalledReleaseError("installed_platform_source_mismatch")
    # Import only after all wheel/package-boundary checks.  Strict research
    # reproduction must fingerprint the installed package and resolved
    # environment rather than silently hashing a nonexistent checkout/src.
    from market_research.research.code_provenance import collect_code_provenance

    research_provenance = collect_code_provenance(Path.cwd())
    if (
        research_provenance.get("source_layout") != "installed_distribution"
        or int(research_provenance.get("source_file_count") or 0) < 1
        or research_provenance.get("dependency_contract_basis")
        != "resolved_installed_distributions"
    ):
        raise InstalledReleaseError("installed_research_provenance_invalid")
    return {
        "status": "VERIFIED",
        "git_sha": git_sha,
        "release_id": manifest.get("release_id"),
        "build_digest": manifest.get("build_digest"),
        "platform_source_digest": next(iter(platform_digests)),
        "research_code_provenance_hash": research_provenance["code_provenance_hash"],
        "python": os.path.realpath(sys.executable),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    print(json.dumps(verify_installed_release(args.manifest), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
