from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

import market_research.research.portable_research_package as portable_package_module
from market_research.paths import ResearchPathManager
from market_research.research.cli import cmd_research_backtest
from market_research.research.hashing import (
    content_hash_payload,
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research.portable_research_package import (
    PORTABLE_RESEARCH_PACKAGE_HASH_LABEL,
    PortableResearchPackageError,
    PortableResearchPackageVerification,
    build_portable_research_package,
    reproduce_portable_research_package,
    verify_portable_research_package,
)
from market_research.research.reproduction import (
    validate_reproduction_receipt_report_binding,
)
from market_research.research_cli.context import ResearchAppContext
from market_research.settings import ResearchSettings
from tests.clean_provenance_fixture import install_committed_checkout_provenance
from tests.research_sma_success_fixture import create_success_fixture


@pytest.fixture(autouse=True)
def _committed_receipt_source(monkeypatch: pytest.MonkeyPatch) -> None:
    install_committed_checkout_provenance(monkeypatch)
    for name, value in {
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LC_NUMERIC": "C",
        "PYTHONHASHSEED": "0",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "BLIS_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
    }.items():
        monkeypatch.setenv(name, value)


def _baseline(
    tmp_path: Path,
) -> tuple[ResearchPathManager, Path, Path, Path, Path, Path]:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True)
    db_path, experiment_manifest_path = create_success_fixture(source_root)
    settings = ResearchSettings(
        data_root=tmp_path / "state" / "datasets",
        artifact_root=tmp_path / "state" / "artifacts",
        report_root=tmp_path / "state" / "reports",
        cache_root=tmp_path / "state" / "cache",
        db_path=db_path,
        max_workers=1,
        random_seed=0,
    )
    manager = ResearchPathManager.from_settings(settings, project_root=Path.cwd())
    context = ResearchAppContext(settings=settings, paths=manager, printer=print)
    assert (
        cmd_research_backtest(
            context=context,
            manifest_path=str(experiment_manifest_path),
        )
        == 0
    )
    report_root = manager.report_path("research", "sma_success_import_boundary")
    result_path = report_root / "backtest_report.json"
    receipt_path = report_root / "reproduction_receipt.json"
    experiment_payload = json.loads(
        experiment_manifest_path.read_text(encoding="utf-8")
    )
    artifact_manifest_path = Path(
        experiment_payload["dataset"]["artifact_manifest_uri"]
    )
    artifact_payload = json.loads(artifact_manifest_path.read_text(encoding="utf-8"))
    dataset_path = Path(artifact_payload["artifact"]["uri"])
    return (
        manager,
        result_path,
        experiment_manifest_path,
        receipt_path,
        artifact_manifest_path,
        dataset_path,
    )


def _build_external(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path, Path, ResearchPathManager]:
    (
        manager,
        result_path,
        experiment_manifest_path,
        receipt_path,
        artifact_manifest_path,
        dataset_path,
    ) = _baseline(tmp_path)
    package_path = tmp_path / "published" / "sample.mrpkg"
    built = build_portable_research_package(
        manager=manager,
        result_path=result_path,
        experiment_manifest_path=experiment_manifest_path,
        reproduction_receipt_path=receipt_path,
        output_path=package_path,
        dataset_mode="external_content_addressed",
    )
    assert built["status"] == "BUILT_OR_VERIFIED_EXISTING"
    return (
        package_path,
        artifact_manifest_path,
        dataset_path,
        result_path,
        experiment_manifest_path,
        receipt_path,
        manager,
    )


def _archive_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path, mode="r") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _write_archive(path: Path, members: dict[str, bytes]) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=True,
    ) as archive:
        for name, raw in sorted(members.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o400) << 16
            archive.writestr(info, raw)
    path.write_bytes(buffer.getvalue())


