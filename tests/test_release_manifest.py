from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "release_manifest.py"
SPEC = importlib.util.spec_from_file_location("release_manifest_tool", MODULE_PATH)
assert SPEC and SPEC.loader
release_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_manifest)
sys.path.insert(0, str(ROOT / "tools"))
import build_release_artifacts  # noqa: E402

PREFLIGHT_MODULE_PATH = (
    ROOT
    / "services"
    / "research_operations"
    / "deploy"
    / "native"
    / "bin"
    / "preflight.py"
)
PREFLIGHT_SPEC = importlib.util.spec_from_file_location(
    "release_manifest_native_preflight",
    PREFLIGHT_MODULE_PATH,
)
assert PREFLIGHT_SPEC and PREFLIGHT_SPEC.loader
native_preflight = importlib.util.module_from_spec(PREFLIGHT_SPEC)
PREFLIGHT_SPEC.loader.exec_module(native_preflight)


def _deployment_fixture(root: Path) -> tuple[Path, Path, Path]:
    operations = root / "services" / "research_operations"
    marker = operations / "deploy" / "OFFICIAL_DEPLOYMENT"
    native_policy = operations / "deploy" / "native" / "policy.conf"
    runtime_script = operations / "scripts" / "runtime-entrypoint.py"
    marker.parent.mkdir(parents=True)
    native_policy.parent.mkdir(parents=True)
    runtime_script.parent.mkdir(parents=True)
    marker.write_text("native-systemd\n", encoding="utf-8")
    native_policy.write_text("policy=true\n", encoding="utf-8")
    runtime_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    marker.chmod(0o644)
    native_policy.chmod(0o640)
    runtime_script.chmod(0o755)
    return marker, native_policy, runtime_script


def _deployment_digests(root: Path) -> tuple[str, str]:
    return (
        release_manifest._deployment_digest(root),
        native_preflight._deployment_digest(root),
    )


def _assert_deployment_invalid(root: Path) -> None:
    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match="release_deployment_invalid",
    ):
        release_manifest._deployment_digest(root)
    with pytest.raises(
        native_preflight.PreflightError,
        match="release_deployment_invalid",
    ):
        native_preflight._deployment_digest(root)


def test_release_and_preflight_deployment_digests_match() -> None:
    release_digest, preflight_digest = _deployment_digests(ROOT)
    assert release_digest == preflight_digest


def test_deployment_digest_binds_marker_script_content_and_modes(
    tmp_path: Path,
) -> None:
    marker, _native_policy, runtime_script = _deployment_fixture(tmp_path)
    baseline = _deployment_digests(tmp_path)
    assert baseline[0] == baseline[1]

    marker.write_text("container-reference\n", encoding="utf-8")
    marker_content_changed = _deployment_digests(tmp_path)
    assert marker_content_changed[0] == marker_content_changed[1]
    assert marker_content_changed[0] != baseline[0]
    marker.write_text("native-systemd\n", encoding="utf-8")

    runtime_script.write_text(
        "#!/usr/bin/env python3\nprint('changed')\n",
        encoding="utf-8",
    )
    script_content_changed = _deployment_digests(tmp_path)
    assert script_content_changed[0] == script_content_changed[1]
    assert script_content_changed[0] != baseline[0]
    runtime_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    runtime_script.chmod(0o700)
    script_mode_changed = _deployment_digests(tmp_path)
    assert script_mode_changed[0] == script_mode_changed[1]
    assert script_mode_changed[0] != baseline[0]
    runtime_script.chmod(0o755)

    marker.chmod(0o600)
    marker_mode_changed = _deployment_digests(tmp_path)
    assert marker_mode_changed[0] == marker_mode_changed[1]
    assert marker_mode_changed[0] != baseline[0]


def test_deployment_digest_ignores_generated_bytecode_but_binds_new_source(
    tmp_path: Path,
) -> None:
    _marker, native_policy, _runtime_script = _deployment_fixture(tmp_path)
    baseline = _deployment_digests(tmp_path)

    bytecode = native_policy.parent / "__pycache__" / "policy.cpython-312.pyc"
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"generated-bytecode-is-not-a-release-input")
    assert _deployment_digests(tmp_path) == baseline

    additional_policy = native_policy.parent / "additional-policy.conf"
    additional_policy.write_text("policy=added\n", encoding="utf-8")
    changed = _deployment_digests(tmp_path)
    assert changed[0] == changed[1]
    assert changed[0] != baseline[0]


