from __future__ import annotations

import json
from copy import deepcopy

import pytest

from market_research.research.multi_asset.authoritative_inputs import (
    AuthoritativeInputError,
    AuthoritativeInputFactory,
    AuthoritativeInputReceipt,
    AuthoritativeOutputBinding,
)
from market_research.research.multi_asset.evidence import evidence_hash
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


def _document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "spot": {
            "instrument_id": "asset_xyz",
            "price": "100",
            "quantity": "2",
        },
    }


def _canonical_row(document: dict[str, object]) -> dict[str, object]:
    row: dict[str, object] = {
        "row_id": "row:canonical:1",
        "row_kind": "CANONICAL_RESEARCH_INPUTS",
        "event_at": "2026-01-02T14:30:00Z",
        "knowledge_at": "2026-01-02T14:30:01Z",
        "source_id": "externally-prepared-fixture",
        "source_schema_version": "v1",
        "payload": document,
    }
    row["content_hash"] = research_input_source_row_hash(row)
    return row


def _resolved(
    *,
    document: dict[str, object] | None = None,
    rows: list[dict[str, object]] | None = None,
) -> ResolvedEvidenceArtifact:
    actual_document = _document() if document is None else document
    actual_rows = [_canonical_row(actual_document)] if rows is None else rows
    payload = {
        "artifact_kind": "IMMUTABLE_RESEARCH_INPUTS",
        "input_schema_id": "fixture-inputs",
        "input_schema_version": 1,
        "input_document": actual_document,
        "input_document_hash": research_input_document_hash(actual_document),
        "source_rows": actual_rows,
        "source_rows_hash": research_input_source_rows_hash(actual_rows),
    }
    return ResolvedEvidenceArtifact(
        reference=EvidenceArtifactRef(
            role=EvidenceArtifactRole.RESEARCH_INPUTS,
            logical_id="research-inputs:fixture",
            version="v1",
            uri="file:///tmp/research-inputs-fixture.json",
            content_hash=bytes_sha256(b"immutable-input-artifact"),
            schema_hash=evidence_artifact_schema_hash(),
            byte_length=24,
        ),
        quality_flags=("EXTERNALLY_PREPARED_FIXTURE",),
        payload_json=json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ),
        verified_at="2026-01-02T14:30:02Z",
    )


def _factory() -> AuthoritativeInputFactory:
    return AuthoritativeInputFactory(
        input_schema_id="fixture-inputs",
        input_schema_version=1,
    )


def test_factory_resolves_every_leaf_and_output_back_to_actual_source_row() -> None:
    document = _document()
    receipt = _factory().resolve(
        (_resolved(document=document),),
        input_document=document,
        decision_cutoff="2026-01-02T14:31:00Z",
    )

    assert receipt.input_document == document
    assert set(receipt.input_paths) == {
        "/schema_version",
        "/spot/instrument_id",
        "/spot/price",
        "/spot/quantity",
    }
    assert receipt.source_rows_for_path("/spot/price")[0].payload == document
    assert receipt.input_value_for_path("/spot/price") == "100"
    assert receipt.source_value_for_path("/spot/price") == "100"
    binding = receipt.bind_output(
        output_path="/study/T-01/gross_notional",
        output_value="200",
        input_paths=("/spot",),
        computation_hash=evidence_hash(
            {"operation": "price_times_quantity"},
            label="test-computation",
        ),
    )
    assert {item.input_path for item in receipt.coverages_for_path("/spot")} == {
        "/spot/instrument_id",
        "/spot/price",
        "/spot/quantity",
    }
    assert receipt.source_rows_for_output(binding) == receipt.source_rows
    assert binding.as_dict()["content_hash"] == binding.content_hash
    serialized = receipt.as_dict()
    assert serialized["input_document"] == document
    assert serialized["source_rows"] == [item.as_dict() for item in receipt.source_rows]
    assert serialized["coverage"] == [item.as_dict() for item in receipt.coverage]
    assert serialized["content_hash"] == receipt.content_hash


def test_factory_rejects_caller_document_self_certification() -> None:
    artifact_document = _document()
    caller_document = deepcopy(artifact_document)
    caller_document["spot"]["price"] = "101"  # type: ignore[index]

    with pytest.raises(
        AuthoritativeInputError,
        match="caller_document_mismatch",
    ):
        _factory().resolve(
            (_resolved(document=artifact_document),),
            input_document=caller_document,
            decision_cutoff="2026-01-02T14:31:00Z",
        )


