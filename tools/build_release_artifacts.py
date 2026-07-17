#!/usr/bin/env python3
"""Build provenance-bearing release artifacts from the exact clean Git commit."""

from __future__ import annotations

import argparse
import copy
import gzip
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import release_manifest


class ReleaseBuildError(RuntimeError):
    """Raised when a release cannot be built from an immutable source snapshot."""


def _safe_extract(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path, "r:") as archive:
        members = archive.getmembers()
        for member in members:
            logical = PurePosixPath(member.name)
            if (
                logical.is_absolute()
                or ".." in logical.parts
                or member.issym()
                or member.islnk()
            ):
                raise ReleaseBuildError("git_archive_member_unsafe")
        archive.extractall(destination, members=members, filter="data")


def _inject_provenance(snapshot: Path, git_sha: str) -> None:
    provenance = release_manifest.expected_build_provenance(snapshot, git_sha)
    for component, payload in provenance.items():
        configuration = release_manifest._COMPONENT_SOURCES[component]
        path = (
            snapshot
            / configuration["source_root"]
            / configuration["provenance_package"]
            / release_manifest._PROVENANCE_FILENAME
        )
        path.write_bytes(release_manifest._canonical(payload) + b"\n")


def _normalize_sdist(path: Path, source_date_epoch: int) -> None:
    """Rewrite one sdist with deterministic tar and gzip metadata."""

    if source_date_epoch < 0:
        raise ReleaseBuildError("source_date_epoch_invalid")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".normalized",
        dir=path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with (
            tarfile.open(path, "r:gz") as source,
            temporary.open("wb") as output,
        ):
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=output,
                compresslevel=9,
                mtime=source_date_epoch,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as target:
                    for member in source.getmembers():
                        normalized = copy.copy(member)
                        normalized.mtime = source_date_epoch
                        normalized.uid = 0
                        normalized.gid = 0
                        normalized.uname = ""
                        normalized.gname = ""
                        normalized.pax_headers = {
                            key: value
                            for key, value in member.pax_headers.items()
                            if key not in {"mtime", "atime", "ctime"}
                        }
                        payload = (
                            source.extractfile(member) if member.isfile() else None
                        )
                        target.addfile(normalized, payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except (OSError, tarfile.TarError) as error:
        raise ReleaseBuildError("sdist_normalization_failed") from error
    finally:
        temporary.unlink(missing_ok=True)


def build_release_artifacts(root: Path, output_directory: Path) -> str:
    root = root.resolve()
    output_directory = output_directory.resolve()
    git_sha = release_manifest.ensure_clean_checkout(root)
    commit_timestamp = release_manifest._git(
        root,
        ["show", "-s", "--format=%ct", git_sha],
    )
    if not commit_timestamp.isdecimal():
        raise ReleaseBuildError("git_commit_timestamp_invalid")

    with tempfile.TemporaryDirectory(prefix="research-platform-build-") as temporary:
        temporary_root = Path(temporary)
        git_archive = temporary_root / "source.tar"
        snapshot = temporary_root / "source"
        built = temporary_root / "artifacts"
        snapshot.mkdir()
        subprocess.run(
            [
                "git",
                "archive",
                "--format=tar",
                f"--output={git_archive}",
                git_sha,
            ],
            cwd=root,
            check=True,
        )
        _safe_extract(git_archive, snapshot)
        _inject_provenance(snapshot, git_sha)

        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["SOURCE_DATE_EPOCH"] = commit_timestamp
        subprocess.run(
            [
                "uv",
                "build",
                "--all-packages",
                "--out-dir",
                str(built),
                "--clear",
                "--no-create-gitignore",
            ],
            cwd=snapshot,
            env=environment,
            check=True,
        )
        for sdist in sorted(built.glob("*.tar.gz")):
            _normalize_sdist(sdist, int(commit_timestamp))
        components = release_manifest._component_metadata(snapshot)
        artifacts = release_manifest.discover_artifacts(snapshot, built, components)
        release_manifest.build_release_manifest(
            root=snapshot,
            release_id="build-validation",
            git_sha=git_sha,
            artifacts=artifacts,
        )

        output_directory.mkdir(parents=True, exist_ok=True)
        for artifact in artifacts.values():
            temporary_target = output_directory / f".{artifact.name}.pending"
            shutil.copyfile(artifact, temporary_target)
            os.chmod(temporary_target, 0o644)
            os.replace(temporary_target, output_directory / artifact.name)

    return git_sha


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = Path(__file__).resolve().parents[1]
    git_sha = build_release_artifacts(root, args.output_dir)
    print(f"built_release_git_sha={git_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