def test_deployment_digest_fails_closed_for_missing_inputs(tmp_path: Path) -> None:
    marker, _native_policy, _runtime_script = _deployment_fixture(tmp_path)
    marker.unlink()
    _assert_deployment_invalid(tmp_path)

    second_root = tmp_path / "missing-scripts"
    _marker, _native_policy, runtime_script = _deployment_fixture(second_root)
    runtime_script.unlink()
    _assert_deployment_invalid(second_root)


def test_deployment_digest_rejects_symlinks(tmp_path: Path) -> None:
    marker, _native_policy, runtime_script = _deployment_fixture(tmp_path)
    (runtime_script.parent / "linked-policy").symlink_to(marker)
    _assert_deployment_invalid(tmp_path)


def _write_nondeterministic_sdist(
    path: Path,
    *,
    gzip_mtime: int,
    member_mtime: int,
) -> None:
    with path.open("wb") as output:
        with gzip.GzipFile(
            filename="source.tar",
            mode="wb",
            fileobj=output,
            mtime=gzip_mtime,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                member = tarfile.TarInfo("package-1.0/payload.txt")
                payload = b"identical-payload\n"
                member.size = len(payload)
                member.mtime = member_mtime + 0.5
                member.uid = 1000
                member.gid = 1000
                member.uname = "builder"
                member.gname = "builder"
                archive.addfile(member, io.BytesIO(payload))


def test_sdist_normalization_is_byte_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    _write_nondeterministic_sdist(first, gzip_mtime=10, member_mtime=20)
    _write_nondeterministic_sdist(second, gzip_mtime=30, member_mtime=40)

    epoch = 1_700_000_000
    build_release_artifacts._normalize_sdist(first, epoch)
    build_release_artifacts._normalize_sdist(second, epoch)

    assert first.read_bytes() == second.read_bytes()
    with tarfile.open(first, "r:gz") as archive:
        member = archive.getmember("package-1.0/payload.txt")
        assert member.mtime == epoch
        assert member.uid == member.gid == 0
        assert member.uname == member.gname == ""
        assert archive.extractfile(member).read() == b"identical-payload\n"


def _metadata(distribution: str, version: str, component: str) -> bytes:
    project = release_manifest._project_configuration(ROOT, component)
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {distribution}",
        f"Version: {version}",
        f"Requires-Python: {project['requires-python']}",
    ]
    lines.extend(f"Requires-Dist: {item}" for item in project.get("dependencies", []))
    for extra, requirements in project.get("optional-dependencies", {}).items():
        lines.append(f"Provides-Extra: {extra}")
        lines.extend(
            f'Requires-Dist: {item}; extra == "{extra}"' for item in requirements
        )
    return ("\n".join(lines) + "\n\n").encode()


def _add_tar_file(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mtime = 0
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(payload))