@pytest.fixture(scope="module")
def shared_external_package(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[Path, Path, Path, Path, Path, Path, ResearchPathManager]]:
    monkeypatch = pytest.MonkeyPatch()
    install_committed_checkout_provenance(monkeypatch)
    for name, value in {
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LC_NUMERIC": "C",
        "PYTHONHASHSEED": "0",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "BLIS_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
    }.items():
        monkeypatch.setenv(name, value)
    try:
        yield _build_external(tmp_path_factory.mktemp("portable-derived-baseline"))
    finally:
        monkeypatch.undo()


def _replace_member_and_rehash_manifest(
    members: dict[str, bytes],
    *,
    role: str,
    replacement: bytes,
) -> None:
    package_manifest = json.loads(members["package-manifest.json"])
    matching_rows = [
        row for row in package_manifest["artifacts"] if row["role"] == role
    ]
    assert len(matching_rows) == 1
    row = matching_rows[0]
    members[row["relative_path"]] = replacement
    row["content_hash"] = f"sha256:{hashlib.sha256(replacement).hexdigest()}"
    row["size"] = len(replacement)
    package_manifest["content_hash"] = sha256_prefixed(
        {
            key: value
            for key, value in package_manifest.items()
            if key != "content_hash"
        },
        label=PORTABLE_RESEARCH_PACKAGE_HASH_LABEL,
    )
    members["package-manifest.json"] = (
        json.dumps(
            package_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def test_external_package_is_deterministic_complete_and_explicit(
    tmp_path: Path,
) -> None:
    (
        package_path,
        artifact_manifest_path,
        dataset_path,
        result_path,
        experiment_manifest_path,
        receipt_path,
        manager,
    ) = _build_external(tmp_path)
    before = package_path.read_bytes()

    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_external_dataset_inputs_required",
    ):
        verify_portable_research_package(package_path)

    verification = verify_portable_research_package(
        package_path,
        external_artifact_manifest_path=artifact_manifest_path,
        external_dataset_path=dataset_path,
    )
    assert verification.status == "PASS"
    assert verification.package_file_hash == (
        f"sha256:{hashlib.sha256(package_path.read_bytes()).hexdigest()}"
    )
    assert verification.dataset_mode == "external_content_addressed"
    assert verification.publication_status == "NON_PROMOTABLE_RESEARCH_ONLY_PACKAGE"
    assert "AUTHORITATIVE" not in verification.publication_status

    members = _archive_members(package_path)
    assert "dataset/candles.sqlite" not in members
    manifest = json.loads(members["package-manifest.json"])
    assert manifest["dataset_requirement"]["external_input_required"] is True
    assert manifest["dataset_requirement"]["license_evidence"]["status"] == (
        "EXTERNAL_CONTENT_ADDRESSED_INPUT_REQUIRED"
    )
    assert manifest["completeness"]["status"] == "INCOMPLETE"
    assert set(manifest["completeness"]["h01_sections"]) == {
        "research_summary",
        "hypothesis_document",
        "data_manifest",
        "code_manifest",
        "experiment_manifest",
        "result_package",
        "validation_report",
        "limitations",
        "reproduction_command",
    }

    build_portable_research_package(
        manager=manager,
        result_path=result_path,
        experiment_manifest_path=experiment_manifest_path,
        reproduction_receipt_path=receipt_path,
        output_path=package_path,
        dataset_mode="external_content_addressed",
    )
    assert package_path.read_bytes() == before


@pytest.mark.parametrize("dataset_mode", ("included", "external_content_addressed"))
@pytest.mark.parametrize(
    ("section", "nested_in_dataset"),
    (
        ("top_of_book", True),
        ("depth", True),
        ("corporate_action_set", False),
        ("universe", False),
        ("market_calendar", False),
        ("etf_nav", False),
    ),
)
def test_package_rejects_additional_execution_inputs_until_portably_bound(
    tmp_path: Path,
    shared_external_package: tuple[
        Path, Path, Path, Path, Path, Path, ResearchPathManager
    ],
    dataset_mode: str,
    section: str,
    nested_in_dataset: bool,
) -> None:
    (
        _,
        _,
        _,
        result_path,
        experiment_manifest_path,
        receipt_path,
        manager,
    ) = shared_external_package
    manifest = json.loads(experiment_manifest_path.read_text(encoding="utf-8"))
    if nested_in_dataset:
        manifest["dataset"][section] = {
            "source": "content_addressed_test_fixture",
            "required": True,
            "source_uri": str((tmp_path / f"{section}.sqlite").resolve()),
        }
    else:
        manifest[section] = {
            "source_uri": str((tmp_path / f"{section}.json").resolve())
        }
    manifest_path = tmp_path / f"manifest-{section}-{dataset_mode}.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        PortableResearchPackageError,
        match=(
            f"portable_package_additional_execution_inputs_not_portably_bound:{section}"
        ),
    ):
        build_portable_research_package(
            manager=manager,
            result_path=result_path,
            experiment_manifest_path=manifest_path,
            reproduction_receipt_path=receipt_path,
            output_path=tmp_path / f"unsupported-{section}-{dataset_mode}.mrpkg",
            dataset_mode=dataset_mode,
        )


