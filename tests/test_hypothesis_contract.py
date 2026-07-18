from __future__ import annotations

import copy

import pytest

from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research.experiment_registry import (
    research_freedom_hash,
    research_identity_from_manifest,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research_composition import parse_builtin_manifest
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2
from tests.test_research_semantics_v2_contract import _manifest_payload


def _hypothesis_spec(**overrides):
    return hypothesis_spec_v2(**overrides)


def _structured_manifest_payload():
    payload = copy.deepcopy(_manifest_payload())
    payload["parameter_space"]["NOOP_DECISION_REASON"] = ["noop_baseline_hold"]
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


def _enabled_validation_risk_policy():
    return {
        "schema_version": 1,
        "max_daily_loss_krw": 1_000_000,
        "max_position_loss_pct": 100,
        "max_drawdown_pct": 100,
        "max_daily_order_count": 1_000,
        "max_trade_count_per_day": 1_000,
        "cooldown_after_loss_min": 0,
        "max_open_positions": 1,
        "unresolved_order_policy": "block",
        "policy_status": "enabled",
        "missing_policy": "fail_closed_for_validation",
        "source": "manifest",
    }


@pytest.mark.parametrize(
    "field",
    (
        "hypothesis_id",
        "version",
        "phenomenon",
        "mechanism",
        "observation_conditions",
        "comparison_target",
        "falsification_criteria",
        "experiment_family_id",
        "hypothesis_text",
        "actor_id",
        "created_at",
        "observations",
        "research_question",
        "research_question_ref",
        "observation_refs",
    ),
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
    second_payload["hypothesis_spec"] = _hypothesis_spec(version="2.0.0")
    first = parse_builtin_manifest(first_payload)
    second = parse_builtin_manifest(second_payload)
    assert first.manifest_hash() != second.manifest_hash()
    assert research_identity_from_manifest(first)["hypothesis_version"] == "1.0.0"
    assert research_identity_from_manifest(second)["hypothesis_version"] == "2.0.0"
    assert research_freedom_hash(
        research_identity_from_manifest(first)
    ) != research_freedom_hash(research_identity_from_manifest(second))


def test_semantic_fingerprint_ignores_labels_and_formatting_but_changes_with_claim():
    first_payload = _structured_manifest_payload()
    alias_payload = copy.deepcopy(first_payload)
    alias_payload["hypothesis_spec"] = _hypothesis_spec(
        hypothesis_id="another-label",
        version="9.0.0",
        experiment_family_id="another-family",
        phenomenon="SMA CROSSOVERS have positive conditional expectancy.",
    )
    changed_payload = copy.deepcopy(first_payload)
    changed_payload["hypothesis_spec"] = _hypothesis_spec(
        mechanism="A materially different causal mechanism."
    )

    first = parse_builtin_manifest(first_payload).hypothesis_spec
    alias = parse_builtin_manifest(alias_payload).hypothesis_spec
    changed = parse_builtin_manifest(changed_payload).hypothesis_spec
    assert first is not None and alias is not None and changed is not None
    assert first.semantic_fingerprint() == alias.semantic_fingerprint()
    assert first.semantic_fingerprint() != changed.semantic_fingerprint()
    assert (
        research_identity_from_manifest(parse_builtin_manifest(first_payload))[
            "hypothesis_semantic_fingerprint"
        ]
        == first.semantic_fingerprint()
    )


def test_legacy_hypothesis_is_explicitly_unregistered_and_not_pre_registered():
    manifest = parse_builtin_manifest(_manifest_payload())
    identity = research_identity_from_manifest(manifest)
    assert identity["hypothesis_status"] == "unregistered"
    assert identity["pre_registration_verified"] is False
    assert identity["hypothesis_identity_source"] == "legacy_manifest.hypothesis"


def test_pre_registration_requires_complete_evidence():
    payload = _manifest_payload()
    payload["hypothesis_spec"] = _hypothesis_spec(registration_status="pre_registered")
    with pytest.raises(
        ManifestValidationError, match="pre_registered hypothesis requires"
    ):
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


def test_schema_one_is_readable_only_for_legacy_research_only_manifests():
    legacy = {
        key: value
        for key, value in _hypothesis_spec().items()
        if key
        not in {
            "hypothesis_text",
            "actor_id",
            "created_at",
            "observations",
            "research_question",
            "research_question_ref",
            "observation_refs",
        }
    }
    legacy["schema_version"] = 1
    legacy.pop("pre_registered_at")
    legacy.pop("registration_evidence_hash")
    payload = _structured_manifest_payload()
    payload["hypothesis_spec"] = legacy

    assert parse_builtin_manifest(payload).hypothesis_spec.schema_version == 1

    payload["research_classification"] = "exploratory"
    with pytest.raises(ManifestValidationError, match="schema_version 2"):
        parse_builtin_manifest(payload)


def test_schema_one_pre_registration_fails_closed_even_for_research_only():
    payload = _structured_manifest_payload()
    legacy = {
        key: value
        for key, value in _hypothesis_spec().items()
        if key
        not in {
            "hypothesis_text",
            "actor_id",
            "created_at",
            "observations",
            "research_question",
            "research_question_ref",
            "observation_refs",
        }
    }
    legacy.update(
        {
            "schema_version": 1,
            "registration_status": "pre_registered",
            "pre_registered_at": "2026-01-01T00:00:00+00:00",
            "registration_evidence_hash": "sha256:" + "b" * 64,
        }
    )
    payload["hypothesis_spec"] = legacy

    with pytest.raises(ManifestValidationError, match="schema_version 2"):
        parse_builtin_manifest(payload)


def test_schema_one_fails_closed_for_validation_bound_manifest():
    payload = _structured_manifest_payload()
    legacy = {
        key: value
        for key, value in _hypothesis_spec().items()
        if key
        not in {
            "hypothesis_text",
            "actor_id",
            "created_at",
            "observations",
            "research_question",
            "research_question_ref",
            "observation_refs",
        }
    }
    legacy["schema_version"] = 1
    legacy.pop("pre_registered_at")
    legacy.pop("registration_evidence_hash")
    payload["hypothesis_spec"] = legacy
    payload["research_classification"] = "validated_candidate"

    with pytest.raises(ManifestValidationError, match="schema_version 2"):
        parse_builtin_manifest(payload)


def test_lineage_rejects_missing_mismatched_and_orphan_observation_refs():
    missing = _structured_manifest_payload()
    missing["hypothesis_spec"]["research_question"]["observation_refs"] = []
    with pytest.raises(ManifestValidationError, match="non-empty array"):
        parse_builtin_manifest(missing)

    mismatched = _structured_manifest_payload()
    mismatched["hypothesis_spec"]["observation_refs"][0]["observation_hash"] = (
        "sha256:" + "f" * 64
    )
    with pytest.raises(ManifestValidationError, match="observation hash mismatch"):
        parse_builtin_manifest(mismatched)

    orphan = _structured_manifest_payload()
    extra = copy.deepcopy(orphan["hypothesis_spec"]["observations"][0])
    extra["observation_id"] = "obs-orphan"
    orphan["hypothesis_spec"]["observations"].append(extra)
    with pytest.raises(ManifestValidationError, match="resolve every observation"):
        parse_builtin_manifest(orphan)


def test_lineage_rejects_question_hash_and_manifest_target_mismatches():
    question_mismatch = _structured_manifest_payload()
    question_mismatch["hypothesis_spec"]["research_question_ref"]["question_hash"] = (
        "sha256:" + "f" * 64
    )
    with pytest.raises(ManifestValidationError, match="question_ref hash mismatch"):
        parse_builtin_manifest(question_mismatch)

    target_mismatch = _structured_manifest_payload()
    target_mismatch["hypothesis_spec"]["observations"][0]["market"] = "KRW-ETH"
    observation = target_mismatch["hypothesis_spec"]["observations"][0]
    observation_hash = sha256_prefixed(observation)
    target_mismatch["hypothesis_spec"]["observation_refs"][0]["observation_hash"] = (
        observation_hash
    )
    target_mismatch["hypothesis_spec"]["research_question"]["observation_refs"][0][
        "observation_hash"
    ] = observation_hash
    target_mismatch["hypothesis_spec"]["research_question_ref"]["question_hash"] = (
        sha256_prefixed(target_mismatch["hypothesis_spec"]["research_question"])
    )
    with pytest.raises(ManifestValidationError, match="manifest market"):
        parse_builtin_manifest(target_mismatch)


def test_competing_hypotheses_share_question_without_overwriting_failed_sibling():
    competitors = [
        {
            "hypothesis_id": "sma-uptrend-edge",
            "version": "1.0.0",
            "hypothesis_text": "SMA crossovers have positive expectancy after costs.",
        },
        {
            "hypothesis_id": "sma-volatility-edge",
            "version": "1.0.0",
            "hypothesis_text": "Volatility contraction explains the conditional edge.",
        },
    ]
    failed_payload = _structured_manifest_payload()
    failed_payload["hypothesis_spec"] = hypothesis_spec_v2(
        hypothesis_id="sma-uptrend-edge",
        hypothesis_text=competitors[0]["hypothesis_text"],
        registration_status="rejected",
        competing_hypotheses=competitors,
    )
    sibling_payload = _structured_manifest_payload()
    sibling_payload["hypothesis_spec"] = hypothesis_spec_v2(
        hypothesis_id="sma-volatility-edge",
        hypothesis_text=competitors[1]["hypothesis_text"],
        competing_hypotheses=competitors,
    )

    failed = parse_builtin_manifest(failed_payload).hypothesis_spec
    sibling = parse_builtin_manifest(sibling_payload).hypothesis_spec
    assert failed is not None and sibling is not None
    assert failed.research_question is not None
    assert sibling.research_question is not None
    assert failed.research_question.contract_hash() == (
        sibling.research_question.contract_hash()
    )
    assert failed.registration_status == "rejected"
    assert sibling.registration_status == "unregistered"
    assert failed.hypothesis_id != sibling.hypothesis_id
    assert failed.contract_hash() != sibling.contract_hash()


def test_validation_bound_manifest_requires_structured_hypothesis():
    payload = _manifest_payload()
    payload["research_classification"] = "validated_candidate"
    with pytest.raises(ManifestValidationError, match="hypothesis_spec is required"):
        parse_builtin_manifest(payload)


def test_validation_bound_manifest_requires_final_holdout_split():
    payload = _structured_manifest_payload()
    payload["research_classification"] = "validated_candidate"
    payload["risk_policy"] = _enabled_validation_risk_policy()

    with pytest.raises(
        ManifestValidationError,
        match="dataset.final_holdout is required",
    ):
        parse_builtin_manifest(payload)


def test_validation_bound_manifest_cannot_disable_final_holdout_gate():
    payload = _structured_manifest_payload()
    payload["research_classification"] = "validated_candidate"
    payload["dataset"]["final_holdout"] = {
        "start": "2026-01-03",
        "end": "2026-01-03",
    }
    payload["acceptance_gate"]["final_holdout_required_for_validation"] = False
    payload["risk_policy"] = _enabled_validation_risk_policy()

    with pytest.raises(
        ManifestValidationError,
        match="final_holdout_required_for_validation must be true",
    ):
        parse_builtin_manifest(payload)
