from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.authoritative_inputs import (
    AuthoritativeInputFactory,
    AuthoritativeInputReceipt,
    AuthoritativeOutputBinding,
)
from market_research.research.multi_asset.evidence import evidence_hash
from market_research.research.multi_asset.public_execution_evidence import (
    PublicExecutionEvidenceBundle,
    PublicExecutionEvidenceError,
    build_public_execution_evidence_bundle,
    publish_public_execution_evidence_bundle,
)
from market_research.research.multi_asset.public_integrated_profile import (
    PublicIntegratedProfileReceipt,
    build_public_t04_fixture_inputs,
    run_public_integrated_profile,
)
from market_research.research.multi_asset.public_option_profile import (
    PublicOptionInstitutionalReceipt,
    build_public_t03_fixture_inputs,
    default_public_option_institutional_factory,
    run_public_option_profile,
)
from market_research.research.multi_asset.public_spot_futures_profile import (
    PublicFuturesProfileReceipt,
    PublicSpotProfileReceipt,
    build_public_t01_inputs,
    build_public_t02_inputs,
    run_public_t01_spot_profile,
    run_public_t02_futures_profile,
)
from market_research.research.multi_asset.research_package import (
    EvidenceArtifactRef,
    EvidenceArtifactRole,
    ResolvedEvidenceArtifact,
    bytes_sha256,
    evidence_artifact_schema_hash,
    research_input_document_hash,
    research_input_source_row_hash,
    research_input_source_rows_hash,
)
from market_research.settings import ResearchSettings


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_SPEC_HASH = evidence_hash(
    {"experiment_id": "experiment.public.execution.evidence"},
    label="public-execution-evidence-test-spec",
)


def _hash(label: str) -> str:
    return evidence_hash(
        {"label": label},
        label="public-execution-evidence-focused-test",
    )


def _input_document() -> dict[str, object]:
    return {
        "T-01": {"fixture_id": "public-spot"},
        "T-02": {"fixture_id": "public-futures"},
        "T-03": {"fixture_id": "public-option"},
        "T-04": {"fixture_id": "public-integrated"},
    }


def _authoritative_input_receipt(
    *,
    artifact_variant: str = "primary",
) -> AuthoritativeInputReceipt:
    document = _input_document()
    row: dict[str, object] = {
        "row_id": "row:public:execution-evidence",
        "row_kind": "CANONICAL_RESEARCH_INPUTS",
        "event_at": "2026-01-01T00:00:00Z",
        "knowledge_at": "2026-01-01T00:00:01Z",
        "source_id": "externally-prepared-fixture",
        "source_schema_version": "v1",
        "payload": document,
    }
    row["content_hash"] = research_input_source_row_hash(row)
    rows = [row]
    payload = {
        "artifact_kind": "IMMUTABLE_RESEARCH_INPUTS",
        "input_schema_id": "public-execution-evidence-inputs",
        "input_schema_version": 1,
        "input_document": document,
        "input_document_hash": research_input_document_hash(document),
        "source_rows": rows,
        "source_rows_hash": research_input_source_rows_hash(rows),
    }
    artifact_bytes = f"immutable-input-artifact:{artifact_variant}".encode()
    resolved = ResolvedEvidenceArtifact(
        reference=EvidenceArtifactRef(
            role=EvidenceArtifactRole.RESEARCH_INPUTS,
            logical_id="research-inputs:public-execution-evidence",
            version="v1",
            uri=(
                f"file:///tmp/public-execution-evidence-inputs-{artifact_variant}.json"
            ),
            content_hash=bytes_sha256(artifact_bytes),
            schema_hash=evidence_artifact_schema_hash(),
            byte_length=len(artifact_bytes),
        ),
        quality_flags=("EXTERNALLY_PREPARED_FIXTURE",),
        payload_json=json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
        verified_at="2026-01-01T00:00:02Z",
    )
    return AuthoritativeInputFactory(
        input_schema_id="public-execution-evidence-inputs",
        input_schema_version=1,
    ).resolve(
        (resolved,),
        input_document=document,
        decision_cutoff="2026-01-01T00:01:00Z",
    )


def _spot_receipt() -> PublicSpotProfileReceipt:
    return run_public_t01_spot_profile(
        build_public_t01_inputs(
            source_document_id="document.public.execution.t01",
            source_document_hashes=(_hash("T-01"),),
            observation_at="2026-01-02T10:00:00Z",
            valuation_at="2026-01-20T10:00:00Z",
            knowledge_at="2026-01-20T10:00:00Z",
            instrument_id="instrument.public.execution.spot",
            currency="USD",
            entry_price=Decimal("100"),
            quantity=Decimal("100"),
        )
    )


