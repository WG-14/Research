from __future__ import annotations

import pytest

from market_research.research.strategy_package import StrategyPackageError, build_strategy_research_package
from market_research.research.hashing import sha256_prefixed
from market_research.research.hashing import report_content_hash_payload
from market_research.research_composition import builtin_strategy_registry
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research.governance import (
    GovernanceSubject, GovernanceSubjectType, append_lifecycle_transition,
    approve_strategy_candidate,
)
from tests.test_run_lifecycle import _context


def _result():
    registry = builtin_strategy_registry()
    compiled = StrategyCompiler(registry).compile(
        strategy_name="noop_baseline", raw_parameters={}, fee_rate=0, slippage_bps=0).as_dict()
    strategy_spec = registry.resolve("noop_baseline").spec.as_dict()
    capability = compiled["capability_contract"]
    evidence = {"declared_execution_timing_hash": "sha256:t", "executed_execution_timing_hash": "sha256:t", "declared_execution_model_hash": "sha256:m", "executed_execution_model_hash": "sha256:m", "execution_request_count": 1, "execution_model_invocation_count": 1, "fill_count": 1, "decision_stream_hash": "sha256:d", "metrics_hash": "sha256:metrics", "execution_request_stream_hash": "sha256:r", "execution_fill_stream_hash": "sha256:f", "portfolio_ledger_hash": "sha256:l", "timing_invariant_status": "PASS"}
    scenario = {"scenario_id": "base", "compiled_strategy_contract": compiled,
                "compiled_strategy_contract_hash": compiled["compiled_contract_hash"],
                "execution_evidence": evidence,
                "validation_metrics": {"return_pct": 1.0, "max_drawdown_pct": 2.0, "trade_count": 1}}
    candidate = {"parameter_candidate_id": "candidate-1", "primary_scenario_id": "base",
                 "scenario_results": [scenario], "strategy_spec_hash": "sha256:spec",
                 "strategy_registry_hash": compiled["strategy_registry_hash"],
                 "strategy_plugin_contract_hash": compiled["strategy_plugin_contract_hash"],
                 "compiled_strategy_contract": compiled, "compiled_strategy_contract_hash": compiled["compiled_contract_hash"],
                 "capability_contract_hash": compiled["capability_contract_hash"], "capability_contract": capability,
                 "effective_strategy_parameters_hash": compiled["materialized_parameters_hash"],
                 "effective_strategy_parameters": compiled["materialized_parameters"],
                 "metrics_hash": "sha256:metrics", "decision_contract_version": "v1", "execution_evidence": evidence,
                 "data_requirements": {}, "execution_timing_policy": {}, "execution_model": {}, "cost_assumption": {},
                 "partial_fill_assumptions": {}, "order_failure_assumptions": {}, "portfolio_policy": {}, "risk_policy": {},
                 "execution_limitations": [], "suspension_or_invalidation_criteria": []}
    candidate["strategy_spec"] = strategy_spec
    candidate["strategy_spec_hash"] = sha256_prefixed(strategy_spec)
    selection_material = {
        "schema_version": 1,
        "artifact_type": "pre_holdout_candidate_selection",
        "manifest_hash": "sha256:" + "1" * 64,
        "selected_candidate_id": "candidate-1",
        "parameter_values_hash": "sha256:" + "2" * 64,
        "effective_strategy_parameters_hash": compiled["materialized_parameters_hash"],
        "compiled_strategy_contract_hash": compiled["compiled_contract_hash"],
        "selection_universe_hash": "sha256:" + "3" * 64,
        "validation_evidence_hash": "sha256:" + "4" * 64,
        "final_selection_contract_hash": "sha256:" + "5" * 64,
        "candidate_scores_hash": "sha256:" + "6" * 64,
    }
    selection_artifact = {
        **selection_material,
        "content_hash": sha256_prefixed(selection_material, label="selection_artifact"),
    }
    confirmation_material = {
        "schema_version": 1,
        "artifact_type": "final_holdout_confirmation",
        "selection_artifact_hash": selection_artifact["content_hash"],
        "selected_candidate_id": "candidate-1",
        "candidate_results": [{"candidate_id": "candidate-1", "metrics": {
            "return_pct": 2.0, "max_drawdown_pct": 3.0, "trade_count": 1,
        }}],
        "confirmation_gate_result": "PASS",
        "confirmation_gate_fail_reasons": [],
    }
    confirmation = {
        **confirmation_material,
        "content_hash": sha256_prefixed(confirmation_material, label="final_holdout_confirmation"),
    }
    hypothesis_spec = {
        "schema_version": 1, "hypothesis_id": "edge", "version": "1",
        "phenomenon": "The candidate has positive conditional expectancy.",
        "mechanism": "The declared deterministic rule captures the proposed edge.",
        "observation_conditions": ["immutable candle data"], "comparison_target": "cash",
        "falsification_criteria": ["non-positive final holdout return"],
        "experiment_family_id": "edge-family", "registration_status": "unregistered",
        "pre_registered_at": None, "registration_evidence_hash": None,
    }
    report = {
        "hypothesis_id": "edge",
        "hypothesis_version": "1",
        "hypothesis_contract_hash": sha256_prefixed(hypothesis_spec),
        "hypothesis_spec": hypothesis_spec,
        "market": "KRW-BTC",
        "interval": "1m",
        "strategy_spec": strategy_spec,
        "strategy_spec_hash": sha256_prefixed(strategy_spec),
        "allowed_live_regimes": [],
        "blocked_live_regimes": [],
        "data_limitations": {"queue_position_available": False},
        "execution_limitations": [],
        "statistical_evidence_limitations": [],
        "final_selection_gate_result": "PASS",
        "selected_candidate_id": "candidate-1",
        "candidates": [candidate],
        "selection_artifact": selection_artifact,
        "final_holdout_confirmation": confirmation,
    }
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    return report


