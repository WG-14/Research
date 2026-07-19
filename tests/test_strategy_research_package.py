from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from market_research.research.strategy_package import (
    StrategyPackageError,
    build_strategy_research_package,
)
from market_research.research.validation_pipeline import (
    validate_validated_research_result,
)
from market_research.research.validation_protocol import _candidate_result_path
from market_research.research.hashing import sha256_prefixed
from market_research.research.hashing import report_content_hash_payload
from market_research.research.hypothesis_contract import parse_hypothesis_spec
from market_research.research.knowledge_registry import freeze_validation_admission
from market_research.research.final_selection import (
    FINAL_HOLDOUT_CONFIRMATION_SCHEMA_VERSION,
    FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION,
    compute_final_holdout_result_hash,
    selection_candidate_binding_summary,
)
from market_research.research.report_writer import candidate_evidence_hash_inputs
from market_research.research.experiment_registry import (
    FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION,
    append_attempt_completion,
    reserve_research_attempt,
)
from market_research.research_composition import (
    builtin_strategy_registry,
    parse_builtin_manifest,
)
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research.governance import (
    GovernanceSubject,
    GovernanceSubjectType,
    append_lifecycle_transition,
    approve_strategy_candidate,
)
from tests.test_run_lifecycle import _context
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2