def _futures_receipt() -> PublicFuturesProfileReceipt:
    return run_public_t02_futures_profile(
        build_public_t02_inputs(
            source_document_id="document.public.t02",
            source_document_hashes=(_hash("T-02"),),
            observation_at="2026-01-01T10:00:00Z",
            valuation_at="2026-01-03T10:00:00Z",
            knowledge_at="2026-01-03T09:00:00Z",
            underlying_instrument_id="instrument.public.index",
            root_id="root.public.index",
            near_contract_id="future.public.near",
            selected_contract_id="future.public.selected",
            currency="USD",
            entry_price=Decimal("100"),
            final_price=Decimal("105"),
            multiplier=Decimal("50"),
            quantity=Decimal("2"),
        )
    )


def _option_receipt() -> PublicOptionInstitutionalReceipt:
    return run_public_option_profile(
        receipt_id="receipt.public.execution.t03",
        inputs=build_public_t03_fixture_inputs(
            source_document_id="document.public.execution.t03",
            source_document_hashes=(_hash("T-03"),),
            observation_at="2026-01-02T12:00:00Z",
            knowledge_at="2026-01-02T12:00:02Z",
            valuation_at="2026-01-02T12:00:10Z",
        ),
        factory=default_public_option_institutional_factory(),
    )


def _integrated_receipt() -> PublicIntegratedProfileReceipt:
    return run_public_integrated_profile(
        build_public_t04_fixture_inputs(
            source_document_id="document.public.execution.t04",
            source_document_hashes=(
                sha256_prefixed(
                    "document",
                    label="public-execution-evidence-manual",
                ),
            ),
            opened_at="2026-01-02T12:00:00Z",
            closed_at="2026-01-02T12:10:00Z",
        )
    )


@dataclass(frozen=True, slots=True)
class _Materials:
    authoritative_input_receipt: AuthoritativeInputReceipt
    spot_profile_receipt: PublicSpotProfileReceipt
    futures_profile_receipt: PublicFuturesProfileReceipt
    option_profile_receipt: PublicOptionInstitutionalReceipt
    integrated_profile_receipt: PublicIntegratedProfileReceipt
    output_bindings: tuple[AuthoritativeOutputBinding, ...]
    bundle: PublicExecutionEvidenceBundle


@pytest.fixture(scope="module")
def materials() -> _Materials:
    authoritative = _authoritative_input_receipt()
    spot = _spot_receipt()
    futures = _futures_receipt()
    option = _option_receipt()
    integrated = _integrated_receipt()
    bindings = (
        authoritative.bind_output(
            output_path="/T-01/institutional_receipt",
            output_value=spot.as_dict(),
            input_paths=("/T-01",),
            computation_hash=spot.content_hash,
        ),
        authoritative.bind_output(
            output_path="/T-02/institutional_receipt",
            output_value=futures.as_dict(),
            input_paths=("/T-02",),
            computation_hash=futures.content_hash,
        ),
        authoritative.bind_output(
            output_path="/T-03/institutional_receipt",
            output_value=option.as_dict(),
            input_paths=("/T-03",),
            computation_hash=option.content_hash,
        ),
        authoritative.bind_output(
            output_path="/T-04/institutional_receipt",
            output_value=integrated.as_dict(),
            input_paths=("/T-04",),
            computation_hash=integrated.content_hash,
        ),
    )
    bundle = build_public_execution_evidence_bundle(
        experiment_spec_hash=EXPERIMENT_SPEC_HASH,
        authoritative_input_receipt=authoritative,
        spot_profile_receipt=spot,
        futures_profile_receipt=futures,
        option_profile_receipt=option,
        integrated_profile_receipt=integrated,
        output_bindings=bindings,
    )
    return _Materials(
        authoritative_input_receipt=authoritative,
        spot_profile_receipt=spot,
        futures_profile_receipt=futures,
        option_profile_receipt=option,
        integrated_profile_receipt=integrated,
        output_bindings=bindings,
        bundle=bundle,
    )


def _rebuild(
    materials: _Materials,
    *,
    authoritative_input_receipt: AuthoritativeInputReceipt | None = None,
    output_bindings: tuple[AuthoritativeOutputBinding, ...] | None = None,
) -> PublicExecutionEvidenceBundle:
    return build_public_execution_evidence_bundle(
        experiment_spec_hash=EXPERIMENT_SPEC_HASH,
        authoritative_input_receipt=(
            authoritative_input_receipt or materials.authoritative_input_receipt
        ),
        spot_profile_receipt=materials.spot_profile_receipt,
        futures_profile_receipt=materials.futures_profile_receipt,
        option_profile_receipt=materials.option_profile_receipt,
        integrated_profile_receipt=materials.integrated_profile_receipt,
        output_bindings=(
            materials.output_bindings if output_bindings is None else output_bindings
        ),
    )


