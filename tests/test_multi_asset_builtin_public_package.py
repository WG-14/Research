from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from importlib import import_module
from pathlib import Path
from typing import Callable, cast

import pytest

from market_research.research.multi_asset.builtin_runner import (
    BuiltinMultiAssetRequest,
    load_builtin_execution_record,
    write_builtin_multi_asset_request,
)
from market_research.research.multi_asset.evidence_graph import (
    EvidenceGraph,
    EvidenceGraphResolver,
    EvidenceNodeKind,
)
from market_research.research.multi_asset.validated_package import (
    ValidatedPackageError,
    reproduce_validated_package,
    verify_validated_package,
)
from market_research.research_cli.context import ResearchAppContext
from market_research.research_cli.registry import command_registry


def _public_request_fixture(
    tmp_path: Path,
) -> tuple[BuiltinMultiAssetRequest, ResearchAppContext]:
    fixture_module = import_module("tests.test_multi_asset_builtin_cli")
    fixture = cast(
        Callable[
            [Path],
            tuple[BuiltinMultiAssetRequest, ResearchAppContext],
        ],
        getattr(fixture_module, "_builtin_request"),
    )
    return fixture(tmp_path)


def _json_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert all(isinstance(key, str) for key in payload)
    return cast(dict[str, object], payload)


def _artifact_row(
    manifest: dict[str, object],
    role: str,
    *,
    logical_id: str | None = None,
) -> dict[str, object]:
    artifacts = manifest["artifacts"]
    assert isinstance(artifacts, list)
    rows = [
        cast(dict[str, object], item)
        for item in artifacts
        if isinstance(item, dict)
        and item.get("role") == role
        and (logical_id is None or item.get("logical_id") == logical_id)
    ]
    assert len(rows) == 1
    return rows[0]


def _artifact_payload(
    package: Path,
    manifest: dict[str, object],
    role: str,
    *,
    logical_id: str | None = None,
) -> dict[str, object]:
    relative_path = _artifact_row(
        manifest,
        role,
        logical_id=logical_id,
    )["relative_path"]
    assert isinstance(relative_path, str)
    return _json_object(package / relative_path)