def _approval(report, tmp_path):
    manager = _context(tmp_path).paths
    subject = GovernanceSubject(GovernanceSubjectType.STRATEGY_CANDIDATE, "candidate-1", "1")
    for source, target, evidence in (
        (None, "DRAFT", {}),
        ("DRAFT", "BACKTESTED", {"backtest_report_hash": "sha256:" + "1" * 64}),
        ("BACKTESTED", "ROBUSTNESS_PASSED", {"stress_suite_hash": "sha256:" + "2" * 64}),
        ("ROBUSTNESS_PASSED", "OUT_OF_SAMPLE_PASSED", {
            "final_holdout_confirmation_hash": report["final_holdout_confirmation"]["content_hash"]
        }),
    ):
        append_lifecycle_transition(
            manager=manager, subject=subject, from_state=source, to_state=target,
            actor_id="researcher-a", reason=f"advance to {target}", evidence_hashes=evidence,
        )
    hypothesis = GovernanceSubject(GovernanceSubjectType.HYPOTHESIS, "edge", "1")
    for source, target, evidence in (
        (None, "IDEA", {"hypothesis_semantic_fingerprint": "sha256:" + "0" * 64}),
        ("IDEA", "HYPOTHESIS_DEFINED", {"hypothesis_contract_hash": report["hypothesis_contract_hash"]}),
        ("HYPOTHESIS_DEFINED", "EXPLORING", {}),
        ("EXPLORING", "VALIDATING", {"validation_manifest_hash": "sha256:" + "6" * 64}),
        ("VALIDATING", "SUPPORTED", {"validation_report_hash": report["content_hash"]}),
    ):
        append_lifecycle_transition(
            manager=manager, subject=hypothesis, from_state=source, to_state=target,
            actor_id="researcher-a", reason=f"advance hypothesis to {target}", evidence_hashes=evidence,
        )
    return approve_strategy_candidate(
        manager=manager, subject=subject, source_report_hash=report["content_hash"],
        hypothesis_subject=hypothesis, hypothesis_contract_hash=report["hypothesis_contract_hash"],
        strategy_name=report["candidates"][0]["compiled_strategy_contract"]["strategy_name"],
        strategy_version=report["candidates"][0]["compiled_strategy_contract"]["strategy_version"],
        strategy_plugin_contract_hash=report["candidates"][0]["strategy_plugin_contract_hash"],
        effective_strategy_parameters_hash=report["candidates"][0]["effective_strategy_parameters_hash"],
        final_holdout_confirmation_hash=report["final_holdout_confirmation"]["content_hash"],
        reviewer_id="approver-a", rationale="human research review passed",
    )