def test_bundle_is_complete_and_deterministic(materials: _Materials) -> None:
    first = materials.bundle
    second = _rebuild(materials)
    payload = first.as_dict()

    assert first == second
    assert first.content_hash == second.content_hash
    assert set(payload) == {
        "experiment_spec_hash",
        "authoritative_input_receipt",
        "spot_profile_receipt",
        "futures_profile_receipt",
        "option_profile_receipt",
        "integrated_profile_receipt",
        "output_bindings",
        "content_hash",
    }
    assert (
        payload["authoritative_input_receipt"]
        == materials.authoritative_input_receipt.as_dict()
    )
    assert tuple(binding.output_path for binding in first.output_bindings) == (
        "/T-01/institutional_receipt",
        "/T-02/institutional_receipt",
        "/T-03/institutional_receipt",
        "/T-04/institutional_receipt",
    )
    assert all(
        binding.input_receipt_hash == materials.authoritative_input_receipt.content_hash
        for binding in first.output_bindings
    )


def test_bundle_is_factory_only(materials: _Materials) -> None:
    with pytest.raises(
        PublicExecutionEvidenceError,
        match="bundle_requires_factory",
    ):
        replace(
            materials.bundle,
            experiment_spec_hash=_hash("forged-experiment-spec"),
        )


def test_bundle_rejects_binding_mismatches(materials: _Materials) -> None:
    authoritative = materials.authoritative_input_receipt
    wrong_value = authoritative.bind_output(
        output_path="/T-01/institutional_receipt",
        output_value={"forged": True},
        input_paths=("/T-01",),
        computation_hash=materials.spot_profile_receipt.content_hash,
    )
    with pytest.raises(
        PublicExecutionEvidenceError,
        match="output_value_hash_mismatch",
    ):
        _rebuild(
            materials,
            output_bindings=(
                wrong_value,
                *materials.output_bindings[1:],
            ),
        )

    wrong_computation = authoritative.bind_output(
        output_path="/T-01/institutional_receipt",
        output_value=materials.spot_profile_receipt.as_dict(),
        input_paths=("/T-01",),
        computation_hash=_hash("forged-computation"),
    )
    with pytest.raises(
        PublicExecutionEvidenceError,
        match="computation_hash_mismatch",
    ):
        _rebuild(
            materials,
            output_bindings=(
                wrong_computation,
                *materials.output_bindings[1:],
            ),
        )

    other_authority = _authoritative_input_receipt(artifact_variant="other")
    wrong_authority = other_authority.bind_output(
        output_path="/T-01/institutional_receipt",
        output_value=materials.spot_profile_receipt.as_dict(),
        input_paths=("/T-01",),
        computation_hash=materials.spot_profile_receipt.content_hash,
    )
    with pytest.raises(
        PublicExecutionEvidenceError,
        match="authoritative_input_receipt_mismatch",
    ):
        _rebuild(
            materials,
            output_bindings=(
                wrong_authority,
                *materials.output_bindings[1:],
            ),
        )

    with pytest.raises(
        PublicExecutionEvidenceError,
        match="canonical_order_required",
    ):
        _rebuild(
            materials,
            output_bindings=tuple(reversed(materials.output_bindings)),
        )


def test_atomic_content_addressed_republish(
    tmp_path: Path,
    materials: _Materials,
) -> None:
    paths = ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "datasets",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=None,
            max_workers=1,
            random_seed=7,
        ),
        project_root=PROJECT_ROOT,
    )
    first = publish_public_execution_evidence_bundle(
        path_manager=paths,
        experiment_id="experiment.public.execution.evidence",
        bundle=materials.bundle,
    )
    first_stat = first[0].stat()
    second = publish_public_execution_evidence_bundle(
        path_manager=paths,
        experiment_id="experiment.public.execution.evidence",
        bundle=materials.bundle,
    )
    second_stat = second[0].stat()

    assert first == second
    assert first[0].parent == paths.research_artifact_path(
        "experiment.public.execution.evidence",
        "public-execution-evidence",
    )
    assert first[0].name == (
        f"{materials.bundle.content_hash.removeprefix('sha256:')}.json"
    )
    assert first[1] == materials.bundle.content_hash
    assert first[2].uri == first[0].as_uri()
    assert json.loads(first[0].read_text(encoding="utf-8")) == (
        materials.bundle.as_dict()
    )
    assert (first_stat.st_ino, first_stat.st_mtime_ns) == (
        second_stat.st_ino,
        second_stat.st_mtime_ns,
    )