def _result():
    registry = builtin_strategy_registry()
    compiled = (
        StrategyCompiler(registry)
        .compile(
            strategy_name="noop_baseline", raw_parameters={}, fee_rate=0, slippage_bps=0
        )
        .as_dict()
    )
    strategy_spec = registry.resolve("noop_baseline").spec.as_dict()
    capability = compiled["capability_contract"]
    evidence = {
        "execution_evidence_schema_version": 3,
        "declared_execution_timing_hash": "sha256:t",
        "executed_execution_timing_hash": "sha256:t",
        "declared_execution_timing_policy_hash": "sha256:t",
        "executed_execution_timing_policy_hash": "sha256:t",
        "execution_timing_stream_hash": "sha256:timing-stream",
        "declared_execution_model_hash": "sha256:m",
        "executed_execution_model_hash": "sha256:m",
        "execution_attempt_count": 1,
        "execution_reference_failure_count": 0,
        "model_eligible_request_count": 1,
        "execution_request_count": 1,
        "execution_model_invocation_count": 1,
        "fill_count": 1,
        "decision_stream_hash": "sha256:d",
        "metrics_hash": "sha256:metrics",
        "execution_request_stream_hash": "sha256:r",
        "execution_fill_stream_hash": "sha256:f",
        "portfolio_ledger_hash": "sha256:l",
        "ledger_stream_hash": "sha256:l",
        "timing_invariant_status": "PASS",
        "decision_timeline_invariant_status": "PASS",
        "causal_timeline_validator": "execution_invariants.v1",
        "market_knowledge_time_policy": (
            "event_time_lte_observed_availability_lte_portfolio_effective_time"
        ),
        "market_knowledge_time_basis_counts": {
            "quote_observed_at": 0,
            "quote_event_time_assumption": 0,
            "depth_observed_at": 0,
            "depth_event_time_assumption": 0,
        },
        "market_knowledge_time_assumption_count": 0,
    }
    scenario = {
        "scenario_id": "base",
        "compiled_strategy_contract": compiled,
        "compiled_strategy_contract_hash": compiled["compiled_contract_hash"],
        "execution_evidence": evidence,
        "execution_timing_policy": {"fill_reference": "next_open"},
        "execution_model": {"type": "fixed_bps"},
        "cost_assumption": {"fee_rate": 0.0, "slippage_bps": 0.0},
        "execution_reality_contract": {
            "partial_fill_model": {"type": "fixed_bps", "partial_fill_rate": 0.0},
            "order_failure_model": {"type": "fixed_bps", "order_failure_rate": 0.0},
        },
        "portfolio_policy": {"starting_cash_krw": 1_000_000.0},
        "validation_metrics": {
            "return_pct": 1.0,
            "max_drawdown_pct": 2.0,
            "trade_count": 1,
        },
    }
    candidate = {
        "parameter_candidate_id": "candidate-1",
        "primary_scenario_id": "base",
        "parameter_values": {},
        "parameter_values_raw": {},
        "scenario_results": [scenario],
        "strategy_spec_hash": "sha256:spec",
        "strategy_registry_hash": compiled["strategy_registry_hash"],
        "strategy_plugin_contract_hash": compiled["strategy_plugin_contract_hash"],
        "compiled_strategy_contract": compiled,
        "compiled_strategy_contract_hash": compiled["compiled_contract_hash"],
        "capability_contract_hash": compiled["capability_contract_hash"],
        "capability_contract": capability,
        "effective_strategy_parameters_hash": compiled["materialized_parameters_hash"],
        "effective_strategy_parameters": compiled["materialized_parameters"],
        "metrics_hash": "sha256:metrics",
        "decision_contract_version": "v1",
        "execution_evidence": evidence,
        "data_requirements": {},
        "execution_timing_policy": {},
        "execution_model": {},
        "cost_assumption": {},
        "partial_fill_assumptions": {},
        "order_failure_assumptions": {},
        "portfolio_policy": {},
        "risk_policy": {},
        "execution_limitations": [],
        "suspension_or_invalidation_criteria": [],
    }
    candidate["strategy_spec"] = strategy_spec
    candidate["strategy_spec_hash"] = sha256_prefixed(strategy_spec)
    selection_material = {
        "schema_version": 2,
        "artifact_type": "pre_holdout_candidate_selection",
        "manifest_hash": "sha256:" + "1" * 64,
        "selected_candidate_id": "candidate-1",
        "parameter_values_hash": sha256_prefixed({}),
        "effective_strategy_parameters_hash": compiled["materialized_parameters_hash"],
        "compiled_strategy_contract_hash": compiled["compiled_contract_hash"],
        "selection_universe_hash_semantics": (
            "candidate_identity_contract_and_final_score_hashes_v1"
        ),
        "selection_universe_hash": "sha256:" + "3" * 64,
        "validation_evidence_hash": sha256_prefixed(
            {
                "candidate_id": "candidate-1",
                "validation_metrics": candidate.get("validation_metrics"),
                "validation_metrics_v2": candidate.get("validation_metrics_v2"),
                "validation_stress_suite": candidate.get("validation_stress_suite"),
                "walk_forward_metrics": candidate.get("walk_forward_metrics"),
                "acceptance_gate_result": candidate.get("acceptance_gate_result"),
            }
        ),
        "final_selection_contract_hash": "sha256:" + "5" * 64,
        "candidate_scores_hash": "sha256:" + "6" * 64,
    }
    selection_artifact = {
        **selection_material,
        "content_hash": sha256_prefixed(selection_material, label="selection_artifact"),
    }
    confirmation_material = {
        "schema_version": FINAL_HOLDOUT_CONFIRMATION_SCHEMA_VERSION,
        "artifact_type": "final_holdout_confirmation",
        "manifest_hash": selection_artifact["manifest_hash"],
        "selection_artifact_hash": selection_artifact["content_hash"],
        "selected_candidate_id": "candidate-1",
        "candidate_results": [
            {
                "candidate_id": "candidate-1",
                "compiled_strategy_contract_hash": compiled["compiled_contract_hash"],
                "metrics": {
                    "return_pct": 2.0,
                    "max_drawdown_pct": 3.0,
                    "trade_count": 1,
                },
            }
        ],
        "confirmation_gate_result": "PASS",
        "confirmation_gate_fail_reasons": [],
        "final_holdout_result_hash_schema_version": (
            FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION
        ),
    }
    confirmation_material["final_holdout_result_hash"] = (
        compute_final_holdout_result_hash(confirmation_material)
    )
    confirmation = {
        **confirmation_material,
        "content_hash": sha256_prefixed(
            confirmation_material, label="final_holdout_confirmation"
        ),
    }
    parsed_hypothesis = parse_hypothesis_spec(
        hypothesis_spec_v2(
            hypothesis_id="edge",
            version="1",
            hypothesis_text="The candidate has positive conditional expectancy.",
            phenomenon="The candidate has positive conditional expectancy.",
            mechanism="The deterministic rule captures the proposed edge.",
            experiment_family_id="edge-family",
            competing_hypotheses=[
                {
                    "hypothesis_id": "edge",
                    "version": "1",
                    "hypothesis_text": (
                        "The candidate has positive conditional expectancy."
                    ),
                },
                {
                    "hypothesis_id": "null-edge",
                    "version": "1",
                    "hypothesis_text": (
                        "The candidate has no positive conditional expectancy."
                    ),
                },
            ],
        )
    )
    hypothesis_spec = parsed_hypothesis.as_dict()
    question_ref = parsed_hypothesis.research_question_ref
    assert question_ref is not None
    report = {
        "schema_version": 3,
        "artifact_type": "validated_research_result",
        "research_classification": "validated_candidate",
        "end_to_end_validation_result": "PASS",
        "validation_blocking_reasons": [],
        "validation_stages": [
            {"name": "readiness", "status": "PASS"},
            {"name": "dataset_quality", "status": "PASS"},
            {"name": "backtest", "status": "PASS"},
            {"name": "final_holdout", "status": "PASS"},
            {"name": "stress_suite", "status": "PASS"},
            {"name": "statistical_validation", "status": "PASS"},
            {"name": "walk_forward", "status": "NOT_REQUIRED"},
            {"name": "final_selection", "status": "PASS"},
            {"name": "research_candidate_report", "status": "PASS"},
        ],
        "dataset_quality_gate_status": "PASS",
        "stress_suite_gate_result": "PASS",
        "statistical_gate_result": "PASS",
        "validation_eligibility_gate_result": "PASS",
        "gate_result": "PASS",
        "hypothesis_id": "edge",
        "hypothesis_version": "1",
        "hypothesis_contract_hash": parsed_hypothesis.contract_hash(),
        "hypothesis_lineage_hash": parsed_hypothesis.lineage_hash(),
        "research_question_id": question_ref.question_id,
        "research_question_version": question_ref.version,
        "research_question_hash": question_ref.question_hash,
        "observation_hashes": [
            item.observation_hash for item in parsed_hypothesis.observation_refs
        ],
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
        "execution_timing_policy": scenario["execution_timing_policy"],
        "execution_model": scenario["execution_model"],
        "base_cost_assumption": scenario["cost_assumption"],
        "portfolio_policy": scenario["portfolio_policy"],
        "risk_policy": {"max_drawdown_pct": 25.0},
        "final_selection_gate_result": "PASS",
        "selected_candidate_id": "candidate-1",
        "selected_candidate": candidate,
        "candidates": [candidate],
        "selection_artifact": selection_artifact,
        "final_holdout_confirmation": confirmation,
    }
    report["risk_policy_hash"] = sha256_prefixed(report["risk_policy"])
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    return report


