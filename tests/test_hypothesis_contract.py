from __future__ import annotations

import copy

import pytest

from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research.experiment_registry import (
    research_freedom_hash,
    research_identity_from_manifest,
)
from market_research.research_composition import parse_builtin_manifest
from tests.test_research_semantics_v2_contract import _manifest_payload


def _hypothesis_spec(**overrides):
    payload = {
        "schema_version": 1,
        "hypothesis_id": "sma-uptrend-edge",
        "version": "1.0.0",
        "phenomenon": "SMA crossovers have positive conditional expectancy.",
        "mechanism": "Trend persistence delays price adjustment after crossovers.",
        "observation_conditions": ["uptrend", "sufficient candle coverage"],
        "comparison_target": "cash",
        "falsification_criteria": ["validation return is not positive"],
        "experiment_family_id": "sma-uptrend-family",
        "registration_status": "unregistered",
    }
    payload.update(overrides)
    return payload


def _structured_manifest_payload():
    payload = copy.deepcopy(_manifest_payload())
    payload["hypothesis_spec"] = _hypothesis_spec()
    payload["strategy_version"] = "noop_baseline.research_contract.v1"
    payload["execution_timing"] = {
        "fill_reference_policy": "next_candle_open",
        "allow_same_candle_close_fill": False,
        "min_execution_reality_level_for_validation": "candle_next_open",
    }
    payload["portfolio_policy"] = {
        "schema_version": 1,
        "starting_cash_krw": 1_000_000,
        "quote_currency": "KRW",
        "initial_position_qty": 0,
        "cash_interest_policy": "zero",
        "position_sizing": {
            "type": "fractional_cash",
            "buy_fraction": 0.99,
            "sell_policy": "sell_all_available_position",
            "cash_buffer_policy": "retain_1_percent_before_fees",
            "min_order_krw": None,
            "max_order_krw": None,
            "rounding_policy": "engine_float_no_exchange_lot_rounding",
        },
        "source": "manifest",
    }
    payload["risk_policy"] = {
        "schema_version": 1,
        "disabled": True,
        "source": "manifest",
    }
    return payload


@pytest.mark.parametrize(
    "field",
    ("hypothesis_id", "version", "phenomenon", "mechanism", "observation_conditions",
     "comparison_target", "falsification_criteria", "experiment_family_id"),
)
def test_structured_hypothesis_rejects_missing_required_field(field):
    payload = _manifest_payload()
    spec = _hypothesis_spec()
    spec.pop(field)
    payload["hypothesis_spec"] = spec
    with pytest.raises(ManifestValidationError, match=f"hypothesis_spec.{field}"):
        parse_builtin_manifest(payload)


def test_hypothesis_version_changes_manifest_hash_and_registry_identity():
    first_payload = _structured_manifest_payload()
    second_payload = copy.deepcopy(first_payload)
    second_payload["hypothesis_spec"]["version"] = "2.0.0"
    first = parse_builtin_manifest(first_payload)
    second = parse_builtin_manifest(second_payload)
    assert first.manifest_hash() != second.manifest_hash()
    assert research_identity_from_manifest(first)["hypothesis_version"] == "1.0.0"
    assert research_identity_from_manifest(second)["hypothesis_version"] == "2.0.0"
    assert research_freedom_hash(research_identity_from_manifest(first)) != research_freedom_hash(
        research_identity_from_manifest(second)
    )


def test_semantic_fingerprint_ignores_labels_and_formatting_but_changes_with_claim():
    first_payload = _structured_manifest_payload()
    alias_payload = copy.deepcopy(first_payload)
    alias_payload["hypothesis_spec"]["hypothesis_id"] = "another-label"
    alias_payload["hypothesis_spec"]["version"] = "9.0.0"
    alias_payload["hypothesis_spec"]["experiment_family_id"] = "another-family"
    alias_payload["hypothesis_spec"]["phenomenon"] = "  SMA CROSSOVERS  have positive conditional expectancy. "
    changed_payload = copy.deepcopy(first_payload)
    changed_payload["hypothesis_spec"]["mechanism"] = "A materially different causal mechanism."

    first = parse_builtin_manifest(first_payload).hypothesis_spec
    alias = parse_builtin_manifest(alias_payload).hypothesis_spec
    changed = parse_builtin_manifest(changed_payload).hypothesis_spec
    assert first is not None and alias is not None and changed is not None
    assert first.semantic_fingerprint() == alias.semantic_fingerprint()
    assert first.semantic_fingerprint() != changed.semantic_fingerprint()
    assert research_identity_from_manifest(parse_builtin_manifest(first_payload))[
        "hypothesis_semantic_fingerprint"
    ] == first.semantic_fingerprint()


def test_legacy_hypothesis_is_explicitly_unregistered_and_not_pre_registered():
    manifest = parse_builtin_manifest(_manifest_payload())
    identity = research_identity_from_manifest(manifest)
    assert identity["hypothesis_status"] == "unregistered"
    assert identity["pre_registration_verified"] is False
    assert identity["hypothesis_identity_source"] == "legacy_manifest.hypothesis"


def test_pre_registration_requires_complete_evidence():
    payload = _manifest_payload()
    payload["hypothesis_spec"] = _hypothesis_spec(registration_status="pre_registered")
    with pytest.raises(ManifestValidationError, match="pre_registered hypothesis requires"):
        parse_builtin_manifest(payload)


def test_pre_registration_evidence_is_bound_to_identity():
    payload = _structured_manifest_payload()
    payload["hypothesis_spec"] = _hypothesis_spec(
        registration_status="pre_registered",
        pre_registered_at="2026-01-01T00:00:00+00:00",
        registration_evidence_hash="sha256:" + "a" * 64,
    )
    identity = research_identity_from_manifest(parse_builtin_manifest(payload))
    assert identity["pre_registration_verified"] is True
    assert identity["registration_evidence_hash"] == "sha256:" + "a" * 64


def test_validation_bound_manifest_requires_structured_hypothesis():
    payload = _manifest_payload()
    payload["research_classification"] = "validated_candidate"
    with pytest.raises(ManifestValidationError, match="hypothesis_spec is required"):
        parse_builtin_manifest(payload)
