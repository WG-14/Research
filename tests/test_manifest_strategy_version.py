from __future__ import annotations

import pytest

from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research_composition import builtin_strategy_registry, parse_builtin_manifest
from tests.test_hypothesis_contract import _structured_manifest_payload
from tests.test_research_semantics_v2_contract import _manifest_payload


def test_declared_strategy_version_is_canonical_and_hash_bound():
    payload = _manifest_payload()
    version = builtin_strategy_registry().resolve("noop_baseline").version
    payload["strategy_version"] = version
    declared = parse_builtin_manifest(payload)
    legacy = parse_builtin_manifest(_manifest_payload())
    assert declared.strategy_version == version
    assert declared.canonical_payload()["strategy_version"] == version
    assert declared.manifest_hash() != legacy.manifest_hash()


def test_manifest_rejects_registered_strategy_version_mismatch():
    payload = _manifest_payload()
    payload["strategy_version"] = "noop_baseline.incompatible.v999"
    with pytest.raises(ManifestValidationError, match="does not match registered strategy"):
        parse_builtin_manifest(payload)


def test_validation_bound_manifest_requires_strategy_version_after_hypothesis_contract():
    payload = _structured_manifest_payload()
    payload["research_classification"] = "validated_candidate"
    payload.pop("strategy_version")
    with pytest.raises(ManifestValidationError, match="explicit contract field.*strategy_version"):
        parse_builtin_manifest(payload)
