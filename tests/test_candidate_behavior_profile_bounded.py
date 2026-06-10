from __future__ import annotations

import json

from bithumb_bot.research.hashing import canonical_json_bytes, sha256_prefixed
from bithumb_bot.research.promotion_gate import build_candidate_behavior_profile, build_candidate_profile
from tests.factories.research_reports import minimal_candidate_payload, minimal_scenario_result


class TraversalSentinel:
    def __iter__(self):
        raise AssertionError("behavior profile traversed runtime stage_trace")


def test_behavior_profile_does_not_traverse_large_resource_usage() -> None:
    candidate = minimal_candidate_payload(
        scenario_results=[
            minimal_scenario_result(
                train_resource_usage={"stage_trace": TraversalSentinel(), "candles_processed": 10},
                validation_resource_usage={"stage_trace": TraversalSentinel(), "candles_processed": 20},
            )
        ]
    )
    base_profile = build_candidate_profile(candidate)

    behavior_profile = build_candidate_behavior_profile(candidate, base_profile=base_profile)

    assert "stage_trace" not in canonical_json_bytes(behavior_profile).decode("utf-8")


def test_behavior_profile_uses_allowed_keys() -> None:
    candidate = minimal_candidate_payload(
        acceptance_gate_result="FAIL",
        manifest_hash="sha256:manifest-runtime",
        experiment_id="runtime-experiment",
    )
    behavior_profile = build_candidate_behavior_profile(
        candidate,
        base_profile=build_candidate_profile(candidate),
    )

    assert "acceptance_gate_result" not in behavior_profile
    assert "source_experiment" not in behavior_profile
    assert "manifest_hash" not in behavior_profile
    assert "parameter_values" in behavior_profile
    assert "scenario_behavior_evidence_hashes" in behavior_profile


def test_behavior_profile_hash_stable_with_runtime_observability_changes() -> None:
    candidate = minimal_candidate_payload()
    base_profile = build_candidate_profile(candidate)
    base_hash = sha256_prefixed(build_candidate_behavior_profile(candidate, base_profile=base_profile))

    changed = json.loads(json.dumps(candidate))
    changed.update(
        {
            "experiment_id": "changed",
            "manifest_hash": "sha256:changed",
            "runtime_observability": {"wall_seconds": 999.0, "worker_pid": 123},
            "report_path": "/runtime/reports/changed.json",
        }
    )
    for scenario in changed.get("scenario_results") or []:
        scenario.setdefault("train_resource_usage", {})["stage_trace"] = [{"payload": "changed"}] * 100
        scenario.setdefault("validation_resource_usage", {})["stage_trace"] = [{"payload": "changed"}] * 100

    changed_hash = sha256_prefixed(
        build_candidate_behavior_profile(changed, base_profile=build_candidate_profile(changed))
    )

    assert changed_hash == base_hash
