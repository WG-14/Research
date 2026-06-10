from __future__ import annotations

from bithumb_bot.research.hashing import canonical_json_bytes, sha256_prefixed
from bithumb_bot.research.promotion_gate import build_candidate_profile, evaluate_candidate_for_promotion
from tests.factories.research_reports import minimal_candidate_payload, minimal_scenario_result


def _large_candidate() -> dict[str, object]:
    stage_trace = [{"index": index, "payload": "x" * 100} for index in range(2_000)]
    candidate = minimal_candidate_payload(
        scenario_results=[
            minimal_scenario_result(
                train_resource_usage={"stage_trace": stage_trace, "candles_processed": 10},
                validation_resource_usage={"stage_trace": stage_trace, "candles_processed": 20},
                retained_detail_summary={"stage_trace": stage_trace, "content_hash": "sha256:retained"},
            )
        ],
        strategy_diagnostics={"large": ["diagnostic"] * 1_000, "content_hash": "sha256:diagnostics"},
        market_regime_bucket_performance=[{"bucket": index, "payload": "y" * 100} for index in range(1_000)],
    )
    candidate["candidate_profile_hash"] = sha256_prefixed(build_candidate_profile(candidate))
    return candidate


def test_candidate_profile_uses_scenario_evidence_hashes_not_full_results() -> None:
    profile = build_candidate_profile(_large_candidate())

    assert "scenario_results" not in profile
    assert profile["scenario_result_evidence_hashes"]
    assert profile["scenario_result_evidence_summary"]["scenario_result_count"] == 1


def test_candidate_profile_omits_full_resource_usage_from_profile_hash() -> None:
    profile = build_candidate_profile(_large_candidate())
    serialized = canonical_json_bytes(profile).decode("utf-8")

    assert "stage_trace" not in serialized
    assert "train_resource_usage" not in serialized or "stage_trace_count" in serialized
    assert "validation_resource_usage" not in serialized or "stage_trace_count" in serialized


def test_candidate_profile_large_stage_trace_does_not_increase_hash_payload_linearly() -> None:
    profile = build_candidate_profile(_large_candidate())

    assert len(canonical_json_bytes(profile)) < 50_000


def test_large_scenario_results_do_not_enter_candidate_profile_hash() -> None:
    candidate = _large_candidate()

    ok, reasons = evaluate_candidate_for_promotion(candidate)

    assert "candidate_profile_hash_mismatch" not in reasons
    assert isinstance(ok, bool)