def _bind_validation_admission(report, manager):
    if report.get("validation_admission_binding_schema_version") == 1:
        return
    manifest_payload = json.loads(
        Path("examples/research/sma_filter_manifest.example.json").read_text(
            encoding="utf-8"
        )
    )
    manifest_payload["experiment_id"] = "validated-candidate-fixture"
    manifest_payload["hypothesis_spec"] = report["hypothesis_spec"]
    manifest = parse_builtin_manifest(manifest_payload)
    admission = freeze_validation_admission(
        manager=manager,
        manifest=manifest,
        admitted_at="2026-01-01T00:00:00+00:00",
    )
    manifest_hash = manifest.manifest_hash()

    selection_artifact = report["selection_artifact"]
    selection_artifact["manifest_hash"] = manifest_hash
    selection_material = {
        key: value for key, value in selection_artifact.items() if key != "content_hash"
    }
    selection_artifact["content_hash"] = sha256_prefixed(
        selection_material,
        label="selection_artifact",
    )
    confirmation = report["final_holdout_confirmation"]
    confirmation["manifest_hash"] = manifest_hash
    confirmation["selection_artifact_hash"] = selection_artifact["content_hash"]
    confirmation["final_holdout_result_hash"] = compute_final_holdout_result_hash(
        confirmation
    )
    confirmation_material = {
        key: value for key, value in confirmation.items() if key != "content_hash"
    }
    confirmation["content_hash"] = sha256_prefixed(
        confirmation_material,
        label="final_holdout_confirmation",
    )
    report.update(
        {
            "experiment_id": manifest.experiment_id,
            "manifest_hash": manifest_hash,
            "validation_admission_binding_schema_version": 1,
            "knowledge_registry_path": admission["path"],
            "validation_admission_record_hash": admission["admission_record_hash"],
            "validation_admission_row_hash": admission["admission_row_hash"],
            "validation_admission": admission["admission"],
        }
    )
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))