def test_package_contains_execution_and_ledger_contract_hashes(monkeypatch, tmp_path):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    report = _result()
    approval = _approval(report, tmp_path)
    package = build_strategy_research_package(report, approval=approval)
    assert package["execution_model_hash"] == "sha256:m" and package["ledger_stream_hash"] == "sha256:l"
    assert build_strategy_research_package(report, approval=approval)["content_hash"] == package["content_hash"]


def test_package_self_contains_complete_review_specification(monkeypatch, tmp_path):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    report = _result()
    package = build_strategy_research_package(report, approval=_approval(report, tmp_path))

    assert package["schema_version"] == 5
    assert package["hypothesis"] == report["hypothesis_spec"]
    assert package["target_asset"] == {"market": "KRW-BTC", "interval": "1m"}
    assert package["feature_definitions"]
    assert package["entry_conditions"]["entry"]["rule_id"] == "noop_hold"
    assert package["compiled_strategy_contract"]["materialized_parameters"] == package["effective_strategy_parameters"]
    assert package["expected_performance_range"]["metric_ranges"]["return_pct"] == {
        "minimum": 1.0, "maximum": 2.0, "observation_count": 2,
    }
    assert package["known_limitations"]["data"] == {"queue_position_available": False}
    assert package["approval_record"]["reviewer_id"] == "approver-a"
    assert package["approval_record"]["approved_at"]


def test_package_rejects_missing_feature_definitions(monkeypatch):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    report = _result()
    report["strategy_spec"]["feature_definitions"] = []
    report["strategy_spec_hash"] = sha256_prefixed(report["strategy_spec"])
    report["candidates"][0]["strategy_spec"] = report["strategy_spec"]
    report["candidates"][0]["strategy_spec_hash"] = report["strategy_spec_hash"]
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))

    with pytest.raises(StrategyPackageError, match="feature_definitions_missing"):
        build_strategy_research_package(report)


def test_package_rejects_automatic_pass_without_human_approval(monkeypatch):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    with pytest.raises(StrategyPackageError, match="strategy_approval_missing"):
        build_strategy_research_package(_result())


def test_package_rejects_missing_execution_evidence(monkeypatch):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    value = _result(); value["candidates"][0]["execution_evidence"].pop("portfolio_ledger_hash")
    with pytest.raises(StrategyPackageError, match="missing_execution_evidence"):
        build_strategy_research_package(value)


def test_package_rejects_tampered_selection_or_confirmation_binding(monkeypatch):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    report = _result()
    report["final_holdout_confirmation"]["candidate_results"][0]["candidate_id"] = "candidate-2"
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))

    with pytest.raises(StrategyPackageError, match="confirmation_invalid"):
        build_strategy_research_package(report)


def test_package_rejects_failed_confirmation_without_fallback(monkeypatch):
    monkeypatch.setattr("market_research.research.strategy_package.validate_final_selection_report", lambda report: [])
    report = _result()
    confirmation = report["final_holdout_confirmation"]
    confirmation["confirmation_gate_result"] = "FAIL"
    material = {key: value for key, value in confirmation.items() if key != "content_hash"}
    confirmation["content_hash"] = sha256_prefixed(material, label="final_holdout_confirmation")
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))

    with pytest.raises(StrategyPackageError, match="requires_final_holdout_confirmation_pass"):
        build_strategy_research_package(report)