def test_replay_rejects_archive_changed_after_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_external_package: tuple[
        Path, Path, Path, Path, Path, Path, ResearchPathManager
    ],
) -> None:
    source_package, artifact_manifest_path, dataset_path, *_ = shared_external_package
    package_path = tmp_path / "replace-after-verification.mrpkg"
    package_path.write_bytes(source_package.read_bytes())
    original_verify = portable_package_module.verify_portable_research_package

    def verify_then_replace(
        package_arg: str | Path,
        *,
        external_artifact_manifest_path: str | Path | None = None,
        external_dataset_path: str | Path | None = None,
    ) -> PortableResearchPackageVerification:
        verification = original_verify(
            package_arg,
            external_artifact_manifest_path=external_artifact_manifest_path,
            external_dataset_path=external_dataset_path,
        )
        package_path.write_bytes(b"different-package-after-verification")
        return verification

    monkeypatch.setattr(
        portable_package_module,
        "verify_portable_research_package",
        verify_then_replace,
    )
    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_changed_after_verification",
    ):
        reproduce_portable_research_package(
            package_path,
            workspace=tmp_path / "changed-package-replay",
            external_artifact_manifest_path=artifact_manifest_path,
            external_dataset_path=dataset_path,
        )


@pytest.mark.parametrize(
    "role",
    (
        "RESEARCH_SUMMARY",
        "HYPOTHESIS_DOCUMENT",
        "DATA_MANIFEST",
        "CODE_MANIFEST",
        "ENVIRONMENT_MANIFEST",
        "PARAMETER_MANIFEST",
        "RESULT_INDEX",
        "VALIDATION_REPORT",
        "LIMITATIONS",
        "REPRODUCTION_PLAN",
    ),
)
def test_package_rejects_consistently_rehashed_derived_artifact_tampering(
    tmp_path: Path,
    shared_external_package: tuple[
        Path, Path, Path, Path, Path, Path, ResearchPathManager
    ],
    role: str,
) -> None:
    package_path, artifact_manifest_path, dataset_path, *_ = shared_external_package
    members = _archive_members(package_path)
    package_manifest = json.loads(members["package-manifest.json"])
    row = next(item for item in package_manifest["artifacts"] if item["role"] == role)
    forged = json.loads(members[row["relative_path"]])
    if role == "LIMITATIONS":
        forged["portable_package_limitations"] = []
        forged["unverified_risks"] = []
    else:
        forged["caller_rehashed_semantic_override"] = True
    replacement = (
        json.dumps(
            forged,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    _replace_member_and_rehash_manifest(
        members,
        role=role,
        replacement=replacement,
    )
    tampered = tmp_path / f"rehashed-{role.lower()}.mrpkg"
    _write_archive(tampered, members)

    with pytest.raises(
        PortableResearchPackageError,
        match=f"portable_package_derived_artifact_mismatch:{role}",
    ):
        verify_portable_research_package(
            tampered,
            external_artifact_manifest_path=artifact_manifest_path,
            external_dataset_path=dataset_path,
        )


@pytest.mark.parametrize(
    ("logical_id", "role", "relative_path", "replacement"),
    (
        (
            "caller-added",
            "CALLER_ADDED_UNVERIFIED_EVIDENCE",
            "evidence/caller-added.json",
            b"{}\n",
        ),
        (
            "dataset-artifact",
            "IMMUTABLE_DATASET",
            "dataset/candles.sqlite",
            b"caller-added-dataset-bytes",
        ),
    ),
)
def test_package_rejects_consistently_rehashed_unexpected_role_graph_member(
    tmp_path: Path,
    shared_external_package: tuple[
        Path, Path, Path, Path, Path, Path, ResearchPathManager
    ],
    logical_id: str,
    role: str,
    relative_path: str,
    replacement: bytes,
) -> None:
    package_path, artifact_manifest_path, dataset_path, *_ = shared_external_package
    members = _archive_members(package_path)
    package_manifest = json.loads(members["package-manifest.json"])
    members[relative_path] = replacement
    package_manifest["artifacts"].append(
        {
            "logical_id": logical_id,
            "version": "1",
            "role": role,
            "relative_path": relative_path,
            "content_hash": f"sha256:{hashlib.sha256(replacement).hexdigest()}",
            "size": len(replacement),
        }
    )
    package_manifest["artifacts"].sort(
        key=lambda row: (row["relative_path"], row["logical_id"])
    )
    package_manifest["content_hash"] = sha256_prefixed(
        {
            key: value
            for key, value in package_manifest.items()
            if key != "content_hash"
        },
        label=PORTABLE_RESEARCH_PACKAGE_HASH_LABEL,
    )
    members["package-manifest.json"] = (
        json.dumps(
            package_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    tampered = tmp_path / "rehashed-extra-role.mrpkg"
    _write_archive(tampered, members)

    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_artifact_role_graph_invalid",
    ):
        verify_portable_research_package(
            tampered,
            external_artifact_manifest_path=artifact_manifest_path,
            external_dataset_path=dataset_path,
        )


def test_package_rejects_missing_tampered_secret_duplicate_role_and_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        package_path,
        artifact_manifest_path,
        dataset_path,
        result_path,
        experiment_manifest_path,
        receipt_path,
        manager,
    ) = _build_external(tmp_path)
    members = _archive_members(package_path)

    missing = tmp_path / "missing.mrpkg"
    missing_members = dict(members)
    del missing_members["evidence/limitations.json"]
    _write_archive(missing, missing_members)
    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_artifact_missing:limitations",
    ):
        verify_portable_research_package(
            missing,
            external_artifact_manifest_path=artifact_manifest_path,
            external_dataset_path=dataset_path,
        )

    tampered = tmp_path / "tampered.mrpkg"
    tampered_members = dict(members)
    tampered_members["evidence/source-result.json"] += b" "
    _write_archive(tampered, tampered_members)
    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_artifact_binding_mismatch:source-result",
    ):
        verify_portable_research_package(
            tampered,
            external_artifact_manifest_path=artifact_manifest_path,
            external_dataset_path=dataset_path,
        )

    duplicate = tmp_path / "duplicate-role.mrpkg"
    duplicate_members = dict(members)
    package_manifest = json.loads(duplicate_members["package-manifest.json"])
    package_manifest["artifacts"][1]["role"] = package_manifest["artifacts"][0]["role"]
    package_manifest["content_hash"] = sha256_prefixed(
        {
            key: value
            for key, value in package_manifest.items()
            if key != "content_hash"
        },
        label=PORTABLE_RESEARCH_PACKAGE_HASH_LABEL,
    )
    duplicate_members["package-manifest.json"] = (
        json.dumps(
            package_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    _write_archive(duplicate, duplicate_members)
    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_artifact_role_duplicate",
    ):
        verify_portable_research_package(
            duplicate,
            external_artifact_manifest_path=artifact_manifest_path,
            external_dataset_path=dataset_path,
        )

    forged_authority = tmp_path / "forged-authority.mrpkg"
    forged_members = dict(members)
    forged_manifest = json.loads(forged_members["package-manifest.json"])
    forged_manifest["publication_status"] = "AUTHORITATIVE"
    forged_manifest["content_hash"] = sha256_prefixed(
        {key: value for key, value in forged_manifest.items() if key != "content_hash"},
        label=PORTABLE_RESEARCH_PACKAGE_HASH_LABEL,
    )
    forged_members["package-manifest.json"] = (
        json.dumps(
            forged_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    _write_archive(forged_authority, forged_members)
    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_offline_authority_contract_invalid",
    ):
        verify_portable_research_package(
            forged_authority,
            external_artifact_manifest_path=artifact_manifest_path,
            external_dataset_path=dataset_path,
        )

    hardlink = tmp_path / "hardlink.mrpkg"
    os.link(package_path, hardlink)
    try:
        with pytest.raises(
            PortableResearchPackageError,
            match="portable_package_file_invalid",
        ):
            verify_portable_research_package(
                hardlink,
                external_artifact_manifest_path=artifact_manifest_path,
                external_dataset_path=dataset_path,
            )
    finally:
        hardlink.unlink()

    secret_result = tmp_path / "secret-result.json"
    source_result = json.loads(result_path.read_text(encoding="utf-8"))
    source_result["api_key"] = "must-not-enter-package"
    secret_result.write_text(json.dumps(source_result), encoding="utf-8")
    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_secret_field_forbidden:source_result.api_key",
    ):
        build_portable_research_package(
            manager=manager,
            result_path=secret_result,
            experiment_manifest_path=experiment_manifest_path,
            reproduction_receipt_path=receipt_path,
            output_path=tmp_path / "secret.mrpkg",
            dataset_mode="external_content_addressed",
        )

    source_result.pop("api_key")
    auth_token_result = dict(source_result)
    auth_token_result["auth_token"] = "opaque-credential-material-0123456789"
    auth_token_result["content_hash"] = sha256_prefixed(
        report_content_hash_payload(auth_token_result)
    )
    auth_token_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    auth_token_receipt["source_report_hash"] = auth_token_result["content_hash"]
    auth_token_receipt["receipt_content_hash"] = sha256_prefixed(
        content_hash_payload(
            {
                key: value
                for key, value in auth_token_receipt.items()
                if key != "receipt_content_hash"
            }
        ),
        label="reproduction_receipt_content",
    )
    validate_reproduction_receipt_report_binding(
        report=auth_token_result,
        receipt=auth_token_receipt,
    )
    auth_token_result_path = tmp_path / "auth-token-result.json"
    auth_token_receipt_path = tmp_path / "auth-token-receipt.json"
    auth_token_result_path.write_text(json.dumps(auth_token_result), encoding="utf-8")
    auth_token_receipt_path.write_text(json.dumps(auth_token_receipt), encoding="utf-8")
    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_secret_field_forbidden:source_result.auth_token",
    ):
        build_portable_research_package(
            manager=manager,
            result_path=auth_token_result_path,
            experiment_manifest_path=experiment_manifest_path,
            reproduction_receipt_path=auth_token_receipt_path,
            output_path=tmp_path / "auth-token.mrpkg",
            dataset_mode="external_content_addressed",
        )

    private_material_result = tmp_path / "private-material-result.json"
    source_result["research_note"] = (
        "-----BEGIN PRIVATE KEY-----\nprivate-material-must-not-enter\n"
    )
    private_material_result.write_text(json.dumps(source_result), encoding="utf-8")
    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_private_material_forbidden:source_result",
    ):
        build_portable_research_package(
            manager=manager,
            result_path=private_material_result,
            experiment_manifest_path=experiment_manifest_path,
            reproduction_receipt_path=receipt_path,
            output_path=tmp_path / "private-material.mrpkg",
            dataset_mode="external_content_addressed",
        )

    private_token = "opaque-private-token-value-0123456789"
    monkeypatch.setenv("PORTABLE_TEST_PRIVATE_TOKEN", private_token)
    source_result["research_note"] = private_token
    environment_value_result = tmp_path / "environment-value-result.json"
    environment_value_result.write_text(json.dumps(source_result), encoding="utf-8")
    with pytest.raises(
        PortableResearchPackageError,
        match=(
            "portable_package_sensitive_environment_value_forbidden:"
            "source_result:PORTABLE_TEST_PRIVATE_TOKEN"
        ),
    ):
        build_portable_research_package(
            manager=manager,
            result_path=environment_value_result,
            experiment_manifest_path=experiment_manifest_path,
            reproduction_receipt_path=receipt_path,
            output_path=tmp_path / "environment-value.mrpkg",
            dataset_mode="external_content_addressed",
        )


