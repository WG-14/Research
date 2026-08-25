from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from market_research.paths import ResearchPathManager
from market_research.research.datasets.hashing_contract import artifact_content_hash
from market_research.research.datasets.source_catalog import (
    SourceCatalog,
    build_source_catalog,
)
from market_research.research.datasets.source_provenance import (
    STANDARDIZED_CANONICALIZATION_ID,
    DatasetSourceProvenance,
    TransformationTrustKey,
    TransformationTrustStore,
    build_dataset_source_provenance,
    build_transformation_receipt,
    local_artifact_bytes_hash,
    source_record_artifact_id,
    transformation_receipt_bytes,
)
from market_research.research.hashing import canonical_json_bytes
from market_research.settings import ResearchSettings


_TEST_TRANSFORMATION_AUTHORITY_ID = "test-only-dataset-transform-authority"
_TEST_TRANSFORMATION_STORES: dict[str, TransformationTrustStore] = {}
_TEST_TRANSFORMATION_STORES_BY_ROOT: dict[Path, TransformationTrustStore] = {}
_TEST_TRANSFORMATION_CONFIG: dict[str, tuple[Path, str]] = {}
_BOUND_PROVENANCE_CACHE: dict[tuple[str, ...], DatasetSourceProvenance] = {}


def build_test_source_catalog(
    *,
    provider_id: str = "test-provider",
    source_kinds: tuple[str, ...] = ("file_export",),
) -> SourceCatalog:
    return build_source_catalog(
        catalog_id="test-research-source-catalog",
        version="test-v1",
        approved_at="2025-12-31T00:00:00Z",
        approved_by="test-data-steward",
        entries=(
            {
                "provider_id": provider_id,
                "display_name": "Externally prepared test fixture",
                "data_kinds": ["ohlcv"],
                "frequencies": ["1m"],
                "source_kinds": sorted(source_kinds),
                "point_in_time_policy": ("event_available_received_processed_times"),
                "revision_policy": "append_new_release_preserve_prior",
                "license_id": "test-research-license-v1",
                "research_use_terms": "offline reproducible research only",
                "redistribution_allowed": False,
                "quality_level": "VERIFIED",
                "preparation_boundary": (
                    "externally_prepared_offline_immutable_input_only"
                ),
                "credential_boundary": (
                    "credentials_external_to_research_distribution"
                ),
                "owner": "test-data-steward",
                "expected_delivery_lag_seconds": 1.0,
                "maximum_staleness_seconds": 3600.0,
            },
        ),
    )


TEST_SOURCE_CATALOG = build_test_source_catalog()

TEST_SOURCE_PROVENANCE = build_dataset_source_provenance(
    source_catalog=TEST_SOURCE_CATALOG,
    sources=(
        {
            "provider_id": "test-provider",
            "dataset_id": "test-candles",
            "release_id": "test-release-v1",
            "source_kind": "file_export",
            "request_parameters": {
                "interval": "1m",
                "market": "KRW-BTC",
            },
            "requested_at": "2026-01-01T00:00:00Z",
            "received_at": "2026-01-01T00:00:01Z",
            "response_version": "test-export-v1",
            "acquisition_code_version": "external-fixture-v1",
            "retry_count": 0,
            "acquisition_status": "complete",
            "error_code": "",
            "coverage_start_ts": -(2**63),
            "coverage_end_ts": 2**63 - 1,
            "artifact_uri": "/tmp/market-research-test/source.bin",
            "content_hash": "sha256:" + "1" * 64,
        },
    ),
    source_priority=("test-provider",),
    lineage=(
        {
            "layer": "raw",
            "artifact_id": "test-raw-v1",
            "artifact_uri": "/tmp/market-research-test/raw.bin",
            "content_hash": "sha256:" + "2" * 64,
            "schema_version": 1,
            "transformation_id": "external-acquisition-v1",
            "transformation_receipt_uri": "/tmp/market-research-test/raw.receipt.json",
            "transformation_receipt_content_hash": "sha256:" + "5" * 64,
            "code_artifact_uri": "/tmp/market-research-test/raw.code",
            "config_artifact_uri": "/tmp/market-research-test/raw.config",
        },
        {
            "layer": "cleaned",
            "artifact_id": "test-cleaned-v1",
            "artifact_uri": "/tmp/market-research-test/cleaned.bin",
            "content_hash": "sha256:" + "3" * 64,
            "schema_version": 1,
            "transformation_id": "test-cleaner-v1",
            "transformation_receipt_uri": "/tmp/market-research-test/cleaned.receipt.json",
            "transformation_receipt_content_hash": "sha256:" + "6" * 64,
            "code_artifact_uri": "/tmp/market-research-test/cleaned.code",
            "config_artifact_uri": "/tmp/market-research-test/cleaned.config",
        },
        {
            "layer": "standardized",
            "artifact_id": "test-standardized-v1",
            "artifact_uri": "/tmp/market-research-test/standardized.sqlite",
            "content_hash": "sha256:" + "4" * 64,
            "schema_version": 1,
            "transformation_id": "test-standardizer-v1",
            "transformation_receipt_uri": "/tmp/market-research-test/standardized.receipt.json",
            "transformation_receipt_content_hash": "sha256:" + "7" * 64,
            "code_artifact_uri": "/tmp/market-research-test/standardized.code",
            "config_artifact_uri": "/tmp/market-research-test/standardized.config",
            "canonicalization_id": STANDARDIZED_CANONICALIZATION_ID,
            "canonical_content_hash": "sha256:" + "8" * 64,
        },
    ),
)