def _wheel_record(members: dict[str, bytes], record_name: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, payload in sorted(members.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        writer.writerow((name, "sha256=" + digest.decode(), str(len(payload))))
    writer.writerow((record_name, "", ""))
    return output.getvalue().encode()


def _replace_wheel_member(path: Path, member_name: str, payload: bytes) -> None:
    with zipfile.ZipFile(path) as archive:
        members = {
            name: archive.read(name)
            for name in archive.namelist()
            if not name.endswith("/RECORD")
        }
        record_name = next(
            name for name in archive.namelist() if name.endswith("/RECORD")
        )
    members[member_name] = payload
    members[record_name] = _wheel_record(members, record_name)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            archive.writestr(name, content)


def _artifacts(
    tmp_path: Path,
    *,
    git_sha: str,
    embedded_git_sha: str | None = None,
    source_override: tuple[str, str, bytes] | None = None,
) -> dict[str, Path]:
    components = release_manifest._component_metadata(ROOT)
    provenance = release_manifest.expected_build_provenance(
        ROOT,
        git_sha if embedded_git_sha is None else embedded_git_sha,
    )
    result = {}
    for component, component_metadata in components.items():
        distribution = component_metadata["distribution"]
        version = component_metadata["version"]
        filename_stem = distribution.replace("-", "_")
        source_payloads = release_manifest._source_payloads(ROOT, component)
        if source_override is not None and source_override[0] == component:
            source_payloads[source_override[1]] = source_override[2]
        provenance_package = release_manifest._COMPONENT_SOURCES[component][
            "provenance_package"
        ]
        provenance_payload = release_manifest._canonical(provenance[component]) + b"\n"

        wheel = tmp_path / f"{filename_stem}-{version}-py3-none-any.whl"
        dist_info = f"{filename_stem}-{version}.dist-info"
        wheel_members = dict(source_payloads)
        wheel_members[f"{provenance_package}/_build_provenance.json"] = (
            provenance_payload
        )
        wheel_members[f"{dist_info}/METADATA"] = _metadata(
            distribution, version, component
        )
        wheel_members[f"{dist_info}/WHEEL"] = (
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n\n"
        )
        project = release_manifest._project_configuration(ROOT, component)
        scripts = project.get("scripts", {})
        if scripts:
            entry_points = ["[console_scripts]"]
            entry_points.extend(f"{key} = {value}" for key, value in scripts.items())
            wheel_members[f"{dist_info}/entry_points.txt"] = (
                "\n".join(entry_points) + "\n"
            ).encode()
        wheel_members[f"{dist_info}/top_level.txt"] = (
            "\n".join(release_manifest._COMPONENT_SOURCES[component]["packages"]) + "\n"
        ).encode()
        record_name = f"{dist_info}/RECORD"
        wheel_members[record_name] = _wheel_record(wheel_members, record_name)
        with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in sorted(wheel_members.items()):
                archive.writestr(name, payload)

        sdist = tmp_path / f"{filename_stem}-{version}.tar.gz"
        archive_prefix = f"{filename_stem}-{version}"
        with tarfile.open(sdist, "w:gz") as archive:
            _add_tar_file(
                archive,
                f"{archive_prefix}/PKG-INFO",
                _metadata(distribution, version, component),
            )
            project_path = release_manifest._COMPONENTS[component]
            _add_tar_file(
                archive,
                f"{archive_prefix}/pyproject.toml",
                (ROOT / project_path).read_bytes(),
            )
            for name, payload in sorted(source_payloads.items()):
                _add_tar_file(archive, f"{archive_prefix}/src/{name}", payload)
            _add_tar_file(
                archive,
                f"{archive_prefix}/src/{provenance_package}/_build_provenance.json",
                provenance_payload,
            )
        result[f"{component}-wheel"] = wheel
        result[f"{component}-sdist"] = sdist
    return result


def test_release_manifest_binds_every_distribution_and_migration(
    tmp_path: Path,
) -> None:
    git_sha = "a" * 40
    manifest = release_manifest.build_release_manifest(
        root=ROOT,
        release_id="platform-2026.07.17",
        git_sha=git_sha,
        artifacts=_artifacts(tmp_path, git_sha=git_sha),
    )

    assert manifest["schema_version"] == 1
    assert manifest["git_sha"] == git_sha
    assert manifest["components"] == {
        "core": {"distribution": "market-research", "version": "0.1.0"},
        "web": {
            "distribution": "market-research-internal-web",
            "version": "0.1.0",
        },
        "operations": {
            "distribution": "research-operations",
            "version": "0.1.0",
        },
    }
    assert manifest["migrations"]["web"]["latest"].startswith("0008_")
    assert manifest["migrations"]["operations"]["latest"].startswith("0004_")
    assert set(manifest["artifacts"]) == {
        "core-wheel",
        "core-sdist",
        "web-wheel",
        "web-sdist",
        "operations-wheel",
        "operations-sdist",
    }
    for key in (
        "migration_digest",
        "lock_digest",
        "deployment_digest",
        "build_digest",
        "release_bundle_digest",
    ):
        assert manifest[key].startswith("sha256:")
        assert len(manifest[key]) == 71


def test_release_manifest_is_deterministic(tmp_path: Path) -> None:
    git_sha = "b" * 40
    artifacts = _artifacts(tmp_path, git_sha=git_sha)
    first = release_manifest.build_release_manifest(
        root=ROOT,
        release_id="platform-test",
        git_sha=git_sha,
        artifacts=artifacts,
    )
    second = release_manifest.build_release_manifest(
        root=ROOT,
        release_id="platform-test",
        git_sha=git_sha,
        artifacts=dict(reversed(tuple(artifacts.items()))),
    )
    assert first == second


def test_release_manifest_rejects_filename_only_fake_artifacts(tmp_path: Path) -> None:
    git_sha = "c" * 40
    artifacts = _artifacts(tmp_path, git_sha=git_sha)
    artifacts["core-wheel"].write_bytes(b"not a wheel")

    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match="artifact_archive_invalid:core-wheel",
    ):
        release_manifest.build_release_manifest(
            root=ROOT,
            release_id="platform-test",
            git_sha=git_sha,
            artifacts=artifacts,
        )


def test_release_manifest_rejects_artifacts_from_another_commit(tmp_path: Path) -> None:
    artifacts = _artifacts(
        tmp_path,
        git_sha="d" * 40,
        embedded_git_sha="e" * 40,
    )

    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match="artifact_provenance_invalid:core-wheel",
    ):
        release_manifest.build_release_manifest(
            root=ROOT,
            release_id="platform-test",
            git_sha="d" * 40,
            artifacts=artifacts,
        )


