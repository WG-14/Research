from __future__ import annotations

import pytest

from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research_composition import parse_builtin_manifest
from tests.test_hypothesis_contract import _structured_manifest_payload
from tests.test_research_semantics_v2_contract import _manifest_payload


def _complete_payload():
    return _structured_manifest_payload()


@pytest.mark.parametrize("field", ("strategy_version", "execution_timing", "portfolio_policy", "risk_policy"))
def test_structured_manifest_rejects_implicit_core_experiment_contract(field):
    payload = _complete_payload()
    payload.pop(field)
    with pytest.raises(ManifestValidationError, match=f"explicit contract field.*{field}"):
        parse_builtin_manifest(payload)


def test_structured_manifest_canonical_payload_contains_all_core_contracts():
    manifest = parse_builtin_manifest(_complete_payload())
    canonical = manifest.canonical_payload()
    assert canonical["strategy_version"] == "noop_baseline.research_contract.v1"
    assert canonical["execution_timing"]["fill_reference_policy"] == "next_candle_open"
    assert canonical["portfolio_policy"]["source"] == "manifest"
    assert canonical["risk_policy"]["policy_status"] == "disabled_explicit"


def test_legacy_research_only_manifest_remains_loadable_but_has_no_structured_contract():
    manifest = parse_builtin_manifest(_manifest_payload())
    assert manifest.hypothesis_spec is None
    assert manifest.strategy_version is None