def build_bound_test_source_provenance(
    source_db: str | Path,
    *,
    template: DatasetSourceProvenance | None = None,
    provider_id: str = "test-provider",
    source_catalog: SourceCatalog | None = None,
    namespace: str | None = None,
    signed_at_by_layer: dict[str, str] | None = None,
) -> DatasetSourceProvenance:
    """Build real repository-external files and a verifiable v4 receipt chain."""
    if template is not None:
        if len(template.sources) != 1:
            raise ValueError("test_provenance_binder_requires_one_source")
        provider_id = template.sources[0].provider_id
        source_catalog = template.source_catalog
    standardized = Path(source_db).resolve()
    effective_template = template or TEST_SOURCE_PROVENANCE
    effective_catalog = source_catalog or (
        TEST_SOURCE_CATALOG
        if provider_id == "test-provider"
        else build_test_source_catalog(provider_id=provider_id)
    )
    cache_key = (
        str(standardized),
        local_artifact_bytes_hash(standardized),
        effective_template.provenance_manifest_hash,
        effective_catalog.catalog_hash,
        provider_id,
        namespace or "",
        json.dumps(signed_at_by_layer or {}, sort_keys=True),
    )
    cached = _BOUND_PROVENANCE_CACHE.get(cache_key)
    if cached is not None and _bound_test_files_exist(cached):
        return cached
    _BOUND_PROVENANCE_CACHE.pop(cache_key, None)
    root = standardized.parent / (
        f".{standardized.stem}-provenance-{namespace or provider_id}"
    )
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    source_artifact = root / "provider-source.bin"
    raw_artifact = root / "raw.bin"
    cleaned_artifact = root / "cleaned.bin"
    source_artifact.write_bytes(b"externally prepared immutable source fixture\n")
    raw_artifact.write_bytes(b"raw stage fixture\n")
    cleaned_artifact.write_bytes(b"cleaned stage fixture\n")

    source_hash = local_artifact_bytes_hash(source_artifact)
    catalog = effective_catalog
    source_template_record = (
        template.sources[0]
        if template is not None
        else TEST_SOURCE_PROVENANCE.sources[0]
    )
    source_record = replace(
        source_template_record,
        provider_id=provider_id,
        artifact_uri=str(source_artifact),
        content_hash=source_hash,
    )
    default_signed_at = (
        datetime.fromisoformat(source_record.received_at.replace("Z", "+00:00"))
        + timedelta(seconds=1)
    ).isoformat().replace("+00:00", "Z")

    # This issuer is intentionally test-only.  Tests inject the resulting
    # in-memory public authority while exercising production signature logic;
    # production trust loading still requires an operated, root-owned store.
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    public_key_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = "test-key-" + hashlib.sha256(public_key_raw).hexdigest()[:24]
    public_key_path = root / f"{key_id}.ed25519.pub"
    public_key_path.write_bytes(
        b"ed25519:" + base64.b64encode(public_key_raw) + b"\n"
    )
    public_key_path.chmod(0o644)
    public_key_hash = local_artifact_bytes_hash(public_key_path)
    trust_payload = {
        "schema_version": 1,
        "artifact_type": "dataset_transformation_trust_store",
        "authority_id": _TEST_TRANSFORMATION_AUTHORITY_ID,
        "issued_at": "2020-01-01T00:00:00Z",
        "expires_at": "2099-12-31T23:59:59Z",
        "keys": [
            {
                "key_id": key_id,
                "algorithm": "ed25519",
                "public_key_path": str(public_key_path),
                "public_key_content_hash": public_key_hash,
                "valid_from": "2020-01-01T00:00:00Z",
                "valid_until": "2099-12-31T23:59:59Z",
                "revoked_at": None,
                "revocation_reason": "",
            }
        ],
    }
    trust_store_path = root / "test-only-transformation-trust-store.json"
    trust_store_path.write_bytes(canonical_json_bytes(trust_payload) + b"\n")
    trust_store_path.chmod(0o644)
    trust_store_hash = local_artifact_bytes_hash(trust_store_path)
    trust_store = TransformationTrustStore(
        authority_id=_TEST_TRANSFORMATION_AUTHORITY_ID,
        issued_at="2020-01-01T00:00:00Z",
        expires_at="2099-12-31T23:59:59Z",
        keys=(
            TransformationTrustKey(
                key_id=key_id,
                public_key_path=str(public_key_path),
                public_key_content_hash=public_key_hash,
                valid_from="2020-01-01T00:00:00Z",
                valid_until="2099-12-31T23:59:59Z",
                revoked_at=None,
                revocation_reason="",
                public_key=public_key,
            ),
        ),
        content_hash=trust_store_hash,
    )
    _TEST_TRANSFORMATION_STORES[key_id] = trust_store
    _TEST_TRANSFORMATION_STORES_BY_ROOT[root.resolve()] = trust_store
    _TEST_TRANSFORMATION_CONFIG[key_id] = (trust_store_path, trust_store_hash)
    template_stages = (
        template.lineage if template is not None else TEST_SOURCE_PROVENANCE.lineage
    )
    stage_bindings = tuple(
        (
            stage.layer,
            standardized
            if stage.layer == "standardized"
            else raw_artifact
            if stage.layer == "raw"
            else cleaned_artifact,
            stage.transformation_id,
            stage.artifact_id,
            stage.schema_version,
        )
        for stage in template_stages
    )
    previous = (
        {
            "artifact_id": source_record_artifact_id(
                source_record,
                source_catalog_hash=catalog.catalog_hash,
            ),
            "content_hash": source_hash,
        },
    )
    lineage: list[dict[str, object]] = []
    for (
        layer,
        artifact,
        transformation_id,
        artifact_id,
        schema_version,
    ) in stage_bindings:
        output = {
            "artifact_id": artifact_id,
            "content_hash": local_artifact_bytes_hash(artifact),
        }
        code_path = root / f"{layer}.code"
        config_path = root / f"{layer}.config.json"
        code_path.write_bytes(f"test code: {transformation_id}\n".encode())
        config_path.write_bytes(
            (f'{{"transformation_id":"{transformation_id}"}}\n').encode()
        )
        canonicalization_id: str | None = None
        canonical_content_hash: str | None = None
        if layer == "standardized":
            with sqlite3.connect(f"file:{standardized}?mode=ro", uri=True) as db:
                rows = db.execute(
                    "SELECT pair, interval, ts, open, high, low, close, volume "
                    "FROM candles ORDER BY pair, interval, ts"
                ).fetchall()
            canonicalization_id = STANDARDIZED_CANONICALIZATION_ID
            canonical_content_hash = artifact_content_hash(rows)
        receipt = build_transformation_receipt(
            layer=layer,
            transformation_id=transformation_id,
            input_artifacts=previous,
            output_artifact=output,
            authority_id=_TEST_TRANSFORMATION_AUTHORITY_ID,
            key_id=key_id,
            signed_at=(signed_at_by_layer or {}).get(
                layer, default_signed_at
            ),
            code_artifact_id=f"{transformation_id}:code",
            code_hash=local_artifact_bytes_hash(code_path),
            config_artifact_id=f"{transformation_id}:config",
            config_hash=local_artifact_bytes_hash(config_path),
            output_schema_version=schema_version,
            output_canonicalization_id=canonicalization_id,
            output_canonical_content_hash=canonical_content_hash,
            private_key=private_key,
        )
        receipt_path = root / f"{layer}.receipt.json"
        receipt_path.write_bytes(transformation_receipt_bytes(receipt))
        stage: dict[str, object] = {
            "layer": layer,
            "artifact_id": artifact_id,
            "artifact_uri": str(artifact),
            "content_hash": output["content_hash"],
            "schema_version": schema_version,
            "transformation_id": transformation_id,
            "transformation_receipt_uri": str(receipt_path),
            "transformation_receipt_content_hash": local_artifact_bytes_hash(
                receipt_path
            ),
            "code_artifact_uri": str(code_path),
            "config_artifact_uri": str(config_path),
        }
        if layer == "standardized":
            stage.update(
                {
                    "canonicalization_id": canonicalization_id,
                    "canonical_content_hash": canonical_content_hash,
                }
            )
        lineage.append(stage)
        previous = (output,)

    provenance = build_dataset_source_provenance(
        source_catalog=catalog,
        sources=(source_record.as_dict(),),
        source_priority=(provider_id,),
        lineage=lineage,
    )
    _BOUND_PROVENANCE_CACHE[cache_key] = provenance
    return provenance