def test_release_manifest_rejects_wheel_metadata_drift(tmp_path: Path) -> None:
    git_sha = "4" * 40
    artifacts = _artifacts(tmp_path, git_sha=git_sha)
    metadata_name = "market_research-0.1.0.dist-info/METADATA"
    _replace_wheel_member(
        artifacts["core-wheel"],
        metadata_name,
        b"Metadata-Version: 2.4\nName: market-research\nVersion: 0.1.0\n"
        b"Requires-Python: >=3.12\nRequires-Dist: arbitrary-code\n\n",
    )

    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match="artifact_dependencies_mismatch:core-wheel",
    ):
        release_manifest.build_release_manifest(
            root=ROOT,
            release_id="platform-test",
            git_sha=git_sha,
            artifacts=artifacts,
        )


def test_release_manifest_rejects_source_payload_drift(tmp_path: Path) -> None:
    git_sha = "f" * 40
    artifacts = _artifacts(
        tmp_path,
        git_sha=git_sha,
        source_override=("core", "market_research/__init__.py", b"tampered\n"),
    )

    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match="artifact_source_digest_mismatch:core-wheel",
    ):
        release_manifest.build_release_manifest(
            root=ROOT,
            release_id="platform-test",
            git_sha=git_sha,
            artifacts=artifacts,
        )


def test_release_manifest_rejects_incomplete_artifacts(tmp_path: Path) -> None:
    git_sha = "1" * 40
    artifacts = _artifacts(tmp_path, git_sha=git_sha)
    artifacts.pop("operations-sdist")

    with pytest.raises(
        release_manifest.ReleaseManifestError,
        match="release_artifact_labels_invalid",
    ):
        release_manifest.build_release_manifest(
            root=ROOT,
            release_id="platform-test",
            git_sha=git_sha,
            artifacts=artifacts,
        )


def test_embedded_provenance_is_canonical_and_cross_component_bound(
    tmp_path: Path,
) -> None:
    git_sha = "2" * 40
    artifacts = _artifacts(tmp_path, git_sha=git_sha)
    provenances = []
    for component in ("core", "web", "operations"):
        package = release_manifest._COMPONENT_SOURCES[component]["provenance_package"]
        with zipfile.ZipFile(artifacts[f"{component}-wheel"]) as archive:
            payload = archive.read(f"{package}/_build_provenance.json")
        assert payload.endswith(b"\n")
        provenances.append(json.loads(payload))

    assert {item["git_sha"] for item in provenances} == {git_sha}
    assert len({item["platform_source_digest"] for item in provenances}) == 1
    assert len({item["source_digest"] for item in provenances}) == 3
