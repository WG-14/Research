from __future__ import annotations

import json

import pytest

from market_research.research.multi_asset.research_package import (
    EvidenceArtifactRole,
    MultiAssetResearchPackageError,
    encode_evidence_artifact,
    research_input_document_hash,
    research_input_source_row_hash,
    research_input_source_rows_hash,
)


def _row(
    row_id: str,
    *,
    event_at: str = "2026-01-02T14:30:00Z",
    knowledge_at: str = "2026-01-02T14:30:01Z",
    price: str = "100",
) -> dict[str, object]:
    row: dict[str, object] = {
        "row_id": row_id,
        "row_kind": "SPOT_QUOTE",
        "event_at": event_at,
        "knowledge_at": knowledge_at,
        "source_id": "fixture-provider",
        "source_schema_version": "v1",
        "payload": {
            "instrument_id": "asset_xyz",
            "currency": "USD",
            "price": price,
            "price_unit": "USD_per_unit",
        },
    }
    row["content_hash"] = research_input_source_row_hash(row)
    return row


def _payload() -> dict[str, object]:
    document = {
        "schema_version": 1,
        "spot": {
            "instrument_id": "asset_xyz",
            "entry_price": "100",
        },
    }
    rows = [_row("row:spot:1")]
    return {
        "artifact_kind": "IMMUTABLE_RESEARCH_INPUTS",
        "input_schema_id": "builtin-multi-asset-inputs",
        "input_schema_version": 1,
        "input_document": document,
        "input_document_hash": research_input_document_hash(document),
        "source_rows": rows,
        "source_rows_hash": research_input_source_rows_hash(rows),
    }


def test_research_input_artifact_recomputes_document_and_source_row_hashes() -> None:
    raw = encode_evidence_artifact(
        role=EvidenceArtifactRole.RESEARCH_INPUTS,
        logical_id="research-inputs:fixture",
        version="v1",
        quality_flags=("EXTERNALLY_PREPARED_FIXTURE",),
        payload=_payload(),
    )

    envelope = json.loads(raw)
    assert envelope["payload"]["input_document"]["spot"]["entry_price"] == "100"
    assert envelope["payload"]["source_rows"][0]["row_id"] == "row:spot:1"


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (
            lambda payload: payload["input_document"]["spot"].update(  # type: ignore[index,union-attr]
                {"entry_price": "101"}
            ),
            "research_inputs_document_hash_mismatch",
        ),
        (
            lambda payload: payload["source_rows"][0]["payload"].update(  # type: ignore[index,union-attr]
                {"price": "101"}
            ),
            "research_inputs_source_row_hash_mismatch",
        ),
        (
            lambda payload: payload["source_rows"][0].update(  # type: ignore[index,union-attr]
                {"knowledge_at": "2026-01-02T14:29:59Z"}
            ),
            "research_inputs_source_row_future_event",
        ),
    ],
)
def test_research_input_artifact_rejects_self_certified_or_future_rows(
    mutator: object,
    match: str,
) -> None:
    payload = _payload()
    mutator(payload)  # type: ignore[operator]

    with pytest.raises(MultiAssetResearchPackageError, match=match):
        encode_evidence_artifact(
            role=EvidenceArtifactRole.RESEARCH_INPUTS,
            logical_id="research-inputs:fixture",
            version="v1",
            quality_flags=("EXTERNALLY_PREPARED_FIXTURE",),
            payload=payload,
        )


def test_research_input_artifact_requires_deterministic_unique_row_order() -> None:
    payload = _payload()
    second = _row("row:spot:0", price="99")
    rows = [payload["source_rows"][0], second]  # type: ignore[index]
    payload["source_rows"] = rows
    payload["source_rows_hash"] = research_input_source_rows_hash(rows)  # type: ignore[arg-type]

    with pytest.raises(
        MultiAssetResearchPackageError,
        match="research_inputs_source_rows_order_invalid",
    ):
        encode_evidence_artifact(
            role=EvidenceArtifactRole.RESEARCH_INPUTS,
            logical_id="research-inputs:fixture",
            version="v1",
            quality_flags=("EXTERNALLY_PREPARED_FIXTURE",),
            payload=payload,
        )