def _bound_test_files_exist(provenance: DatasetSourceProvenance) -> bool:
    paths = [Path(source.artifact_uri) for source in provenance.sources]
    for stage in provenance.lineage:
        paths.extend(
            (
                Path(stage.artifact_uri),
                Path(stage.transformation_receipt_uri),
                Path(stage.code_artifact_uri),
                Path(stage.config_artifact_uri),
            )
        )
    return all(path.is_file() and not path.is_symlink() for path in paths)


def freeze_bound_test_dataset(**kwargs):
    """Invoke the production freezer with physical v4 provenance test inputs."""
    source_db = kwargs["source_db"]
    template = kwargs["source_provenance"]
    kwargs["source_provenance"] = build_bound_test_source_provenance(
        source_db, template=template
    )
    return freeze_with_test_transformation_authority(**kwargs)


def get_test_transformation_store(
    provenance: DatasetSourceProvenance,
) -> TransformationTrustStore:
    receipt_path = Path(provenance.lineage[0].transformation_receipt_uri)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    key_id = str(receipt["key_id"])
    try:
        return _TEST_TRANSFORMATION_STORES[key_id]
    except KeyError as exc:
        try:
            return _TEST_TRANSFORMATION_STORES_BY_ROOT[receipt_path.parent.resolve()]
        except KeyError:
            try:
                return _load_test_store_from_fixture_directory(
                    receipt_path.parent,
                    key_id=key_id,
                )
            except (OSError, KeyError, ValueError) as load_exc:
                raise ValueError(
                    "test_transformation_authority_not_provisioned"
                ) from (load_exc or exc)


