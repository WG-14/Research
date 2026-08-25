from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import market_research.research.datasets.source_provenance as source_provenance_module
from market_research.research.dataset_freeze import (
    DatasetFreezeError,
    freeze_sqlite_candles_dataset,
)
from market_research.research.datasets.source_provenance import (
    SourceProvenanceError,
    load_transformation_trust_store,
    local_artifact_bytes_hash,
    parse_dataset_source_provenance,
    source_provenance_hash,
    transformation_receipt_hash,
    validate_source_artifact_chain,
)
from market_research.research.hashing import canonical_json_bytes
from market_research.research_cli.main import main as research_cli_main
from market_research.research_cli.context import ResearchAppContext
from tests.dataset_provenance_fixture import (
    TEST_SOURCE_PROVENANCE,
    build_bound_test_source_provenance,
    build_test_transformation_manager,
    freeze_with_test_transformation_authority,
    get_test_transformation_store,
    use_test_transformation_trust,
)


def _source(path: Path, *, start_ts: int = 1, end_ts: int = 2) -> Path:
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, "
            "open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        db.executemany(
            "INSERT INTO candles VALUES ('KRW-BTC','1m',?,?,?,?,?,?)",
            (
                (start_ts, 1.0, 1.0, 1.0, 1.0, 10.0),
                (end_ts, 2.0, 2.0, 2.0, 2.0, 20.0),
            ),
        )
    return path.resolve()


def _bound(tmp_path: Path):
    source = _source(tmp_path / "standardized.sqlite")
    return source, build_bound_test_source_provenance(
        source, template=TEST_SOURCE_PROVENANCE
    )


def _parse_rehashed(payload: dict[str, object]):
    payload["provenance_manifest_hash"] = source_provenance_hash(payload)
    return parse_dataset_source_provenance(payload)


