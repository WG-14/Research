from __future__ import annotations

from bithumb_bot.canonical_decision import normalize_canonical_decision, observe_canonical_decisions


def _payload(**overrides):
    base = {
        "decision_contract_version": 2,
        "strategy_name": "unit",
        "raw_signal": "HOLD",
        "final_signal": "HOLD",
        "feature_snapshot": {"x": 1},
        "strategy_behavior_payload": {"y": 2},
    }
    base.update(overrides)
    return base


def test_normalize_reuses_existing_feature_and_behavior_hashes() -> None:
    with observe_canonical_decisions() as observer:
        normalized = normalize_canonical_decision(
            _payload(
                feature_snapshot_hash="sha256:feature",
                strategy_behavior_hash="sha256:behavior",
            )
        )

    assert normalized["feature_snapshot_hash"] == "sha256:feature"
    assert normalized["strategy_behavior_hash"] == "sha256:behavior"
    assert observer.as_dict()["canonical_payload_hash_call_count"] == 0


def test_summary_bulk_mode_skips_fallback_hash_when_hash_missing() -> None:
    with observe_canonical_decisions() as observer:
        normalized = normalize_canonical_decision(_payload(), allow_fallback_hash=False)

    assert normalized["feature_snapshot_hash"] == "feature_snapshot_hash_missing"
    assert normalized["strategy_behavior_hash"] == "strategy_behavior_hash_missing"
    assert normalized["feature_snapshot_hash_missing"] is True
    assert normalized["strategy_behavior_hash_missing"] is True
    assert normalized["canonical_hash_fallback_skipped"] is True
    assert observer.as_dict()["canonical_payload_hash_call_count"] == 0


def test_full_mode_preserves_fallback_hash_behavior() -> None:
    with observe_canonical_decisions() as observer:
        normalized = normalize_canonical_decision(_payload())

    assert normalized["feature_snapshot_hash"].startswith("sha256:")
    assert normalized["strategy_behavior_hash"].startswith("sha256:")
    assert observer.as_dict()["canonical_payload_hash_call_count"] == 2
