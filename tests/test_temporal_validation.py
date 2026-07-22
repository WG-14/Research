from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError

import pytest

from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research.data_plane import walk_forward_payload
from market_research.research.temporal_validation import (
    TEMPORAL_VALIDATION_HASH_LABEL,
    NestedTemporalValidationConfig,
    TemporalValidationError,
    build_nested_temporal_validation_plan,
    parse_nested_temporal_validation_plan,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.validation_protocol import (
    ResearchValidationError,
    _admitted_walk_forward_windows,
)
from market_research.research_composition import parse_builtin_manifest
from tests.test_research_semantics_v2_contract import _manifest_payload


SOURCE_HASH = "sha256:" + "a" * 64


def _config(**overrides: int) -> NestedTemporalValidationConfig:
    values = {
        "schema_version": 1,
        "label_horizon_days": 2,
        "purge_days": 2,
        "embargo_days": 1,
        "inner_fold_count": 2,
        "inner_test_window_days": 5,
        "min_inner_train_window_days": 10,
    }
    values.update(overrides)
    return NestedTemporalValidationConfig(**values)


def _windows() -> list[dict[str, dict[str, str]]]:
    return [
        {
            "train": {"start": "2025-01-01", "end": "2025-02-28"},
            "test": {"start": "2025-03-01", "end": "2025-03-10"},
        },
        {
            "train": {"start": "2025-01-11", "end": "2025-03-10"},
            "test": {"start": "2025-03-11", "end": "2025-03-20"},
        },
    ]


def _manifest_payload_with_temporal_validation(
    *, step_days: int = 10
) -> dict[str, object]:
    payload = _manifest_payload()
    dataset = dict(payload["dataset"])
    dataset["train"] = {"start": "2025-01-01", "end": "2025-03-31"}
    dataset["validation"] = {"start": "2025-04-01", "end": "2025-05-31"}
    payload["dataset"] = dataset
    payload["walk_forward"] = {
        "train_window_days": 40,
        "test_window_days": 10,
        "step_days": step_days,
        "min_windows": 2,
        "temporal_validation": _config().as_dict(),
    }
    return payload


def test_nested_plan_is_immutable_deterministic_and_round_trips() -> None:
    plan = build_nested_temporal_validation_plan(
        windows=_windows(), source_binding_hash=SOURCE_HASH, config=_config()
    )
    repeated = build_nested_temporal_validation_plan(
        windows=_windows(), source_binding_hash=SOURCE_HASH, config=_config()
    )

    assert plan.contract_hash() == repeated.contract_hash()
    assert parse_nested_temporal_validation_plan(plan.as_dict()) == plan
    assert len(plan.outer_folds) == 2
    assert all(len(fold.inner_splits) == 2 for fold in plan.outer_folds)
    first = plan.outer_folds[0].outer_split
    assert first.train.end == "2025-02-25"
    assert first.purge.as_dict() == {
        "start": "2025-02-26",
        "end": "2025-02-27",
    }
    assert first.embargo.as_dict() == {
        "start": "2025-02-28",
        "end": "2025-02-28",
    }
    with pytest.raises(FrozenInstanceError):
        plan.source_binding_hash = "sha256:" + "b" * 64  # type: ignore[misc]


def test_plan_hash_and_label_horizon_tampering_fail_closed() -> None:
    plan = build_nested_temporal_validation_plan(
        windows=_windows(), source_binding_hash=SOURCE_HASH, config=_config()
    )
    bad_hash = copy.deepcopy(plan.as_dict())
    bad_hash["content_hash"] = "sha256:" + "0" * 64
    with pytest.raises(
        TemporalValidationError, match="temporal_validation_plan_hash_mismatch"
    ):
        parse_nested_temporal_validation_plan(bad_hash)

    leakage = copy.deepcopy(plan.as_dict())
    leakage["label_intervals"][0]["label_end"] = "2025-03-01"
    with pytest.raises(
        TemporalValidationError, match="temporal_label_horizon_mismatch"
    ):
        parse_nested_temporal_validation_plan(leakage)


def _rehash_plan_payload(payload: dict[str, object]) -> None:
    labels = payload["label_intervals"]
    assert isinstance(labels, list)
    payload["label_intervals_hash"] = sha256_prefixed(
        labels,
        label="temporal_label_intervals",
    )
    canonical = {key: value for key, value in payload.items() if key != "content_hash"}
    payload["content_hash"] = sha256_prefixed(
        canonical,
        label=TEMPORAL_VALIDATION_HASH_LABEL,
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("inner_id", "nested_temporal_inner_split_ids_invalid"),
        ("missing_label", "temporal_label_interval_calendar_incomplete"),
        ("wrong_horizon", "temporal_label_horizon_mismatch"),
        ("overlapping_inner", "nested_temporal_inner_test_ranges_overlap"),
    ),
)
def test_rehashed_forged_plan_cannot_bypass_temporal_contract(
    mutation: str,
    reason: str,
) -> None:
    plan = build_nested_temporal_validation_plan(
        windows=_windows(), source_binding_hash=SOURCE_HASH, config=_config()
    )
    forged = copy.deepcopy(plan.as_dict())
    folds = forged["outer_folds"]
    labels = forged["label_intervals"]
    assert isinstance(folds, list)
    assert isinstance(labels, list)
    first_fold = folds[0]
    assert isinstance(first_fold, dict)
    inner = first_fold["inner_splits"]
    assert isinstance(inner, list)
    assert isinstance(inner[0], dict)
    assert isinstance(inner[1], dict)
    if mutation == "inner_id":
        inner[0]["split_id"] = "renamed_001"
    elif mutation == "missing_label":
        labels.pop()
    elif mutation == "wrong_horizon":
        assert isinstance(labels[-1], dict)
        labels[-1]["label_end"] = "2025-03-23"
    else:
        duplicated = copy.deepcopy(inner[0])
        duplicated["split_id"] = "inner_001_002"
        inner[1] = duplicated
    _rehash_plan_payload(forged)

    with pytest.raises(TemporalValidationError, match=reason):
        parse_nested_temporal_validation_plan(forged)


