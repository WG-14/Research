from __future__ import annotations

import copy
import json
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from market_research.research.experiment_registry import (
    EXPERIMENT_REGISTRY_SCHEMA_VERSION,
    FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
    append_attempt_aborted,
    append_attempt_completion,
    final_holdout_reuse_key_hash_v2_from_parts,
    load_experiment_registry_rows,
    reserve_research_attempt,
    reserve_research_attempt_checked,
    validate_experiment_registry_binding,
)
from market_research.research.final_selection import (
    compute_final_holdout_result_hash,
    validate_confirmation_artifact,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.validation_protocol import (
    run_final_holdout_confirmation,
    run_research_backtest,
)
from market_research.research.cli import cmd_research_registry_validate
from market_research.research_composition import builtin_strategy_registry
from market_research.research_cli.context import ResearchAppContext

from .test_frozen_dataset_multi_split_integration import (
    _prepare_confirmable_single_candidate_report,
    frozen_manifest_and_manager,
)


def test_actual_registry_rows_bind_completed_frozen_artifact_evidence(tmp_path) -> None:
    _, parsed, manager = frozen_manifest_and_manager(
        tmp_path,
        final_selection=True,
        strategy_name="buy_and_hold_baseline",
    )
    parsed = replace(
        parsed,
        raw={
            **parsed.raw,
            "objective_metric": "return",
            "experiment_family_id": "registry-evidence-family",
            "hypothesis_id": "registry-evidence-hypothesis",
        },
    )
    report = run_research_backtest(
        manifest=parsed,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )
    _prepare_confirmable_single_candidate_report(
        report=report,
        manifest=parsed,
        manager=manager,
    )
    confirmation = run_final_holdout_confirmation(
        manifest=parsed,
        selection_report=report,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )
    rows = load_experiment_registry_rows(Path(confirmation["experiment_registry_path"]))
    reservation = next(
        row for row in rows if row["event_type"] == "research_attempt_reserved"
    )
    completion = next(
        row for row in rows if row["event_type"] == "research_attempt_completed"
    )
    assert reservation["schema_version"] == EXPERIMENT_REGISTRY_SCHEMA_VERSION
    assert reservation["pre_exposure_reservation_key_hash"].startswith("sha256:")
    assert reservation.get("final_holdout_reuse_key_hash") is None
    required = (
        "dataset_artifact_evidence_hash",
        "final_holdout_query_hash",
        "final_holdout_data_hash",
        "final_holdout_fingerprint_hash",
        "final_holdout_quality_hash",
        "final_holdout_reuse_key_hash",
    )
    assert all(completion[field].startswith("sha256:") for field in required)
    assert (
        completion["final_holdout_reuse_key_schema_version"]
        == FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
    )
    recomputed = final_holdout_reuse_key_hash_v2_from_parts(
        strategy_name=parsed.strategy_name,
        market=parsed.market,
        interval=parsed.interval,
        final_holdout=confirmation["dataset_evidence"]["requested_range"],
        objective_metric="return",
        dataset_artifact_evidence_hash=confirmation["dataset_artifact_evidence_hash"],
        final_holdout_query_hash=confirmation["final_holdout_query_hash"],
        final_holdout_data_hash=confirmation["final_holdout_data_hash"],
        final_holdout_fingerprint_hash=confirmation["final_holdout_fingerprint_hash"],
        final_holdout_quality_hash=confirmation["final_holdout_quality_hash"],
    )
    assert (
        completion["final_holdout_reuse_key_hash"]
        == recomputed
        == confirmation["final_holdout_reuse_key_hash"]
    )
    for field in required[:-1]:
        assert confirmation[field] == completion[field]
    assert (
        completion["final_holdout_result_hash"]
        == confirmation["final_holdout_result_hash"]
    )
    assert (
        validate_confirmation_artifact(
            confirmation,
            selection_artifact=report["selection_artifact"],
        )
        == []
    )

    tampered = copy.deepcopy(confirmation)
    tampered["candidate_results"][0]["metrics"]["return_pct"] = 999999.0
    tampered["final_holdout_result_hash"] = compute_final_holdout_result_hash(tampered)
    tampered_material = {
        key: value
        for key, value in tampered.items()
        if key not in {"content_hash", "confirmation_artifact_path"}
    }
    tampered["content_hash"] = sha256_prefixed(
        tampered_material,
        label="final_holdout_confirmation",
    )
    assert (
        validate_confirmation_artifact(
            tampered,
            selection_artifact=report["selection_artifact"],
        )
        == []
    )
    assert "experiment_registry_stale" in validate_experiment_registry_binding(
        report=tampered,
        require_complete=True,
    )

    completion_updates = {
        key: completion[key]
        for key in (
            *required,
            "final_holdout_reuse_key_schema_version",
            "selection_artifact_hash",
            "selected_candidate_id",
            "candidate_count",
            "confirmation_gate_result",
            "final_holdout_result_hash_schema_version",
            "final_holdout_result_hash",
        )
    }
    reservation_result = {
        "path": confirmation["experiment_registry_path"],
        "row_hash": reservation["row_hash"],
        "row": reservation,
    }
    with pytest.raises(ValueError, match="attempt_already_terminal"):
        append_attempt_completion(
            manager=manager,
            reservation=reservation_result,
            updates=completion_updates,
        )
    assert (
        append_attempt_aborted(
            manager=manager,
            reservation_row_hash=reservation["row_hash"],
            reason="must not override completed evidence",
        )
        is None
    )


def test_old_and_unknown_registry_schemas_fail_closed(tmp_path) -> None:
    path = tmp_path / "registry.jsonl"
    for schema in (1, 2, 999):
        path.write_text('{"schema_version": %d}\n' % schema)
        try:
            load_experiment_registry_rows(path)
        except ValueError as exc:
            assert "schema_version_unsupported" in str(exc)
        else:
            raise AssertionError("legacy registry schema must not be reinterpreted")


def test_completed_holdout_blocks_second_pre_exposure_reservation(tmp_path) -> None:
    _, _, manager = frozen_manifest_and_manager(tmp_path)
    base = {
        "experiment_family_id": "family",
        "hypothesis_id": "hypothesis",
        "pre_exposure_reservation_key_hash": "sha256:" + "a" * 64,
        "selection_artifact_hash": "sha256:" + "b" * 64,
        "selected_candidate_id": "candidate-a",
    }
    contract = {"gates": {"max_holdout_reuse_count": 0}}

    first = reserve_research_attempt_checked(
        manager=manager,
        base_payload=base,
        statistical_validation_contract=contract,
    )
    second = reserve_research_attempt_checked(
        manager=manager,
        base_payload={**base, "selection_artifact_hash": "sha256:" + "c" * 64},
        statistical_validation_contract=contract,
    )

    assert first["accepted"] is True
    assert first["row"]["selection_artifact_hash"] == base["selection_artifact_hash"]
    assert second["accepted"] is False
    assert "holdout_reuse_budget_exceeded" in second["reasons"]


def test_selection_counters_must_match_authoritative_registry_counters(
    tmp_path,
) -> None:
    _, _, manager = frozen_manifest_and_manager(tmp_path)
    base = {
        "experiment_family_id": "counter-family",
        "hypothesis_id": "counter-hypothesis",
        "pre_exposure_reservation_key_hash": "sha256:" + "9" * 64,
        "selection_artifact_hash": "sha256:" + "8" * 64,
        "selected_candidate_id": "candidate-a",
        "selection_attempt_index": 1,
        "selection_holdout_reuse_count": 0,
    }
    contract = {
        "gates": {
            "max_attempt_index_without_new_hypothesis": 2,
            "max_holdout_reuse_count": 1,
        }
    }

    first = reserve_research_attempt_checked(
        manager=manager,
        base_payload=base,
        statistical_validation_contract=contract,
    )
    stale_second = reserve_research_attempt_checked(
        manager=manager,
        base_payload=base,
        statistical_validation_contract=contract,
    )
    current_second = reserve_research_attempt_checked(
        manager=manager,
        base_payload={
            **base,
            "selection_attempt_index": 2,
            "selection_holdout_reuse_count": 1,
        },
        statistical_validation_contract=contract,
    )

    assert first["accepted"] is True
    assert stale_second["accepted"] is False
    assert "selection_attempt_index_mismatch" in stale_second["reasons"]
    assert "selection_holdout_reuse_count_mismatch" in stale_second["reasons"]
    assert current_second["accepted"] is True


def test_concurrent_same_holdout_reservations_allow_exactly_one(tmp_path) -> None:
    _, _, manager = frozen_manifest_and_manager(tmp_path)
    base = {
        "experiment_family_id": "concurrent-family",
        "hypothesis_id": "concurrent-hypothesis",
        "pre_exposure_reservation_key_hash": "sha256:" + "d" * 64,
        "selection_artifact_hash": "sha256:" + "e" * 64,
        "selected_candidate_id": "candidate-a",
    }
    contract = {"gates": {"max_holdout_reuse_count": 0}}

    def reserve():
        return reserve_research_attempt_checked(
            manager=manager,
            base_payload=base,
            statistical_validation_contract=contract,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: reserve(), range(2)))

    assert sum(result["accepted"] is True for result in results) == 1


def test_registry_cli_rejects_incomplete_attempt_lifecycle(tmp_path) -> None:
    _, _, manager = frozen_manifest_and_manager(tmp_path)
    experiment_id = "incomplete-registry-attempt"
    reserve_research_attempt(
        manager=manager,
        base_payload={
            "experiment_id": experiment_id,
            "experiment_family_id": "incomplete-family",
            "hypothesis_id": "incomplete-hypothesis",
            "pre_exposure_reservation_key_hash": "sha256:" + "7" * 64,
        },
    )
    output: list[str] = []
    context = ResearchAppContext(
        settings=manager.settings,
        paths=manager,
        printer=output.append,
    )

    result = cmd_research_registry_validate(
        context=context,
        experiment_id=experiment_id,
    )

    payload = json.loads(output[-1])
    assert result == 1
    assert payload["ok"] is False
    assert (
        "experiment_registry_incomplete_attempt"
        in payload["registry_lifecycle_summary"][0]["reasons"]
    )
