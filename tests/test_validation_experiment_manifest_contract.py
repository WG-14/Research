from __future__ import annotations

import copy
from typing import Any

import pytest

from market_research.research.experiment_manifest import (
    ManifestValidationError,
    ValidationExperimentExecutionContract,
    ValidationProviderDatasetRef,
)
from market_research.research_composition import parse_builtin_manifest
from tests.test_research_semantics_v2_contract import _manifest_payload


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
PROVIDER_A_URI = "/external/provider-a/artifact.manifest.json"
PROVIDER_B_URI = "/external/provider-b/artifact.manifest.json"


def _validation_experiments_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "nested_selection": {
            "metric_id": "return_pct",
            "direction": "MAXIMIZE",
            "minimum_inner_sample_count": 20,
            "minimum_outer_sample_count": 20,
            "terminal_selection_rule": "outer_mean_then_candidate_id",
        },
        "falsification": {
            "observation_builder_id": ("lagged_strategy_exposure_next_bar_return_v1"),
            "policy_id": "confirmatory-controls",
            "version": "1",
            "seed": 17,
            "placebo_shift": 2,
            "minimum_sample_count": 20,
            "minimum_baseline_abs_effect": 0.2,
            "maximum_control_abs_effect": 0.1,
            "minimum_confounder_adjusted_retention": 0.5,
            "include_confounder_adjusted": True,
        },
        "factor_exposure": {
            "observation_builder_id": ("strategy_period_return_market_factor_v1"),
            "model_id": "ols_newey_west",
            "model_version": "1",
            "hac_lags": 2,
            "confidence_z": 1.96,
        },
        "provider_sensitivity": {
            "selected_provider_id": "provider-a",
            "provider_datasets": [
                {
                    "provider_id": "provider-b",
                    "artifact_manifest_uri": PROVIDER_B_URI,
                    "artifact_manifest_hash": HASH_B,
                },
                {
                    "provider_id": "provider-a",
                    "artifact_manifest_uri": PROVIDER_A_URI,
                    "artifact_manifest_hash": HASH_A,
                },
            ],
            "semantic_definition_id": ("selected_candidate_required_metrics_v1"),
            "metrics": ["return_pct", "max_drawdown_pct"],
            "tolerances": [
                {
                    "metric_id": "return_pct",
                    "absolute_tolerance": 0.1,
                    "relative_tolerance": 0.05,
                },
                {
                    "metric_id": "max_drawdown_pct",
                    "absolute_tolerance": 0.2,
                    "relative_tolerance": 0.1,
                },
            ],
        },
    }


def _payload_with_validation_experiments() -> dict[str, Any]:
    payload = _manifest_payload()
    dataset = payload["dataset"]
    assert isinstance(dataset, dict)
    dataset.update(
        {
            "source": "frozen_sqlite_candles",
            "artifact_manifest_uri": PROVIDER_A_URI,
            "artifact_manifest_hash": HASH_A,
        }
    )
    payload["validation_experiments"] = _validation_experiments_payload()
    return payload


def _nested(value: dict[str, Any], *path: str) -> dict[str, Any]:
    current = value
    for part in path:
        child = current[part]
        assert isinstance(child, dict)
        current = child
    return current


def test_validation_experiments_is_optional_and_absent_from_legacy_hash_domain() -> (
    None
):
    manifest = parse_builtin_manifest(_manifest_payload())

    assert manifest.validation_experiments is None
    assert "validation_experiments" not in manifest.canonical_payload()


def test_validation_experiments_parses_canonicalizes_and_binds_manifest_hash() -> None:
    payload = _payload_with_validation_experiments()
    manifest = parse_builtin_manifest(payload)

    contract = manifest.validation_experiments
    assert isinstance(contract, ValidationExperimentExecutionContract)
    assert isinstance(
        contract.provider_sensitivity.provider_datasets[0],
        ValidationProviderDatasetRef,
    )
    canonical = manifest.canonical_payload()["validation_experiments"]
    assert isinstance(canonical, dict)
    provider = canonical["provider_sensitivity"]
    assert isinstance(provider, dict)
    assert [item["provider_id"] for item in provider["provider_datasets"]] == [
        "provider-a",
        "provider-b",
    ]
    assert provider["metrics"] == ["max_drawdown_pct", "return_pct"]
    assert [item["metric_id"] for item in provider["tolerances"]] == [
        "max_drawdown_pct",
        "return_pct",
    ]

    reordered = copy.deepcopy(payload)
    reordered_provider = _nested(
        reordered, "validation_experiments", "provider_sensitivity"
    )
    reordered_provider["provider_datasets"].reverse()
    reordered_provider["metrics"].reverse()
    reordered_provider["tolerances"].reverse()
    assert parse_builtin_manifest(reordered).manifest_hash() == manifest.manifest_hash()

    changed = copy.deepcopy(payload)
    _nested(changed, "validation_experiments", "falsification")[
        "maximum_control_abs_effect"
    ] = 0.11
    assert parse_builtin_manifest(changed).manifest_hash() != manifest.manifest_hash()