def test_factory_rejects_source_row_known_after_decision_cutoff() -> None:
    document = _document()
    row = _canonical_row(document)
    row["knowledge_at"] = "2026-01-02T14:32:00Z"
    row["content_hash"] = research_input_source_row_hash(row)

    with pytest.raises(
        AuthoritativeInputError,
        match="source_row_future_knowledge",
    ):
        _factory().resolve(
            (_resolved(document=document, rows=[row]),),
            input_document=document,
            decision_cutoff="2026-01-02T14:31:00Z",
        )


@pytest.mark.parametrize(
    ("bindings", "source_record", "match"),
    [
        (
            [
                {
                    "input_path": "/spot/price",
                    "source_path": "/price",
                }
            ],
            {"price": "100"},
            "path_coverage_missing",
        ),
        (
            [
                {
                    "input_path": "/spot/price",
                    "source_path": "/price",
                }
            ],
            {"price": "101"},
            "conflicting_source_value",
        ),
        (
            [
                {
                    "input_path": "/not-a-document-leaf",
                    "source_path": "/price",
                }
            ],
            {"price": "100"},
            "orphan_source_path",
        ),
    ],
)
def test_pointer_rows_fail_closed_on_missing_conflicting_or_orphan_coverage(
    bindings: list[dict[str, str]],
    source_record: dict[str, str],
    match: str,
) -> None:
    document = _document()
    row: dict[str, object] = {
        "row_id": "row:pointer:1",
        "row_kind": "JSON_POINTER_INPUTS",
        "event_at": "2026-01-02T14:30:00Z",
        "knowledge_at": "2026-01-02T14:30:01Z",
        "source_id": "externally-prepared-fixture",
        "source_schema_version": "v1",
        "payload": {
            "bindings": bindings,
            "source_record": source_record,
        },
    }
    row["content_hash"] = research_input_source_row_hash(row)

    with pytest.raises(AuthoritativeInputError, match=match):
        _factory().resolve(
            (_resolved(document=document, rows=[row]),),
            input_document=document,
            decision_cutoff="2026-01-02T14:31:00Z",
        )


def test_factory_rejects_ambiguous_mixed_full_document_and_pointer_coverage() -> None:
    document = _document()
    pointer_row: dict[str, object] = {
        "row_id": "row:pointer:2",
        "row_kind": "JSON_POINTER_INPUTS",
        "event_at": "2026-01-02T14:30:00Z",
        "knowledge_at": "2026-01-02T14:30:01Z",
        "source_id": "externally-prepared-fixture",
        "source_schema_version": "v1",
        "payload": {
            "bindings": [
                {
                    "input_path": "/spot/price",
                    "source_path": "/price",
                }
            ],
            "source_record": {"price": "100"},
        },
    }
    pointer_row["content_hash"] = research_input_source_row_hash(pointer_row)

    with pytest.raises(
        AuthoritativeInputError,
        match="duplicate_source_coverage",
    ):
        _factory().resolve(
            (
                _resolved(
                    document=document,
                    rows=[_canonical_row(document), pointer_row],
                ),
            ),
            input_document=document,
            decision_cutoff="2026-01-02T14:31:00Z",
        )


def test_receipts_and_output_bindings_cannot_be_forged_directly() -> None:
    hash_value = bytes_sha256(b"x")
    with pytest.raises(
        AuthoritativeInputError,
        match="receipt_requires_factory",
    ):
        AuthoritativeInputReceipt(
            input_schema_id="fixture-inputs",
            input_schema_version=1,
            decision_cutoff="2026-01-02T14:31:00Z",
            artifact_logical_id="research-inputs:fixture",
            artifact_version="v1",
            input_document_hash=hash_value,
            artifact_hash=hash_value,
            source_rows_hash=hash_value,
            source_row_hashes=(hash_value,),
            coverage_hash=hash_value,
            source_rows=(),
            coverage=(),
            _input_document_json="{}",
        )
    with pytest.raises(
        AuthoritativeInputError,
        match="output_binding_requires_factory",
    ):
        AuthoritativeOutputBinding(
            output_path="/study/value",
            input_paths=("/spot/price",),
            output_value_hash=hash_value,
            computation_hash=hash_value,
            input_receipt_hash=hash_value,
            source_row_hashes=(hash_value,),
        )