def _bind_selected_candidate_artifact(report, manager):
    selected = deepcopy(report["candidates"][0])
    selected.update(
        {
            "experiment_id": report["experiment_id"],
            "manifest_hash": report["manifest_hash"],
            "dataset_snapshot_id": str(report.get("dataset_snapshot_id") or ""),
            "dataset_content_hash": str(report.get("dataset_content_hash") or ""),
        }
    )
    logical_hash = sha256_prefixed(
        candidate_evidence_hash_inputs(selected),
        label="candidate_evidence_hash",
    )
    selection_binding = selection_candidate_binding_summary(selected)
    candidate_artifact_hash = sha256_prefixed(
        selected, label="candidate_result_artifact_hash"
    )
    candidate_target = _candidate_result_path(
        manager,
        report["experiment_id"],
        "candidate-1",
        candidate_artifact_hash,
    )
    candidate_target.parent.mkdir(parents=True, exist_ok=True)
    candidate_target.write_text(
        json.dumps(selected, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    compact = deepcopy(selected)
    compact.update(
        {
            "candidate_payload_hash": logical_hash,
            "selection_binding": selection_binding,
            "candidate_result_artifact_ref": candidate_target.resolve()
            .relative_to(manager.data_dir().resolve())
            .as_posix(),
            "candidate_result_artifact_hash": candidate_artifact_hash,
            "candidate_result_artifact_detail_policy": "external_full",
        }
    )
    report["candidates"] = [compact]
    report["selected_candidate"] = compact
    selected_target = manager.report_path(
        "research", report["experiment_id"], "selected_candidate.json"
    )
    selected_target.parent.mkdir(parents=True, exist_ok=True)
    selected_target.write_text(
        json.dumps(selected, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    report.update(
        {
            "selected_candidate_binding_schema_version": 1,
            "selected_candidate_path": str(selected_target.resolve()),
            "selected_candidate_artifact_hash": sha256_prefixed(
                selected, label="selected_candidate_artifact_hash"
            ),
        }
    )
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))


def _approval(report, tmp_path):
    manager = _context(tmp_path).paths
    _bind_validation_admission(report, manager)
    confirmation = report["final_holdout_confirmation"]
    if not confirmation.get("experiment_registry_path"):
        reservation = reserve_research_attempt(
            manager=manager,
            base_payload={
                "experiment_id": "strategy-package-fixture",
                "experiment_family_id": "edge-family",
                "hypothesis_id": "edge",
                "manifest_hash": confirmation["manifest_hash"],
                "selection_artifact_hash": report["selection_artifact"]["content_hash"],
                "selected_candidate_id": "candidate-1",
                "final_holdout_content_pending_until_completion": True,
            },
        )
        completion = append_attempt_completion(
            manager=manager,
            reservation=reservation,
            updates={
                "dataset_artifact_evidence_hash": "sha256:" + "a" * 64,
                "final_holdout_query_hash": "sha256:" + "b" * 64,
                "final_holdout_data_hash": "sha256:" + "c" * 64,
                "final_holdout_fingerprint_hash": "sha256:" + "d" * 64,
                "final_holdout_quality_hash": "sha256:" + "e" * 64,
                "final_holdout_reuse_key_hash": "sha256:" + "f" * 64,
                "final_holdout_reuse_key_schema_version": (
                    FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION
                ),
                "selection_artifact_hash": report["selection_artifact"]["content_hash"],
                "selected_candidate_id": "candidate-1",
                "candidate_count": 1,
                "confirmation_gate_result": "PASS",
                "final_holdout_result_hash_schema_version": (
                    FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION
                ),
                "final_holdout_result_hash": confirmation["final_holdout_result_hash"],
            },
        )
        confirmation.update(
            {
                "experiment_registry_path": reservation["path"],
                "experiment_registry_prior_hash": reservation["prior_hash"],
                "experiment_registry_row_hash": reservation["row_hash"],
                "experiment_registry_completion_row_hash": completion["row_hash"],
                "authorization_row_hash": reservation["row_hash"],
                "completion_row_hash": completion["row_hash"],
            }
        )
        confirmation_material = {
            key: value
            for key, value in confirmation.items()
            if key not in {"content_hash", "confirmation_artifact_path"}
        }
        confirmation["content_hash"] = sha256_prefixed(
            confirmation_material,
            label="final_holdout_confirmation",
        )
        report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    _bind_selected_candidate_artifact(report, manager)
    subject = GovernanceSubject(
        GovernanceSubjectType.STRATEGY_CANDIDATE, "candidate-1", "1"
    )
    for source, target, evidence in (
        (None, "DRAFT", {}),
        ("DRAFT", "BACKTESTED", {"backtest_report_hash": "sha256:" + "1" * 64}),
        (
            "BACKTESTED",
            "ROBUSTNESS_PASSED",
            {"stress_suite_hash": "sha256:" + "2" * 64},
        ),
        (
            "ROBUSTNESS_PASSED",
            "OUT_OF_SAMPLE_PASSED",
            {
                "final_holdout_confirmation_hash": report["final_holdout_confirmation"][
                    "content_hash"
                ]
            },
        ),
    ):
        append_lifecycle_transition(
            manager=manager,
            subject=subject,
            from_state=source,
            to_state=target,
            actor_id="researcher-a",
            reason=f"advance to {target}",
            evidence_hashes=evidence,
        )
    hypothesis = GovernanceSubject(GovernanceSubjectType.HYPOTHESIS, "edge", "1")
    for source, target, evidence in (
        (None, "IDEA", {"hypothesis_semantic_fingerprint": "sha256:" + "0" * 64}),
        (
            "IDEA",
            "HYPOTHESIS_DEFINED",
            {"hypothesis_contract_hash": report["hypothesis_contract_hash"]},
        ),
        ("HYPOTHESIS_DEFINED", "EXPLORING", {}),
        ("EXPLORING", "VALIDATING", {"validation_manifest_hash": "sha256:" + "6" * 64}),
        ("VALIDATING", "SUPPORTED", {"validation_report_hash": report["content_hash"]}),
    ):
        append_lifecycle_transition(
            manager=manager,
            subject=hypothesis,
            from_state=source,
            to_state=target,
            actor_id="researcher-a",
            reason=f"advance hypothesis to {target}",
            evidence_hashes=evidence,
        )
    return approve_strategy_candidate(
        manager=manager,
        subject=subject,
        source_report_hash=report["content_hash"],
        hypothesis_subject=hypothesis,
        hypothesis_contract_hash=report["hypothesis_contract_hash"],
        strategy_name=report["candidates"][0]["compiled_strategy_contract"][
            "strategy_name"
        ],
        strategy_version=report["candidates"][0]["compiled_strategy_contract"][
            "strategy_version"
        ],
        strategy_plugin_contract_hash=report["candidates"][0][
            "strategy_plugin_contract_hash"
        ],
        effective_strategy_parameters_hash=report["candidates"][0][
            "effective_strategy_parameters_hash"
        ],
        final_holdout_confirmation_hash=report["final_holdout_confirmation"][
            "content_hash"
        ],
        reviewer_id="approver-a",
        rationale="human research review passed",
    )


def test_package_contains_execution_and_ledger_contract_hashes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    report = _result()
    approval = _approval(report, tmp_path)
    manager = _context(tmp_path).paths
    package = build_strategy_research_package(
        report, approval=approval, manager=manager
    )
    assert (
        package["execution_model_hash"] == "sha256:m"
        and package["ledger_stream_hash"] == "sha256:l"
    )
    assert package["authoritative"] is True
    assert package["package_authority_status"] == "CANONICAL_REGISTRIES_VERIFIED"
    assert package["package_authority_result"] == "PASS"
    assert (
        package["decision_contract_version"]
        == report["strategy_spec"]["decision_contract_version"]
    )
    assert package["risk_policy"] == report["risk_policy"]
    assert (
        build_strategy_research_package(report, approval=approval, manager=manager)[
            "content_hash"
        ]
        == package["content_hash"]
    )

    declared_only = build_strategy_research_package(report, approval=approval)
    assert declared_only["authoritative"] is False
    assert declared_only["package_authority_status"] == "DECLARED_PATH_ONLY"
    assert declared_only["package_authority_result"] == "UNVERIFIED"


def test_package_self_contains_complete_review_specification(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    report = _result()
    package = build_strategy_research_package(
        report,
        approval=_approval(report, tmp_path),
        manager=_context(tmp_path).paths,
    )

    assert package["schema_version"] == 5
    assert package["hypothesis"] == report["hypothesis_spec"]
    assert package["hypothesis_contract_hash"] == report["hypothesis_contract_hash"]
    assert package["hypothesis_lineage_hash"] == report["hypothesis_lineage_hash"]
    assert (
        package["research_question_ref"]
        == report["hypothesis_spec"]["research_question_ref"]
    )
    assert package["observation_refs"] == report["hypothesis_spec"]["observation_refs"]
    assert (
        package["approved_hypothesis_contract_hash"]
        == report["hypothesis_contract_hash"]
    )
    assert package["target_asset"] == {"market": "KRW-BTC", "interval": "1m"}
    assert package["feature_definitions"]
    assert package["entry_conditions"]["entry"]["rule_id"] == "noop_hold"
    assert (
        package["compiled_strategy_contract"]["materialized_parameters"]
        == package["effective_strategy_parameters"]
    )
    assert package["expected_performance_range"]["metric_ranges"]["return_pct"] == {
        "minimum": 1.0,
        "maximum": 2.0,
        "observation_count": 2,
    }
    assert package["known_limitations"]["data"] == {"queue_position_available": False}
    assert package["approval_record"]["reviewer_id"] == "approver-a"
    assert package["approval_record"]["approved_at"]


def test_package_preserves_internal_instrument_and_action_identity(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    report = _result()
    report["instrument_evidence"] = {
        "instrument_id": "inst_test_btc_0001",
        "instrument_version_id": "instv_test_btc_0001_v1",
        "instrument_contract_hash": "sha256:" + "a" * 64,
        "asset_type": "spot",
        "exchange_mic": "XOFF",
        "trading_currency": "KRW",
        "price_tick": "0.01",
        "quantity_step": "0.0001",
        "trading_unit": "1",
        "identity_source": "manifest",
        "corporate_action_set_id": "cas_test_btc_0001",
        "corporate_action_set_hash": "sha256:" + "b" * 64,
        "corporate_action_policy_id": "cap_raw_prices_v1",
        "corporate_action_policy_hash": "sha256:" + "c" * 64,
        "price_series": "raw",
        "price_adjustment": "none",
        "volume_adjustment": "none",
    }
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    package = build_strategy_research_package(
        report,
        approval=_approval(report, tmp_path),
        manager=_context(tmp_path).paths,
    )

    assert package["target_asset"] == {
        "market": "KRW-BTC",
        "interval": "1m",
        "instrument_evidence": report["instrument_evidence"],
    }


def test_validated_result_rejects_missing_or_mismatched_lineage_binding():
    missing = _result()
    missing.pop("hypothesis_lineage_hash")
    assert (
        "validated_research_result_hypothesis_lineage_hash_mismatch"
        in validate_validated_research_result(missing)
    )

    mismatched = _result()
    mismatched["observation_hashes"] = ["sha256:" + "f" * 64]
    assert (
        "validated_research_result_observation_hashes_mismatch"
        in validate_validated_research_result(mismatched)
    )


def test_package_rejects_missing_feature_definitions(monkeypatch):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    report = _result()
    report["strategy_spec"]["feature_definitions"] = []
    report["strategy_spec_hash"] = sha256_prefixed(report["strategy_spec"])
    report["candidates"][0]["strategy_spec"] = report["strategy_spec"]
    report["candidates"][0]["strategy_spec_hash"] = report["strategy_spec_hash"]
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))

    with pytest.raises(StrategyPackageError, match="feature_definitions_missing"):
        build_strategy_research_package(report)


def test_package_rejects_automatic_pass_without_human_approval(monkeypatch):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    with pytest.raises(StrategyPackageError, match="strategy_approval_missing"):
        build_strategy_research_package(_result())


def test_package_rejects_missing_execution_evidence(monkeypatch):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    value = _result()
    value["candidates"][0]["execution_evidence"].pop("ledger_stream_hash")
    with pytest.raises(StrategyPackageError, match="missing_execution_evidence"):
        build_strategy_research_package(value)


def test_package_rejects_incomplete_point_in_time_binding(monkeypatch):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    report = _result()
    report["candidates"][0]["execution_evidence"][
        "point_in_time_decision_stream_hash"
    ] = "sha256:" + "1" * 64
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))

    with pytest.raises(StrategyPackageError, match="point_in_time_binding_incomplete"):
        build_strategy_research_package(report)


