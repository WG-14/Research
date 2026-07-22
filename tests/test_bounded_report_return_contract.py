from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from market_research.research.artifact_store import ResearchArtifactContext
from market_research.research.final_selection import (
    selection_candidate_binding_summary,
)
from market_research.research.report_writer import candidate_evidence_hash_inputs
from market_research.research.validation_protocol import (
    ResearchValidationError,
    _candidate_result_path,
    _install_published_report_payload,
    resolve_candidate_result_artifact,
    run_research_backtest,
    run_research_walk_forward,
)
from market_research.research.hashing import (
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research_composition import builtin_strategy_registry
from tests.test_frozen_dataset_multi_split_integration import (
    frozen_manifest_and_manager,
)


@pytest.mark.parametrize("report_detail", ["index", "summary"])
@pytest.mark.parametrize(
    ("runner", "report_name", "walk_forward"),
    [
        (run_research_backtest, "backtest", False),
        (run_research_walk_forward, "walk_forward", True),
    ],
)
def test_bounded_report_returns_compact_candidates_and_binds_external_full_detail(
    tmp_path,
    report_detail,
    runner,
    report_name,
    walk_forward,
) -> None:
    _, manifest, manager = frozen_manifest_and_manager(
        tmp_path,
        walk_forward=walk_forward,
    )
    manifest = replace(
        manifest,
        research_run=replace(
            manifest.research_run,
            report_detail=report_detail,
        ),
    )

    returned = runner(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )

    returned_candidate = returned["candidates"][0]
    assert "compiled_strategy_contract" not in returned_candidate
    assert "scenario_results" not in returned_candidate
    assert returned_candidate["candidate_result_artifact_detail_policy"] == (
        "external_full"
    )

    persisted_path = manager.report_path(
        "research",
        manifest.experiment_id,
        f"{report_name}_report.json",
    )
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    assert returned["schema_version"] == 2
    assert persisted["schema_version"] == 2
    assert persisted["content_hash"] == sha256_prefixed(
        report_content_hash_payload(persisted),
        label="report_content_hash",
    )
    derived_candidates = json.loads(
        Path(persisted["derived_candidates_path"]).read_text(encoding="utf-8")
    )
    assert derived_candidates["schema_version"] == 2
    assert persisted["derived_candidates_hash"] == sha256_prefixed(
        report_content_hash_payload(derived_candidates),
        label="derived_candidate_summary",
    )
    persisted_candidate = persisted["candidates"][0]
    assert returned_candidate == persisted_candidate

    detail_path = (
        manager.data_dir() / returned_candidate["candidate_result_artifact_ref"]
    ).resolve()
    assert (
        detail_path
        == _candidate_result_path(
            manager,
            manifest.experiment_id,
            returned_candidate["parameter_candidate_id"],
            returned_candidate["candidate_result_artifact_hash"],
        ).resolve()
    )
    detail = json.loads(detail_path.read_text(encoding="utf-8"))
    assert isinstance(detail["compiled_strategy_contract"], dict)
    assert detail["scenario_results"]
    assert returned_candidate["candidate_result_artifact_hash"] == sha256_prefixed(
        detail,
        label="candidate_result_artifact_hash",
    )
    assert (
        resolve_candidate_result_artifact(
            manager=manager,
            compact_candidate=returned_candidate,
            expected_experiment_id=manifest.experiment_id,
            expected_manifest_hash=manifest.manifest_hash(),
            expected_dataset_snapshot_id=str(returned["dataset_snapshot_id"]),
            expected_dataset_content_hash=str(returned["dataset_content_hash"]),
        )
        == detail
    )


@pytest.mark.parametrize(
    ("runner", "report_name", "walk_forward"),
    [
        (run_research_backtest, "backtest", False),
        (run_research_walk_forward, "walk_forward", True),
    ],
)
def test_standard_report_preserves_existing_full_in_memory_contract(
    tmp_path,
    runner,
    report_name,
    walk_forward,
) -> None:
    _, manifest, manager = frozen_manifest_and_manager(
        tmp_path,
        walk_forward=walk_forward,
    )
    manifest = replace(
        manifest,
        research_run=replace(manifest.research_run, report_detail="standard"),
    )

    returned = runner(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )

    assert isinstance(returned["candidates"][0]["compiled_strategy_contract"], dict)
    persisted_path = manager.report_path(
        "research",
        manifest.experiment_id,
        f"{report_name}_report.json",
    )
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    assert "compiled_strategy_contract" not in persisted["candidates"][0]


@pytest.mark.parametrize("report_detail", ["index", "summary"])
def test_oversized_full_candidate_set_is_not_reinserted_into_bounded_return(
    report_detail,
) -> None:
    oversized = "x" * (6 * 1024 * 1024)
    transient = {
        "candidates": [
            {"parameter_candidate_id": f"candidate-{index}", "detail": oversized}
            for index in range(3)
        ]
    }
    published = {
        "candidates": [
            {"parameter_candidate_id": f"candidate-{index}"} for index in range(3)
        ]
    }
    sink: list[dict[str, object]] = []

    full = _install_published_report_payload(
        in_memory_report=transient,
        published_report=published,
        report_detail=report_detail,
        full_candidates_sink=sink,
    )

    assert len(json.dumps(full).encode("utf-8")) > 16 * 1024 * 1024
    assert len(json.dumps(transient).encode("utf-8")) < 4096
    assert sink == full
    assert transient == published


def _candidate_artifact_fixture(tmp_path: Path):
    _, manifest, manager = frozen_manifest_and_manager(tmp_path, walk_forward=False)
    candidate_id = "candidate-artifact-test"
    payload = {
        "experiment_id": manifest.experiment_id,
        "manifest_hash": manifest.manifest_hash(),
        "dataset_snapshot_id": manifest.dataset.snapshot_id,
        "dataset_content_hash": "sha256:" + "d" * 64,
        "parameter_candidate_id": candidate_id,
        "parameter_values_raw": {"window": 20},
        "validation_metrics": {"return_pct": 1.0, "trade_count": 2},
        "compiled_strategy_contract": {"strategy_name": manifest.strategy_name},
        "scenario_results": [
            {
                "scenario_id": "base",
                "execution_evidence": {
                    "point_in_time_decision_stream_hash": "sha256:" + "1" * 64,
                    "point_in_time_authority_binding_hash": "sha256:" + "2" * 64,
                    "point_in_time_evidence_content_hash": "sha256:" + "3" * 64,
                },
            }
        ],
    }
    artifact_hash = sha256_prefixed(
        payload,
        label="candidate_result_artifact_hash",
    )
    path = _candidate_result_path(
        manager,
        manifest.experiment_id,
        candidate_id,
        artifact_hash,
    )
    compact = {
        "parameter_candidate_id": candidate_id,
        "candidate_result_artifact_ref": path.resolve()
        .relative_to(manager.data_dir().resolve())
        .as_posix(),
        "candidate_result_artifact_hash": artifact_hash,
        "candidate_result_artifact_detail_policy": "external_full",
        "candidate_payload_hash": sha256_prefixed(
            candidate_evidence_hash_inputs(payload),
            label="candidate_evidence_hash",
        ),
        "selection_binding": selection_candidate_binding_summary(payload),
    }
    return manifest, manager, path, payload, compact


def test_candidate_result_artifact_publication_is_idempotent_and_immutable(
    tmp_path: Path,
) -> None:
    manifest, manager, path, payload, _ = _candidate_artifact_fixture(tmp_path)
    store = ResearchArtifactContext(
        manager=manager,
        experiment_id=manifest.experiment_id,
    )

    first = store.write_json_atomic_create_or_verify(path, payload)
    replay = store.write_json_atomic_create_or_verify(path, deepcopy(payload))
    assert first.bytes == replay.bytes
    prior = path.read_bytes()
    with pytest.raises(ValueError, match="atomic_json_target_conflict"):
        store.write_json_atomic_create_or_verify(path, {**payload, "revision": 2})
    assert path.read_bytes() == prior

    distinct_payload = {**payload, "revision": 2}
    distinct_hash = sha256_prefixed(
        distinct_payload,
        label="candidate_result_artifact_hash",
    )
    distinct_path = _candidate_result_path(
        manager,
        manifest.experiment_id,
        payload["parameter_candidate_id"],
        distinct_hash,
    )
    store.write_json_atomic_create_or_verify(distinct_path, distinct_payload)
    assert distinct_path != path
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert json.loads(distinct_path.read_text(encoding="utf-8")) == distinct_payload


@pytest.mark.parametrize(
    "failure_mode",
    [
        "missing",
        "tampered",
        "substituted",
        "wrong_ref",
        "wrong_hash",
        "invalid_hash",
        "semantic_metric_tamper",
        "semantic_pit_tamper",
        "traversal",
        "symlink",
    ],
)
def test_candidate_result_artifact_resolver_fails_closed(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    manifest, manager, path, payload, compact = _candidate_artifact_fixture(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if failure_mode == "substituted":
        payload["parameter_candidate_id"] = "candidate-substituted"
    elif failure_mode == "semantic_metric_tamper":
        payload["validation_metrics"]["return_pct"] = 99.0
    elif failure_mode == "semantic_pit_tamper":
        payload["scenario_results"][0]["execution_evidence"][
            "point_in_time_evidence_content_hash"
        ] = "sha256:" + "9" * 64
    if failure_mode in {
        "substituted",
        "semantic_metric_tamper",
        "semantic_pit_tamper",
    }:
        substituted_hash = sha256_prefixed(
            payload,
            label="candidate_result_artifact_hash",
        )
        compact["candidate_result_artifact_hash"] = substituted_hash
        path = _candidate_result_path(
            manager,
            manifest.experiment_id,
            compact["parameter_candidate_id"],
            substituted_hash,
        )
        compact["candidate_result_artifact_ref"] = (
            path.resolve().relative_to(manager.data_dir().resolve()).as_posix()
        )
        path.parent.mkdir(parents=True, exist_ok=True)
    if failure_mode == "symlink":
        target = path.parent / "symlink-target.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        path.symlink_to(target)
    elif failure_mode != "missing":
        path.write_text(json.dumps(payload), encoding="utf-8")
    if failure_mode == "tampered":
        path.write_text(json.dumps({**payload, "tampered": True}), encoding="utf-8")
    elif failure_mode == "wrong_ref":
        compact["candidate_result_artifact_ref"] = "wrong/candidate.json"
    elif failure_mode == "wrong_hash":
        compact["candidate_result_artifact_hash"] = "sha256:" + "0" * 64
    elif failure_mode == "invalid_hash":
        compact["candidate_result_artifact_hash"] = "sha256:not-a-digest"
    elif failure_mode == "traversal":
        compact["candidate_result_artifact_ref"] = "../candidate.json"

    with pytest.raises(ResearchValidationError, match="candidate_result_artifact_"):
        resolve_candidate_result_artifact(
            manager=manager,
            compact_candidate=compact,
            expected_experiment_id=manifest.experiment_id,
            expected_manifest_hash=manifest.manifest_hash(),
            expected_dataset_snapshot_id=manifest.dataset.snapshot_id,
            expected_dataset_content_hash="sha256:" + "d" * 64,
        )


def test_candidate_result_artifact_resolver_binds_authoritative_full_row(
    tmp_path: Path,
) -> None:
    manifest, manager, path, payload, compact = _candidate_artifact_fixture(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    authoritative = {
        **deepcopy(payload),
        "candidate_result_artifact_ref": compact["candidate_result_artifact_ref"],
        "candidate_result_artifact_hash": compact["candidate_result_artifact_hash"],
        "candidate_result_artifact_detail_policy": "external_full",
    }

    assert (
        resolve_candidate_result_artifact(
            manager=manager,
            compact_candidate=authoritative,
            expected_experiment_id=manifest.experiment_id,
            expected_manifest_hash=manifest.manifest_hash(),
            expected_dataset_snapshot_id=manifest.dataset.snapshot_id,
            expected_dataset_content_hash="sha256:" + "d" * 64,
        )
        == payload
    )

    authoritative["validation_metrics"]["return_pct"] = 99.0
    with pytest.raises(
        ResearchValidationError,
        match="candidate_result_artifact_logical_candidate_hash_mismatch",
    ):
        resolve_candidate_result_artifact(
            manager=manager,
            compact_candidate=authoritative,
            expected_experiment_id=manifest.experiment_id,
            expected_manifest_hash=manifest.manifest_hash(),
            expected_dataset_snapshot_id=manifest.dataset.snapshot_id,
            expected_dataset_content_hash="sha256:" + "d" * 64,
        )