def _load_test_store_from_fixture_directory(
    root: Path,
    *,
    key_id: str,
) -> TransformationTrustStore:
    trust_path = root / "test-only-transformation-trust-store.json"
    payload = json.loads(trust_path.read_text(encoding="utf-8"))
    entries = [item for item in payload["keys"] if item["key_id"] == key_id]
    if len(entries) != 1:
        raise ValueError("test_transformation_key_not_found")
    entry = entries[0]
    public_key_path = Path(entry["public_key_path"])
    raw = public_key_path.read_bytes()
    if local_artifact_bytes_hash(public_key_path) != entry["public_key_content_hash"]:
        raise ValueError("test_transformation_public_key_hash_mismatch")
    public_key_bytes = base64.b64decode(
        raw.removeprefix(b"ed25519:").removesuffix(b"\n"), validate=True
    )
    public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    key = TransformationTrustKey(
        key_id=key_id,
        public_key_path=str(public_key_path),
        public_key_content_hash=str(entry["public_key_content_hash"]),
        valid_from=str(entry["valid_from"]),
        valid_until=str(entry["valid_until"]),
        revoked_at=(
            None if entry["revoked_at"] is None else str(entry["revoked_at"])
        ),
        revocation_reason=str(entry["revocation_reason"]),
        public_key=public_key,
    )
    store = TransformationTrustStore(
        authority_id=str(payload["authority_id"]),
        issued_at=str(payload["issued_at"]),
        expires_at=str(payload["expires_at"]),
        keys=(key,),
        content_hash=local_artifact_bytes_hash(trust_path),
    )
    _TEST_TRANSFORMATION_STORES[key_id] = store
    _TEST_TRANSFORMATION_STORES_BY_ROOT[root.resolve()] = store
    _TEST_TRANSFORMATION_CONFIG[key_id] = (
        trust_path,
        store.content_hash,
    )
    return store


def build_test_transformation_manager(
    provenance: DatasetSourceProvenance,
) -> ResearchPathManager:
    store = get_test_transformation_store(provenance)
    key_id = store.keys[0].key_id
    trust_path, trust_hash = _TEST_TRANSFORMATION_CONFIG[key_id]
    root = trust_path.parent / "test-only-runtime-state"
    settings = ResearchSettings(
        data_root=(root / "datasets").resolve(),
        artifact_root=(root / "artifacts").resolve(),
        report_root=(root / "reports").resolve(),
        cache_root=(root / "cache").resolve(),
        db_path=None,
        max_workers=1,
        random_seed=0,
        dataset_transformation_trust_store_path=trust_path.resolve(),
        dataset_transformation_trust_store_hash=trust_hash,
    )
    return ResearchPathManager.from_settings(
        settings,
        project_root=Path(__file__).resolve().parents[1],
    )


@contextmanager
def use_test_transformation_trust(
    provenance: DatasetSourceProvenance,
) -> Iterator[ResearchPathManager]:
    """Inject a test public authority without weakening production loading."""

    store = get_test_transformation_store(provenance)
    manager = build_test_transformation_manager(provenance)
    with patch(
        "market_research.research.datasets.source_provenance."
        "load_transformation_trust_store",
        return_value=store,
    ):
        yield manager


def freeze_with_test_transformation_authority(**kwargs):
    """Run production freeze logic with an explicitly test-only trust issuer."""

    from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset

    provenance = kwargs["source_provenance"]
    with use_test_transformation_trust(provenance) as manager:
        kwargs["manager"] = manager
        return freeze_sqlite_candles_dataset(**kwargs)