def test_package_rejects_point_in_time_lineage_mismatch(monkeypatch):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    report = _result()
    point_in_time = {
        "point_in_time_decision_stream_hash": "sha256:" + "1" * 64,
        "point_in_time_authority_binding_hash": "sha256:" + "2" * 64,
        "point_in_time_evidence_content_hash": "sha256:" + "3" * 64,
    }
    report["candidates"][0]["execution_evidence"].update(point_in_time)
    report["lineage"] = {
        "dataset_split_evidence": {
            "validation": {
                **point_in_time,
                "point_in_time_evidence_content_hash": "sha256:" + "9" * 64,
            }
        }
    }
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))

    with pytest.raises(StrategyPackageError, match="point_in_time_lineage_mismatch"):
        build_strategy_research_package(report)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "decision_timeline_invariant_status",
            "FAIL",
            "decision_timeline_invariant_failure",
        ),
        (
            "market_knowledge_time_assumption_count",
            1,
            "knowledge_time_assumption_not_allowed",
        ),
        ("causal_timeline_validator", "unknown", "causal_validator_mismatch"),
        (
            "market_knowledge_time_policy",
            "unknown",
            "knowledge_time_policy_mismatch",
        ),
    ],
)
def test_package_rejects_tampered_v3_causal_evidence(monkeypatch, field, value, reason):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    report = _result()
    report["candidates"][0]["execution_evidence"][field] = value

    with pytest.raises(StrategyPackageError, match=reason):
        build_strategy_research_package(report)


