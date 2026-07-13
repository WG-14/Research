import copy

import pytest

from market_research.research.hashing import report_content_hash_payload, sha256_prefixed
from market_research.research.strategy_package import StrategyPackageError, build_strategy_research_package
from tests.test_strategy_research_package import _result


def _rehash_report(report):
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))


def test_package_rejects_capability_hash_cross_binding_mismatch(monkeypatch):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    report = copy.deepcopy(_result())
    report["candidates"][0]["capability_contract_hash"] = sha256_prefixed({"tampered": True})
    _rehash_report(report)
    with pytest.raises(StrategyPackageError, match="capability_contract_hash_mismatch"):
        build_strategy_research_package(report)


def test_package_rejects_capability_payload_cross_binding_mismatch(monkeypatch):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    report = copy.deepcopy(_result())
    report["candidates"][0]["capability_contract"]["pyramiding"] = True
    _rehash_report(report)
    with pytest.raises(StrategyPackageError, match="capability_contract_hash_mismatch"):
        build_strategy_research_package(report)