@pytest.mark.parametrize(
    ("section_path", "mutation", "match"),
    [
        (
            (),
            lambda section: section.__setitem__("schema_version", 2),
            r"validation_experiments\.schema_version must be 1",
        ),
        (
            (),
            lambda section: section.__setitem__("network_source", "forbidden"),
            r"validation_experiments unsupported fields: network_source",
        ),
        (
            ("factor_exposure",),
            lambda section: section.pop("hac_lags"),
            r"factor_exposure missing required fields: hac_lags",
        ),
        (
            ("nested_selection",),
            lambda section: section.__setitem__("minimum_inner_sample_count", True),
            r"minimum_inner_sample_count must be an integer",
        ),
        (
            ("falsification",),
            lambda section: section.__setitem__("include_confounder_adjusted", 1),
            r"include_confounder_adjusted must be a boolean",
        ),
    ],
)
def test_validation_experiments_rejects_schema_drift_and_implicit_coercion(
    section_path: tuple[str, ...],
    mutation: Any,
    match: str,
) -> None:
    payload = _payload_with_validation_experiments()
    section = _nested(payload, "validation_experiments", *section_path)
    mutation(section)

    with pytest.raises(ManifestValidationError, match=match):
        parse_builtin_manifest(payload)


@pytest.mark.parametrize(
    ("section_name", "field_name", "value"),
    [
        ("nested_selection", "metric_id", "caller_metric"),
        ("nested_selection", "direction", "MINIMIZE"),
        (
            "nested_selection",
            "terminal_selection_rule",
            "caller_selected_candidate",
        ),
        ("falsification", "observation_builder_id", "python:custom.builder"),
        ("factor_exposure", "observation_builder_id", "python:custom.builder"),
        ("factor_exposure", "model_id", "caller_model"),
    ],
)
def test_validation_experiments_rejects_open_execution_identifiers(
    section_name: str,
    field_name: str,
    value: str,
) -> None:
    payload = _payload_with_validation_experiments()
    _nested(payload, "validation_experiments", section_name)[field_name] = value

    with pytest.raises(ManifestValidationError, match=r"must be one of"):
        parse_builtin_manifest(payload)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (
            lambda provider: provider["provider_datasets"].pop(),
            r"provider_datasets must contain at least two entries",
        ),
        (
            lambda provider: provider.__setitem__(
                "selected_provider_id", "provider-missing"
            ),
            r"selected_provider_id must name a provider dataset",
        ),
        (
            lambda provider: provider["provider_datasets"][0].__setitem__(
                "artifact_manifest_uri", "relative/artifact.manifest.json"
            ),
            r"artifact_manifest_uri must be absolute",
        ),
        (
            lambda provider: provider["provider_datasets"][0].__setitem__(
                "artifact_manifest_hash", "sha256:short"
            ),
            r"artifact_manifest_hash must be a sha256: hash",
        ),
        (
            lambda provider: provider["tolerances"].pop(),
            r"tolerances must cover exactly the declared metrics",
        ),
    ],
)
def test_validation_provider_inputs_fail_closed(
    mutate: Any,
    match: str,
) -> None:
    payload = _payload_with_validation_experiments()
    provider = _nested(payload, "validation_experiments", "provider_sensitivity")
    mutate(provider)

    with pytest.raises(ManifestValidationError, match=match):
        parse_builtin_manifest(payload)


def test_selected_provider_artifact_must_be_the_manifest_dataset() -> None:
    payload = _payload_with_validation_experiments()
    provider = _nested(payload, "validation_experiments", "provider_sensitivity")
    selected = next(
        item
        for item in provider["provider_datasets"]
        if item["provider_id"] == "provider-a"
    )
    selected["artifact_manifest_hash"] = HASH_B

    with pytest.raises(
        ManifestValidationError,
        match=r"selected_provider_id artifact must match manifest dataset",
    ):
        parse_builtin_manifest(payload)
