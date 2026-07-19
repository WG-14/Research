from __future__ import annotations

from copy import deepcopy
from typing import Any

from market_research.research.final_selection import (
    selection_candidate_binding_summary,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.report_writer import candidate_evidence_hash_inputs


def _candidate() -> dict[str, Any]:
    compiled_hash = "sha256:" + "c" * 64
    return {
        "parameter_candidate_id": "candidate-integrity",
        "parameter_values_raw": {"window": 20},
        "effective_strategy_parameters_hash": "sha256:" + "e" * 64,
        "compiled_strategy_contract_hash": compiled_hash,
        "primary_scenario_id": "base",
        "acceptance_gate_result": "PASS",
        "validation_metrics": {"return_pct": 1.5, "trade_count": 4},
        "validation_metrics_v2": {
            "metrics_schema_version": 2,
            "return_risk": {"total_return_pct": 1.5},
        },
        "scenario_results": [
            {
                "scenario_id": "base",
                "compiled_strategy_contract_hash": compiled_hash,
                "validation_metrics": {"return_pct": 1.5, "trade_count": 4},
                "execution_evidence": {
                    "execution_evidence_schema_version": 3,
                    "point_in_time_decision_stream_hash": "sha256:" + "1" * 64,
                    "point_in_time_authority_binding_hash": "sha256:" + "2" * 64,
                    "point_in_time_evidence_content_hash": "sha256:" + "3" * 64,
                },
                "validation_resource_usage": {
                    "stage_trace": [{"stage": "decision", "index": 1}],
                },
            }
        ],
    }


def _candidate_hash(candidate: dict[str, Any]) -> str:
    return sha256_prefixed(
        candidate_evidence_hash_inputs(candidate),
        label="candidate_evidence_hash",
    )


def test_candidate_hash_binds_metric_bodies_and_nested_point_in_time_evidence() -> None:
    candidate = _candidate()
    baseline = _candidate_hash(candidate)

    metric_tamper = deepcopy(candidate)
    metric_tamper["scenario_results"][0]["validation_metrics"]["return_pct"] = 99.0
    assert _candidate_hash(metric_tamper) != baseline

    pit_tamper = deepcopy(candidate)
    pit_tamper["scenario_results"][0]["execution_evidence"][
        "point_in_time_evidence_content_hash"
    ] = "sha256:" + "9" * 64
    assert _candidate_hash(pit_tamper) != baseline

    pit_removal = deepcopy(candidate)
    pit_removal["scenario_results"][0]["execution_evidence"].pop(
        "point_in_time_authority_binding_hash"
    )
    assert _candidate_hash(pit_removal) != baseline


def test_candidate_hash_ignores_only_physical_binding_and_stage_trace_projection() -> (
    None
):
    candidate = _candidate()
    baseline = _candidate_hash(candidate)
    published = deepcopy(candidate)
    usage = published["scenario_results"][0]["validation_resource_usage"]
    usage.pop("stage_trace")
    usage["stage_trace_count"] = 1
    usage["stage_trace_hash"] = "sha256:" + "7" * 64
    published.update(
        {
            "candidate_result_artifact_ref": "derived/candidate.json",
            "candidate_result_artifact_hash": "sha256:" + "8" * 64,
            "candidate_result_artifact_detail_policy": "external_full",
            "reproduction_candidate_fingerprint": {
                "candidate_id": "candidate-integrity",
                "scenarios": [{"metrics_hash": "sha256:" + "9" * 64}],
            },
        }
    )
    assert _candidate_hash(published) == baseline


def test_cached_selection_binding_cannot_mask_authoritative_metric_tamper() -> None:
    candidate = _candidate()
    binding = selection_candidate_binding_summary(candidate)
    tampered = deepcopy(candidate)
    tampered["selection_binding"] = binding
    tampered["validation_metrics"]["return_pct"] = 42.0

    assert selection_candidate_binding_summary(tampered) != binding


def test_compact_projection_can_reuse_binding_without_full_source_fields() -> None:
    binding = selection_candidate_binding_summary(_candidate())
    compact = {
        "parameter_candidate_id": "candidate-integrity",
        "validation_metrics_v2": {"metrics_schema_version": 2},
        "selection_binding": binding,
    }

    assert selection_candidate_binding_summary(compact) == binding