def test_config_rejects_purge_shorter_than_label_horizon() -> None:
    with pytest.raises(
        TemporalValidationError,
        match="temporal_validation_purge_shorter_than_label_horizon",
    ):
        _config(label_horizon_days=3, purge_days=2)


def test_overlapping_outer_test_ranges_are_rejected() -> None:
    overlapping = copy.deepcopy(_windows())
    overlapping[1] = {
        "train": {"start": "2025-01-06", "end": "2025-03-05"},
        "test": {"start": "2025-03-06", "end": "2025-03-15"},
    }
    with pytest.raises(
        TemporalValidationError, match="nested_temporal_outer_test_ranges_overlap"
    ):
        build_nested_temporal_validation_plan(
            windows=overlapping,
            source_binding_hash=SOURCE_HASH,
            config=_config(),
        )


def test_manifest_hash_binds_explicit_temporal_validation_without_legacy_drift() -> (
    None
):
    temporal_payload = _manifest_payload_with_temporal_validation()
    temporal = parse_builtin_manifest(temporal_payload)
    legacy_payload = copy.deepcopy(temporal_payload)
    legacy_walk_forward = dict(legacy_payload["walk_forward"])
    legacy_walk_forward.pop("temporal_validation")
    legacy_payload["walk_forward"] = legacy_walk_forward
    legacy = parse_builtin_manifest(legacy_payload)

    assert temporal.walk_forward is not None
    assert temporal.walk_forward.temporal_validation == _config()
    assert "temporal_validation" in temporal.walk_forward.as_dict()
    assert "temporal_validation" not in legacy.walk_forward.as_dict()
    assert temporal.manifest_hash() != legacy.manifest_hash()


def test_manifest_rejects_unknown_temporal_validation_fields() -> None:
    payload = _manifest_payload_with_temporal_validation()
    walk_forward = dict(payload["walk_forward"])
    temporal = dict(walk_forward["temporal_validation"])
    temporal["legacy_gap_days"] = 3
    walk_forward["temporal_validation"] = temporal
    payload["walk_forward"] = walk_forward

    with pytest.raises(ManifestValidationError, match="unknown:legacy_gap_days"):
        parse_builtin_manifest(payload)


def test_walk_forward_admission_uses_only_purged_outer_train_ranges() -> None:
    manifest = parse_builtin_manifest(_manifest_payload_with_temporal_validation())
    windows, plan = _admitted_walk_forward_windows(manifest)
    readiness = walk_forward_payload(manifest)

    assert plan is not None
    assert windows[0]["train"].end == plan.outer_folds[0].outer_split.train.end
    assert windows[0]["test"].start == plan.outer_folds[0].outer_split.test.start
    assert plan.outer_folds[0].outer_split.purge.day_count == 2
    assert plan.outer_folds[0].outer_split.embargo.day_count == 1
    assert readiness["nested_temporal_validation"]["plan_hash"] == (
        plan.contract_hash()
    )


def test_walk_forward_admission_fails_before_loading_overlapping_test_ranges() -> None:
    manifest = parse_builtin_manifest(
        _manifest_payload_with_temporal_validation(step_days=5)
    )
    with pytest.raises(
        ResearchValidationError,
        match="nested_temporal_validation_admission_failed:"
        "nested_temporal_outer_test_ranges_overlap",
    ):
        _admitted_walk_forward_windows(manifest)
    readiness = walk_forward_payload(manifest)
    assert readiness["status"] == "FAIL"
    assert readiness["reasons"] == [
        "nested_temporal_validation_invalid:nested_temporal_outer_test_ranges_overlap"
    ]