def test_dataset_policy_tamper_and_unverified_independent_result_fail_closed(
    tmp_path: Path,
) -> None:
    (
        package_path,
        artifact_manifest_path,
        dataset_path,
        result_path,
        experiment_manifest_path,
        receipt_path,
        manager,
    ) = _build_external(tmp_path)

    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_dataset_inclusion_not_authorized",
    ):
        build_portable_research_package(
            manager=manager,
            result_path=result_path,
            experiment_manifest_path=experiment_manifest_path,
            reproduction_receipt_path=receipt_path,
            output_path=tmp_path / "included.mrpkg",
            dataset_mode="included",
        )

    changed_dataset = tmp_path / "changed.sqlite"
    raw_dataset = bytearray(dataset_path.read_bytes())
    raw_dataset[-1] ^= 1
    changed_dataset.write_bytes(raw_dataset)
    with pytest.raises(
        PortableResearchPackageError,
        match="portable_package_dataset_verification_failed",
    ):
        verify_portable_research_package(
            package_path,
            external_artifact_manifest_path=artifact_manifest_path,
            external_dataset_path=changed_dataset,
        )

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    independent_path = tmp_path / "caller-owned-independent.json"
    independent_path.write_text(
        json.dumps(
            {
                "status": "PASS",
                "mismatches": [],
                "baseline_receipt_hash": receipt["receipt_content_hash"],
                "expected_fingerprint_hash": receipt["stable_fingerprint"][
                    "stable_fingerprint_hash"
                ],
                "actual_fingerprint_hash": receipt["stable_fingerprint"][
                    "stable_fingerprint_hash"
                ],
            }
        ),
        encoding="utf-8",
    )
    unverified_path = tmp_path / "unverified.mrpkg"
    built = build_portable_research_package(
        manager=manager,
        result_path=result_path,
        experiment_manifest_path=experiment_manifest_path,
        reproduction_receipt_path=receipt_path,
        output_path=unverified_path,
        dataset_mode="external_content_addressed",
        independent_reproduction_path=independent_path,
    )
    assert "AUTHORITATIVE" not in str(built["publication_status"])
    assert "NON_PROMOTABLE" in str(built["publication_status"])

    with pytest.raises(
        PortableResearchPackageError,
        match="portable_replay_baseline_not_created_from_installed_distribution",
    ):
        reproduce_portable_research_package(
            unverified_path,
            workspace=tmp_path / "replay",
            external_artifact_manifest_path=artifact_manifest_path,
            external_dataset_path=dataset_path,
        )