def test_package_rejects_tampered_selection_or_confirmation_binding(monkeypatch):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    report = _result()
    report["final_holdout_confirmation"]["candidate_results"][0]["candidate_id"] = (
        "candidate-2"
    )
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))

    with pytest.raises(StrategyPackageError, match="confirmation_invalid"):
        build_strategy_research_package(report)


def test_package_rejects_failed_confirmation_without_fallback(monkeypatch):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    report = _result()
    confirmation = report["final_holdout_confirmation"]
    confirmation["confirmation_gate_result"] = "FAIL"
    confirmation["final_holdout_result_hash"] = compute_final_holdout_result_hash(
        confirmation
    )
    material = {
        key: value for key, value in confirmation.items() if key != "content_hash"
    }
    confirmation["content_hash"] = sha256_prefixed(
        material, label="final_holdout_confirmation"
    )
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))

    with pytest.raises(
        StrategyPackageError, match="requires_final_holdout_confirmation_pass"
    ):
        build_strategy_research_package(report)


def test_authoritative_package_rejects_contradictory_validation_stage(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "market_research.research.strategy_package.validate_final_selection_report",
        lambda report: [],
    )
    report = _result()
    next(
        stage
        for stage in report["validation_stages"]
        if stage["name"] == "dataset_quality"
    )["status"] = "FAIL"
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    approval = _approval(report, tmp_path)

    with pytest.raises(
        StrategyPackageError,
        match="validated_research_result_stage_not_passed:dataset_quality",
    ):
        build_strategy_research_package(
            report,
            approval=approval,
            manager=_context(tmp_path).paths,
        )
