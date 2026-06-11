from __future__ import annotations

from bithumb_bot.canonical_decision import canonical_payload_hash, observe_canonical_decisions


def test_canonical_payload_hash_records_call_count_and_bytes() -> None:
    with observe_canonical_decisions() as observer:
        digest = canonical_payload_hash({"x": 1}, label="unit_payload")

    payload = observer.as_dict()
    assert digest.startswith("sha256:")
    assert payload["canonical_payload_hash_call_count"] == 1
    assert payload["canonical_hash_payload_bytes"] > 0
    assert payload["largest_canonical_hash_payload_bytes"] > 0
    assert payload["largest_canonical_hash_label"] == "unit_payload"
    assert payload["stable_value_call_count"] == 1
    assert payload["stable_value_wall_seconds"] >= 0.0
    assert payload["canonical_json_wall_seconds"] >= 0.0
