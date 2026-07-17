#!/usr/bin/env python3
"""Install an admitted platform release from exact wheels into a new venv."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

import release_manifest


class ReleaseInstallError(RuntimeError):
    """Raised before mutating an existing or unverified release environment."""


def _load_canonical_manifest(path: Path) -> dict[str, object]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ReleaseInstallError("release_manifest_not_regular_absolute_file")
    raw = path.read_bytes()
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseInstallError("release_manifest_invalid") from error
    if (
        not isinstance(manifest, dict)
        or raw != release_manifest._canonical(manifest) + b"\n"
    ):
        raise ReleaseInstallError("release_manifest_not_canonical")
    return manifest


def install_release(
    *,
    root: Path,
    manifest_path: Path,
    artifacts_directory: Path,
    venv: Path,
) -> None:
    root = root.resolve()
    if not manifest_path.is_absolute():
        raise ReleaseInstallError("release_manifest_must_be_absolute")
    if not artifacts_directory.is_absolute():
        raise ReleaseInstallError("artifacts_directory_must_be_absolute")
    manifest_path = manifest_path.resolve()
    artifacts_directory = artifacts_directory.resolve()
    if not venv.is_absolute() or venv.exists() or venv.is_symlink():
        raise ReleaseInstallError("release_venv_must_be_new_absolute_path")

    git_sha = release_manifest.ensure_clean_checkout(root)
    manifest = _load_canonical_manifest(manifest_path)
    if manifest.get("git_sha") != git_sha:
        raise ReleaseInstallError("release_manifest_git_mismatch")
    release_id = manifest.get("release_id")
    if not isinstance(release_id, str):
        raise ReleaseInstallError("release_manifest_id_invalid")
    components = release_manifest._component_metadata(root)
    artifacts = release_manifest.discover_artifacts(
        root,
        artifacts_directory,
        components,
    )
    expected = release_manifest.build_release_manifest(
        root=root,
        release_id=release_id,
        git_sha=git_sha,
        artifacts=artifacts,
    )
    if manifest != expected:
        raise ReleaseInstallError("release_manifest_evidence_mismatch")

    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="research-platform-install-") as temporary:
        requirements = Path(temporary) / "runtime-requirements.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--all-packages",
                "--no-dev",
                "--no-emit-workspace",
                "--output-file",
                str(requirements),
            ],
            cwd=root,
            env=environment,
            check=True,
        )
        subprocess.run(
            ["uv", "venv", "--python", "3.12", str(venv)],
            cwd=root,
            env=environment,
            check=True,
        )
        python = venv / "bin" / "python"
        subprocess.run(
            [
                "uv",
                "pip",
                "sync",
                "--python",
                str(python),
                "--require-hashes",
                str(requirements),
            ],
            cwd=root,
            env=environment,
            check=True,
        )
        wheels = [
            str(artifacts[f"{component}-wheel"])
            for component in ("core", "web", "operations")
        ]
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                *wheels,
            ],
            cwd=root,
            env=environment,
            check=True,
        )
        subprocess.run(
            [
                str(python),
                str(root / "tools" / "verify_installed_release.py"),
                "--manifest",
                str(manifest_path),
            ],
            cwd=temporary,
            env=environment,
            check=True,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--artifacts-dir", required=True, type=Path)
    parser.add_argument("--venv", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = Path(__file__).resolve().parents[1]
    install_release(
        root=root,
        manifest_path=args.manifest,
        artifacts_directory=args.artifacts_dir,
        venv=args.venv,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