def _cold_run(
    *,
    package: Path,
    entrypoint: str,
    working_directory: Path,
    isolated_home: Path,
) -> subprocess.CompletedProcess[str]:
    python = Path("/usr/bin/python3")
    assert python.is_file()
    return subprocess.run(
        [str(python), "-I", str(package / entrypoint), str(package)],
        cwd=working_directory,
        env={
            "HOME": str(isolated_home),
            "LANG": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _copy_and_change_artifact(
    *,
    source: Path,
    target: Path,
    role: str,
    logical_id: str | None = None,
    delete: bool,
) -> Path:
    shutil.copytree(source, target)
    manifest = _json_object(target / "manifest.json")
    relative_path = _artifact_row(
        manifest,
        role,
        logical_id=logical_id,
    )["relative_path"]
    assert isinstance(relative_path, str)
    artifact_path = target / relative_path
    if delete:
        artifact_path.unlink()
    else:
        artifact_path.write_bytes(artifact_path.read_bytes() + b" ")
    return target


def test_builtin_execute_publishes_cold_replayable_fail_closed_package(
    tmp_path: Path,
) -> None:
    request, context = _public_request_fixture(tmp_path)
    request_path = (tmp_path / "builtin-request.json").resolve()
    execution_path = (tmp_path / "builtin-execution.json").resolve()
    write_builtin_multi_asset_request(context.paths, request_path, request)
    messages: list[str] = []
    context.printer = messages.append

    result = command_registry()["research-multi-asset-execute"].handler(
        argparse.Namespace(
            request=str(request_path),
            out=str(execution_path),
        ),
        context,
    )

    assert result == 0, messages
    execution = load_builtin_execution_record(context.paths, execution_path)
    package = context.paths.research_artifact_path(
        request.spec.experiment_id,
        "portable-packages",
        request.content_hash.removeprefix("sha256:"),
    )
    assert package.is_dir()
    manifest = _json_object(package / "manifest.json")
    assert manifest["content_hash"] == execution.portable_package_manifest_hash
    assert str(context.paths.project_root) not in (package / "manifest.json").read_text(
        encoding="utf-8"
    )
    manifest_artifacts = cast(list[dict[str, object]], manifest["artifacts"])
    model_card_ids = tuple(
        sorted(
            cast(str, item["logical_id"])
            for item in manifest_artifacts
            if item["role"] == "MODEL_CARD"
        )
    )
    assert model_card_ids == (
        "card:model:builtin_public:t01",
        "card:model:builtin_public:t02",
        "card:model:builtin_public:t03",
        "card:model:builtin_public:t04",
    )

    derived = _artifact_payload(
        package,
        manifest,
        "DERIVED_EVIDENCE",
        logical_id="derived:public",
    )
    accounting = _artifact_payload(package, manifest, "ACCOUNTING")
    packaged_request = _artifact_payload(package, manifest, "REQUEST")
    evidence_graph = EvidenceGraph.from_dict(
        _artifact_payload(package, manifest, "EVIDENCE_GRAPH")
    )
    evidence_graph.require_resolvable_report_lineage()
    resolver = EvidenceGraphResolver(evidence_graph)
    report_claim = next(
        node
        for node in evidence_graph.nodes
        if node.kind is EvidenceNodeKind.REPORT_CLAIM
    )
    source_row = next(
        node
        for node in evidence_graph.nodes
        if node.kind is EvidenceNodeKind.SOURCE_ROW
    )
    upstream = resolver.machine_query(report_claim.node_id, direction="upstream")
    downstream = resolver.machine_query(source_row.node_id, direction="downstream")
    assert source_row.node_id in {
        node["node_id"] for node in cast(list[dict[str, object]], upstream["nodes"])
    }
    assert report_claim.node_id in {
        node["node_id"] for node in cast(list[dict[str, object]], downstream["nodes"])
    }
    upstream_node_ids = {
        node["node_id"] for node in cast(list[dict[str, object]], upstream["nodes"])
    }
    assert set(model_card_ids) <= upstream_node_ids
    assert derived["content_hash"] == execution.public_execution_evidence_hash
    assert accounting["study_content_hash"] == execution.study_content_hash
    assert packaged_request["content_hash"] == request.content_hash

    verification = verify_validated_package(package)
    reproduction = reproduce_validated_package(package)
    assert verification.status == "PASS"
    assert verification.manifest_hash == execution.portable_package_manifest_hash
    assert reproduction.status == "PASS"
    assert reproduction.manifest_hash == execution.portable_package_manifest_hash
    assert reproduction.mismatch_fields == ()
    assert verification.study_content_hash == execution.study_content_hash
    assert (
        reproduction.first_study_content_hash
        == reproduction.second_study_content_hash
        == verification.study_content_hash
        == execution.study_content_hash
    )

    cold_root = tmp_path / "cold-root"
    cold_working_directory = cold_root / "cwd"
    cold_home = cold_root / "home"
    cold_working_directory.mkdir(parents=True)
    cold_home.mkdir()
    cold_verify = _cold_run(
        package=package,
        entrypoint="verify.py",
        working_directory=cold_working_directory,
        isolated_home=cold_home,
    )
    assert cold_verify.returncode == 0, cold_verify.stderr
    cold_verify_receipt = json.loads(cold_verify.stdout)
    assert cold_verify_receipt["status"] == "PASS"
    assert cold_verify_receipt["manifest_hash"] == verification.manifest_hash
    cold_reproduce = _cold_run(
        package=package,
        entrypoint="reproduce.py",
        working_directory=cold_working_directory,
        isolated_home=cold_home,
    )
    assert cold_reproduce.returncode == 0, cold_reproduce.stderr
    cold_reproduction_receipt = json.loads(cold_reproduce.stdout)
    assert cold_reproduction_receipt["status"] == "PASS"
    assert cold_reproduction_receipt["mismatch_fields"] == []
    assert cold_reproduction_receipt["manifest_hash"] == reproduction.manifest_hash
    assert (
        cold_reproduction_receipt["first_study_content_hash"]
        == cold_reproduction_receipt["second_study_content_hash"]
        == execution.study_content_hash
    )
    assert tuple(cold_working_directory.iterdir()) == ()
    assert tuple(cold_home.iterdir()) == ()

    tampered = _copy_and_change_artifact(
        source=package,
        target=tmp_path / "tampered-package",
        role="DERIVED_EVIDENCE",
        logical_id="derived:public",
        delete=False,
    )
    with pytest.raises(
        ValidatedPackageError,
        match="artifact_hash_or_length_mismatch:derived:public",
    ):
        verify_validated_package(tampered)
    cold_tamper = _cold_run(
        package=tampered,
        entrypoint="verify.py",
        working_directory=cold_working_directory,
        isolated_home=cold_home,
    )
    assert cold_tamper.returncode == 1
    cold_tamper_failure = json.loads(cold_tamper.stderr)
    assert cold_tamper_failure["status"] == "FAIL"
    assert (
        cold_tamper_failure["error"]
        == "artifact_hash_or_length_mismatch:derived:public"
    )

    tampered_engine_source = _copy_and_change_artifact(
        source=package,
        target=tmp_path / "tampered-engine-source-package",
        role="ENGINE_SOURCE",
        delete=False,
    )
    with pytest.raises(
        ValidatedPackageError,
        match="artifact_hash_or_length_mismatch:source:research-engine",
    ):
        verify_validated_package(tampered_engine_source)

    missing_evidence = _copy_and_change_artifact(
        source=package,
        target=tmp_path / "missing-evidence-package",
        role="NORMALIZED_EVIDENCE",
        logical_id="normalized:public",
        delete=True,
    )
    with pytest.raises(ValidatedPackageError, match="file_set_mismatch"):
        verify_validated_package(missing_evidence)
    cold_missing = _cold_run(
        package=missing_evidence,
        entrypoint="verify.py",
        working_directory=cold_working_directory,
        isolated_home=cold_home,
    )
    assert cold_missing.returncode == 1
    cold_missing_failure = json.loads(cold_missing.stderr)
    assert cold_missing_failure["status"] == "FAIL"
    assert str(cold_missing_failure["error"]).startswith("package.file_set_mismatch:")
    assert tuple(cold_working_directory.iterdir()) == ()
    assert tuple(cold_home.iterdir()) == ()