def _rewrite_receipt(payload: dict[str, object], layer_index: int, mutation) -> None:
    lineage = payload["lineage"]
    stage = lineage[layer_index]
    receipt_path = Path(stage["transformation_receipt_uri"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    mutation(receipt)
    receipt["receipt_hash"] = transformation_receipt_hash(receipt)
    receipt_path.write_bytes(canonical_json_bytes(receipt) + b"\n")
    stage["transformation_receipt_content_hash"] = local_artifact_bytes_hash(
        receipt_path
    )


def _freeze(tmp_path: Path, source: Path, provenance):
    return freeze_with_test_transformation_authority(
        source_db=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=1,
        end_ts=2,
        out_dir=tmp_path / "frozen",
        source_provenance=provenance,
    )


def test_v3_provenance_is_rejected_without_translation() -> None:
    payload = TEST_SOURCE_PROVENANCE.as_dict()
    payload["schema_version"] = 3
    payload["provenance_manifest_hash"] = source_provenance_hash(payload)
    with pytest.raises(SourceProvenanceError, match="schema_version_unsupported"):
        parse_dataset_source_provenance(payload)


def test_manifest_only_source_rehash_cannot_rebind_physical_input(
    tmp_path: Path,
) -> None:
    source, provenance = _bound(tmp_path)
    payload = provenance.as_dict()
    source_artifact = Path(payload["sources"][0]["artifact_uri"])
    source_artifact.write_bytes(b"manually replaced provider bytes\n")
    payload["sources"][0]["content_hash"] = local_artifact_bytes_hash(source_artifact)
    rebound = _parse_rehashed(payload)

    with pytest.raises(DatasetFreezeError, match="input_chain_mismatch"):
        _freeze(tmp_path, source, rebound)


def test_manifest_only_stage_rehash_cannot_replace_receipted_output(
    tmp_path: Path,
) -> None:
    source, provenance = _bound(tmp_path)
    payload = provenance.as_dict()
    raw = Path(payload["lineage"][0]["artifact_uri"])
    raw.write_bytes(b"manually replaced raw stage\n")
    payload["lineage"][0]["content_hash"] = local_artifact_bytes_hash(raw)
    rebound = _parse_rehashed(payload)

    with pytest.raises(DatasetFreezeError, match="output_binding_mismatch"):
        _freeze(tmp_path, source, rebound)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("dataset_id", "other-dataset"),
        ("release_id", "other-release"),
        ("request_parameters", {"interval": "1m", "market": "KRW-ETH"}),
        ("requested_at", "2025-12-31T23:59:59Z"),
        ("received_at", "2026-01-01T00:00:03Z"),
        ("response_version", "other-export-v2"),
        ("acquisition_code_version", "other-acquirer-v2"),
        ("retry_count", 1),
        ("coverage_start_ts", -(2**62)),
        ("coverage_end_ts", 2**62),
    ),
)
def test_source_metadata_rehash_cannot_rebind_signed_raw_input(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    source, provenance = _bound(tmp_path)
    payload = provenance.as_dict()
    payload["sources"][0][field] = replacement
    rebound = _parse_rehashed(payload)

    with pytest.raises(DatasetFreezeError, match="input_chain_mismatch"):
        _freeze(tmp_path, source, rebound)


def test_self_consistent_receipt_rehash_cannot_declare_unmatched_code_bytes(
    tmp_path: Path,
) -> None:
    source, provenance = _bound(tmp_path)
    payload = provenance.as_dict()

    def declare_other_code(receipt: dict[str, object]) -> None:
        receipt["code_hash"] = "sha256:" + "0" * 64

    _rewrite_receipt(payload, 0, declare_other_code)
    rebound = _parse_rehashed(payload)

    with pytest.raises(DatasetFreezeError, match="signature_verification_failed"):
        _freeze(tmp_path, source, rebound)


@pytest.mark.parametrize(
    ("field", "replacement", "error"),
    (
        (
            "authority_id",
            "attacker-transform-authority",
            "authority_not_trusted",
        ),
        ("key_id", "attacker-key", "key_not_trusted"),
    ),
)
def test_receipt_authority_or_key_rebind_is_rejected_before_signature(
    tmp_path: Path,
    field: str,
    replacement: str,
    error: str,
) -> None:
    source, provenance = _bound(tmp_path)
    payload = provenance.as_dict()

    def rebind(receipt: dict[str, object]) -> None:
        receipt[field] = replacement

    _rewrite_receipt(payload, 0, rebind)
    rebound = _parse_rehashed(payload)
    with pytest.raises(DatasetFreezeError, match=error):
        _freeze(tmp_path, source, rebound)


def test_receipt_signature_tamper_and_unsigned_downgrade_are_rejected(
    tmp_path: Path,
) -> None:
    source, provenance = _bound(tmp_path)
    for mode in ("tamper", "remove"):
        payload = provenance.as_dict()

        def mutate(receipt: dict[str, object]) -> None:
            if mode == "tamper":
                receipt["signature"] = "ed25519:" + "A" * 86 + "=="
            else:
                receipt.pop("signature")

        _rewrite_receipt(payload, 0, mutate)
        rebound = _parse_rehashed(payload)
        with pytest.raises(
            DatasetFreezeError,
            match=(
                "signature_verification_failed"
                if mode == "tamper"
                else "signature_invalid"
            ),
        ):
            _freeze(tmp_path, source, rebound)


def test_standardized_replacement_with_rehashed_receipt_fails_signature(
    tmp_path: Path,
) -> None:
    source, provenance = _bound(tmp_path)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE candles SET close=999 WHERE ts=1")
    payload = provenance.as_dict()
    actual_bytes = local_artifact_bytes_hash(source)
    payload["lineage"][2]["content_hash"] = actual_bytes

    def rebind_output(receipt: dict[str, object]) -> None:
        receipt["output_artifact"]["content_hash"] = actual_bytes

    _rewrite_receipt(payload, 2, rebind_output)
    rebound = _parse_rehashed(payload)

    with pytest.raises(DatasetFreezeError, match="signature_verification_failed"):
        _freeze(tmp_path, source, rebound)


def test_lineage_schema_and_canonical_metadata_are_signed_bindings(
    tmp_path: Path,
) -> None:
    source, provenance = _bound(tmp_path)
    schema_payload = provenance.as_dict()
    schema_payload["lineage"][1]["schema_version"] = 2
    schema_rebound = _parse_rehashed(schema_payload)
    with pytest.raises(DatasetFreezeError, match="output_schema_version_mismatch"):
        _freeze(tmp_path, source, schema_rebound)

    canonical_payload = provenance.as_dict()
    canonical_payload["lineage"][2]["canonical_content_hash"] = (
        "sha256:" + "0" * 64
    )
    canonical_rebound = _parse_rehashed(canonical_payload)
    with pytest.raises(DatasetFreezeError, match="canonicalization_mismatch"):
        _freeze(tmp_path, source, canonical_rebound)


def test_valid_signature_cannot_claim_causally_impossible_transform_time(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "causal-standardized.sqlite")
    provenance = build_bound_test_source_provenance(
        source,
        template=TEST_SOURCE_PROVENANCE,
        namespace="causal-inversion",
        signed_at_by_layer={"raw": "2025-12-31T23:59:59Z"},
    )
    with pytest.raises(DatasetFreezeError, match="causal_time_inverted"):
        _freeze(tmp_path, source, provenance)


def test_revoked_or_expired_test_authority_is_rejected(
    tmp_path: Path,
) -> None:
    source, provenance = _bound(tmp_path)
    store = get_test_transformation_store(provenance)
    manager = build_test_transformation_manager(provenance)
    revoked_key = replace(
        store.keys[0],
        revoked_at="2026-02-01T00:00:00Z",
        revocation_reason="test compromise",
    )
    variants = (
        (replace(store, keys=(revoked_key,)), "key_revoked"),
        (replace(store, expires_at="2026-02-01T00:00:00Z"), "store_expired"),
        (
            replace(
                store,
                keys=(replace(store.keys[0], valid_from="2027-01-01T00:00:00Z"),),
            ),
            "outside_key_validity",
        ),
        (replace(store, issued_at="2027-01-01T00:00:00Z"), "store_not_yet_valid"),
    )
    for authority, error in variants:
        with patch(
            "market_research.research.datasets.source_provenance."
            "load_transformation_trust_store",
            return_value=authority,
        ):
            with pytest.raises(DatasetFreezeError, match=error):
                freeze_sqlite_candles_dataset(
                    source_db=source,
                    market="KRW-BTC",
                    interval="1m",
                    start_ts=1,
                    end_ts=2,
                    out_dir=tmp_path / f"frozen-{error}",
                    source_provenance=provenance,
                    manager=manager,
                )


def test_production_api_rejects_missing_or_self_consistent_attacker_trust_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, provenance = _bound(tmp_path)
    kwargs = {
        "source_db": source,
        "market": "KRW-BTC",
        "interval": "1m",
        "start_ts": 1,
        "end_ts": 2,
        "out_dir": tmp_path / "frozen-production-trust",
        "source_provenance": provenance,
    }
    with pytest.raises(
        DatasetFreezeError, match="transformation_trust_manager_required"
    ):
        freeze_sqlite_candles_dataset(**kwargs)

    manager = build_test_transformation_manager(provenance)
    missing = replace(
        manager.settings,
        dataset_transformation_trust_store_path=None,
        dataset_transformation_trust_store_hash=None,
    )
    missing_manager = type(manager).from_settings(
        missing,
        project_root=manager.project_root,
    )
    with pytest.raises(DatasetFreezeError, match="trust_configuration_required"):
        freeze_sqlite_candles_dataset(**kwargs, manager=missing_manager)

    monkeypatch.delenv("RESEARCH_RUNTIME_PROFILE", raising=False)
    with pytest.raises(DatasetFreezeError, match="operated_profile_required"):
        freeze_sqlite_candles_dataset(**kwargs, manager=manager)

    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")
    with pytest.raises(DatasetFreezeError, match="administrator_owner_required"):
        freeze_sqlite_candles_dataset(**kwargs, manager=manager)

    # The fixture is a complete, self-consistent chain signed by a newly
    # generated attacker-controlled key.  Rehashing every manifest and receipt
    # still cannot turn its caller-owned store into the operated trust anchor.
    receipt_key_ids = {
        json.loads(Path(stage.transformation_receipt_uri).read_text())["key_id"]
        for stage in provenance.lineage
    }
    assert receipt_key_ids == {get_test_transformation_store(provenance).keys[0].key_id}


def test_production_trust_store_rejects_symlink_and_writable_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_path, provenance = _bound(tmp_path)
    manager = build_test_transformation_manager(provenance)
    trust_path = manager.settings.dataset_transformation_trust_store_path
    assert trust_path is not None
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")

    trust_path.chmod(0o664)
    with pytest.raises(SourceProvenanceError, match="permissions_too_open"):
        load_transformation_trust_store(manager=manager)
    trust_path.chmod(0o644)

    link = trust_path.parent / "caller-selected-trust-link.json"
    link.symlink_to(trust_path)
    linked_settings = replace(
        manager.settings,
        dataset_transformation_trust_store_path=link,
    )
    linked_manager = type(manager).from_settings(
        linked_settings,
        project_root=manager.project_root,
    )
    with pytest.raises(SourceProvenanceError, match="symlink_rejected"):
        load_transformation_trust_store(manager=linked_manager)


def test_production_public_key_rejects_symlink_and_writable_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_path, provenance = _bound(tmp_path)
    manager = build_test_transformation_manager(provenance)
    store = get_test_transformation_store(provenance)
    trust_path = manager.settings.dataset_transformation_trust_store_path
    assert trust_path is not None
    trust_bytes = trust_path.read_bytes()
    public_key_path = Path(store.keys[0].public_key_path)
    original_loader = source_provenance_module._load_admin_pinned_file
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")

    def allow_test_store_only(path, **kwargs):
        if kwargs["label"] == "transformation_trust_store":
            return trust_bytes
        return original_loader(path, **kwargs)

    with patch.object(
        source_provenance_module,
        "_load_admin_pinned_file",
        side_effect=allow_test_store_only,
    ):
        public_key_path.chmod(0o664)
        with pytest.raises(SourceProvenanceError, match="permissions_too_open"):
            load_transformation_trust_store(manager=manager)
        public_key_path.chmod(0o644)

        target = public_key_path.with_suffix(".real")
        public_key_path.rename(target)
        public_key_path.symlink_to(target)
        try:
            with pytest.raises(SourceProvenanceError, match="symlink_rejected"):
                load_transformation_trust_store(manager=manager)
        finally:
            public_key_path.unlink()
            target.rename(public_key_path)


def test_identical_chain_can_be_explicitly_relocated_without_receipt_rewrite(
    tmp_path: Path,
) -> None:
    source, provenance = _bound(tmp_path)
    payload = deepcopy(provenance.as_dict())
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    path_fields = (
        "artifact_uri",
        "transformation_receipt_uri",
        "code_artifact_uri",
        "config_artifact_uri",
    )
    old_to_new: dict[str, str] = {}
    source_uri = payload["sources"][0]["artifact_uri"]
    source_copy = relocated / "source.bin"
    shutil.copyfile(source_uri, source_copy)
    payload["sources"][0]["artifact_uri"] = str(source_copy)
    for stage in payload["lineage"]:
        for field in path_fields:
            old = stage[field]
            if old not in old_to_new:
                target = relocated / f"{len(old_to_new)}-{Path(old).name}"
                shutil.copyfile(old, target)
                old_to_new[old] = str(target)
            stage[field] = old_to_new[old]
    restored = _parse_rehashed(payload)
    restored_source = Path(restored.lineage[-1].artifact_uri)

    assert restored.provenance_manifest_hash != provenance.provenance_manifest_hash
    with use_test_transformation_trust(provenance) as manager:
        assert (
            validate_source_artifact_chain(restored, manager=manager)
            == restored_source
        )
    result = _freeze(tmp_path, restored_source, restored)
    assert result["row_count"] == 2
    assert Path(source).read_bytes() == restored_source.read_bytes()


def test_symlinked_stage_locator_is_rejected_before_sqlite_read(tmp_path: Path) -> None:
    source, provenance = _bound(tmp_path)
    payload = provenance.as_dict()
    raw = Path(payload["lineage"][0]["artifact_uri"])
    link = raw.parent / "raw-link.bin"
    link.symlink_to(raw)
    payload["lineage"][0]["artifact_uri"] = str(link)
    rebound = _parse_rehashed(payload)

    with pytest.raises(DatasetFreezeError, match="symlink_rejected"):
        _freeze(tmp_path, source, rebound)


def test_receipt_swap_to_symlink_between_resolution_and_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, provenance = _bound(tmp_path)
    receipt = Path(provenance.lineage[0].transformation_receipt_uri)
    target = receipt.with_suffix(".real.json")
    original_resolver = source_provenance_module._resolve_external_local_artifact
    swapped = False

    def swap_after_resolution(*args, **kwargs):
        nonlocal swapped
        resolved = original_resolver(*args, **kwargs)
        if kwargs["label"] == "raw_transformation_receipt" and not swapped:
            receipt.rename(target)
            receipt.symlink_to(target)
            swapped = True
        return resolved

    monkeypatch.setattr(
        source_provenance_module,
        "_resolve_external_local_artifact",
        swap_after_resolution,
    )
    try:
        with pytest.raises(DatasetFreezeError, match="unavailable"):
            _freeze(tmp_path, source, provenance)
    finally:
        if receipt.is_symlink():
            receipt.unlink()
        if target.exists():
            target.rename(receipt)


def test_official_cli_freeze_path_enforces_v4_physical_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    day_start = 1_767_225_600_000
    day_end = 1_767_311_999_999
    source = _source(
        tmp_path / "cli-standardized.sqlite",
        start_ts=day_start,
        end_ts=day_end,
    )
    provenance = build_bound_test_source_provenance(
        source, template=TEST_SOURCE_PROVENANCE
    )
    manifest = tmp_path / "dataset-source-provenance.json"
    manifest.write_bytes(canonical_json_bytes(provenance.as_dict()) + b"\n")

    argv = [
        "research-freeze-dataset",
        "--db",
        str(source),
        "--market",
        "KRW-BTC",
        "--interval",
        "1m",
        "--start",
        "2026-01-01",
        "--end",
        "2026-01-01",
        "--out",
        str(tmp_path / "cli-frozen"),
        "--provenance-manifest",
        str(manifest),
    ]
    with use_test_transformation_trust(provenance) as manager:
        context = ResearchAppContext(settings=manager.settings, paths=manager)
        assert research_cli_main(argv, context=context) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["row_count"] == 2
    assert result["source_provenance"]["schema_version"] == 4


def test_official_cli_freeze_fails_closed_without_operated_trust(
    tmp_path: Path,
) -> None:
    source, provenance = _bound(tmp_path)
    manifest = tmp_path / "untrusted-cli-provenance.json"
    manifest.write_bytes(canonical_json_bytes(provenance.as_dict()) + b"\n")
    manager = build_test_transformation_manager(provenance)
    untrusted_settings = replace(
        manager.settings,
        dataset_transformation_trust_store_path=None,
        dataset_transformation_trust_store_hash=None,
    )
    untrusted_manager = type(manager).from_settings(
        untrusted_settings,
        project_root=manager.project_root,
    )
    context = ResearchAppContext(
        settings=untrusted_settings,
        paths=untrusted_manager,
    )
    with pytest.raises(DatasetFreezeError, match="trust_configuration_required"):
        research_cli_main(
            [
                "research-freeze-dataset",
                "--db",
                str(source),
                "--market",
                "KRW-BTC",
                "--interval",
                "1m",
                "--start",
                "1970-01-01",
                "--end",
                "1970-01-01",
                "--out",
                str(tmp_path / "untrusted-cli-output"),
                "--provenance-manifest",
                str(manifest),
            ],
            context=context,
        )
