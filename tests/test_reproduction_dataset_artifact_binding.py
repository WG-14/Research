from __future__ import annotations
from market_research.research.reproduction import _dataset_split_hashes


def test_receipt_projection_preserves_artifact_and_split_hashes() -> None:
    digest = "sha256:" + "a" * 64
    rows = _dataset_split_hashes({"dataset_splits":{"train":{"content_hash":digest,"quality_hash":digest,"snapshot_data_hash":digest,"snapshot_query_hash":digest,"snapshot_fingerprint_hash":digest,"artifact_id":"a","artifact_manifest_hash":digest,"artifact_content_hash":digest,"artifact_schema_hash":digest,"verification_status":"VERIFIED","verification":{"overall_status":"VERIFIED"},"requested_range":{"start":"2026-01-01","end":"2026-01-01"}}}})
    assert rows[0]["artifact_id"] == "a"
