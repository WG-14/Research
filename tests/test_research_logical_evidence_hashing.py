from __future__ import annotations

from copy import deepcopy

from market_research.research.candidate_profile import build_candidate_profile
from market_research.research.hashing import (
    report_content_hash_payload,
    sha256_prefixed,
)


def test_candidate_profile_recursively_excludes_runtime_and_path_observations() -> None:
    candidate = {
        "parameter_candidate_id": "candidate-a",
        "dataset_content_hash": "sha256:" + "1" * 64,
        "scenario_results": [
            {
                "scenario_id": "base",
                "detail_artifact_path": "/state-a/detail.json",
                "detail_artifact_ref": "research/a/detail.json",
                "detail_artifact_hash": "sha256:" + "2" * 64,
                "validation_resource_usage": {
                    "runtime_seconds": 0.25,
                    "final_cash": 100.0,
                },
            }
        ],
        "runtime_observability": {"wall_seconds": 0.5},
    }
    relocated = deepcopy(candidate)
    relocated["scenario_results"][0].update(
        {
            "detail_artifact_path": "/state-b/detail.json",
            "detail_artifact_ref": "research/b/detail.json",
            "detail_artifact_hash": "sha256:" + "3" * 64,
        }
    )
    relocated["scenario_results"][0]["validation_resource_usage"]["runtime_seconds"] = (
        99.0
    )
    relocated["runtime_observability"]["wall_seconds"] = 88.0

    first = build_candidate_profile(candidate)
    second = build_candidate_profile(relocated)

    assert first == second
    assert first["scenario_results"][0]["validation_resource_usage"] == {
        "final_cash": 100.0
    }
    assert sha256_prefixed(first) == sha256_prefixed(second)
    assert (
        candidate["scenario_results"][0]["detail_artifact_hash"] == "sha256:" + "2" * 64
    )

    changed_evidence = deepcopy(candidate)
    changed_evidence["scenario_results"][0]["validation_resource_usage"][
        "final_cash"
    ] = 101.0
    assert build_candidate_profile(changed_evidence) != first


def test_report_logical_hash_excludes_nested_lineage_runtime_but_keeps_evidence() -> (
    None
):
    report = {
        "manifest_hash": "sha256:" + "1" * 64,
        "lineage": {
            "manifest_hash": "sha256:" + "1" * 64,
            "manifest_path": "/inputs-a/manifest.json",
            "normalized_command_args": {
                "manifest": "/inputs-a/manifest.json",
                "run_id": "RUN-a",
            },
            "command_args_hash": "sha256:" + "2" * 64,
            "environment_config_fingerprint": "sha256:" + "3" * 64,
            "created_at": "2026-01-01T00:00:00+00:00",
            "lineage_hash": "sha256:" + "4" * 64,
        },
        "research_candidate_report_hash": "sha256:" + "8" * 64,
        "candidates": [
            {
                "candidate_id": "candidate-a",
                "validation_resource_usage": {
                    "runtime_seconds": 0.1,
                    "final_cash": 100.0,
                },
            }
        ],
    }
    rerun = deepcopy(report)
    rerun["lineage"].update(
        {
            "manifest_path": "/inputs-b/manifest.json",
            "normalized_command_args": {
                "manifest": "/inputs-b/manifest.json",
                "run_id": "RUN-b",
            },
            "command_args_hash": "sha256:" + "5" * 64,
            "environment_config_fingerprint": "sha256:" + "6" * 64,
            "created_at": "2026-01-02T00:00:00+00:00",
            "lineage_hash": "sha256:" + "7" * 64,
        }
    )
    rerun["research_candidate_report_hash"] = "sha256:" + "9" * 64
    rerun["candidates"][0]["validation_resource_usage"]["runtime_seconds"] = 10.0

    first = report_content_hash_payload(report)
    second = report_content_hash_payload(rerun)

    assert first == second
    assert first["manifest_hash"] == "sha256:" + "1" * 64
    assert first["lineage"]["manifest_hash"] == "sha256:" + "1" * 64
    assert first["candidates"][0]["validation_resource_usage"] == {"final_cash": 100.0}

    changed_evidence = deepcopy(report)
    changed_evidence["manifest_hash"] = "sha256:" + "0" * 64
    assert report_content_hash_payload(changed_evidence) != first
