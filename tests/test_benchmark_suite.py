from __future__ import annotations

from types import SimpleNamespace
import json
from pathlib import Path

from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.experiment_manifest import (
    DateRange,
    ExecutionModelConfig,
    ExecutionScenario,
    ExecutionTimingPolicy,
    legacy_research_portfolio_policy,
)
from market_research.research.risk_contract import ResearchRiskPolicy
from market_research.research.benchmark_suite import BenchmarkSuiteRunner
from market_research.research.benchmark_contract import (
    ApprovedStrategyBenchmarkReference,
    BenchmarkSuiteContract,
    RandomEntryBenchmarkContract,
    SameHoldingPeriodBenchmarkContract,
    StrategyBenchmarkReference,
    parse_benchmark_suite_contract,
)
from market_research.research_composition import builtin_strategy_registry
from market_research.research.hashing import content_hash_payload, sha256_prefixed
from tests.test_strategy_research_package import _approval, _result


def _snapshot() -> DatasetSnapshot:
    return DatasetSnapshot(
        "benchmark",
        "fixture",
        "KRW-BTC",
        "1m",
        "validation",
        DateRange("2026-01-01", "2026-01-01"),
        tuple(
            Candle(index * 60_000, price, price, price, price, 1.0)
            for index, price in enumerate((100.0, 102.0, 104.0, 106.0, 108.0))
        ),
    )


def _approval_artifact(path: Path) -> tuple[str, str]:
    plugin = builtin_strategy_registry().resolve("noop_baseline")
    report = _result()
    approval = _approval(report, path.parent / "governance")
    payload = {
        "artifact_type": "approved_strategy_reference",
        "schema_version": 1,
        "approval_status": "approved",
        "strategy_name": plugin.name,
        "strategy_version": plugin.version,
        "strategy_plugin_contract_hash": plugin.contract_hash(),
        "parameter_values_hash": sha256_prefixed({}),
        "research_approval": approval,
    }
    content_hash = sha256_prefixed(content_hash_payload(payload))
    path.write_text(json.dumps({**payload, "content_hash": content_hash}), encoding="utf-8")
    return str(path), content_hash


def _benchmark_contract(approval_path: str, approval_hash: str) -> BenchmarkSuiteContract:
    strategy = StrategyBenchmarkReference(
        "noop_baseline",
        "noop_baseline.research_contract.v1",
        {},
    )
    return BenchmarkSuiteContract(
        schema_version=1,
        required_for_validation=True,
        random_entry=RandomEntryBenchmarkContract(
            iterations=8,
            seed_policy="derived_from_manifest_split_benchmark_contract_hash",
            entry_index_policy="uniform_causal_entry_holding_to_split_end",
        ),
        same_holding_period=SameHoldingPeriodBenchmarkContract(
            holding_period_source="candidate_median_closed_trade_holding_bars",
            entry_policy="non_overlapping_unconditional_entries",
            min_candidate_closed_trades=2,
        ),
        simpler_strategy=strategy,
        approved_strategy=ApprovedStrategyBenchmarkReference(
            strategy,
            approval_path,
            approval_hash,
        ),
    )


def _manifest(*, fee_rate: float, slippage_bps: float, benchmark_suite=None) -> SimpleNamespace:
    scenario = ExecutionScenario(
        type="fixed_bps",
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        scenario_policy="single_scenario",
        scenario_role="base",
    )
    timing = ExecutionTimingPolicy(
        fill_reference_policy="next_candle_open",
        allow_same_candle_close_fill=False,
    )
    portfolio = legacy_research_portfolio_policy()
    risk = ResearchRiskPolicy()
    simulation_hash = "sha256:" + "d" * 64
    return SimpleNamespace(
        experiment_id="benchmark-suite-test",
        execution_model=ExecutionModelConfig(
            scenarios=(scenario,),
            source="execution_model",
            scenario_policy="single_scenario",
        ),
        execution_timing=timing,
        portfolio_policy=portfolio,
        risk_policy=risk,
        benchmark_suite=benchmark_suite,
        manifest_hash=lambda: "sha256:" + "a" * 64,
        simulation_seed_scope_hash=lambda: "sha256:" + "e" * 64,
        simulation_policy_hash=lambda: simulation_hash,
        portfolio_policy_hash=portfolio.policy_hash,
        risk_policy_hash=risk.policy_hash,
    )


def test_buy_and_hold_benchmark_uses_common_engine_and_execution_costs() -> None:
    snapshot = _snapshot()
    registry = builtin_strategy_registry()
    zero_cost = BenchmarkSuiteRunner(_manifest(fee_rate=0.0, slippage_bps=0.0), registry).run((snapshot,))
    costly = BenchmarkSuiteRunner(_manifest(fee_rate=0.01, slippage_bps=100.0), registry).run((snapshot,))

    zero = zero_cost["validation"]
    stressed = costly["validation"]
    assert zero["buy_and_hold_method"] == "common_simulation_engine"
    assert stressed["buy_and_hold_return_pct"] < zero["buy_and_hold_return_pct"]
    assert stressed["buy_and_hold_metrics_hash"]
    assert stressed["benchmark_execution_contract_hash"] != zero["benchmark_execution_contract_hash"]
    assert stressed["dataset_snapshot_hash"] == snapshot.snapshot_fingerprint_hash()


def test_complete_benchmark_suite_is_deterministic_and_execution_backed(tmp_path: Path) -> None:
    snapshot = _snapshot()
    registry = builtin_strategy_registry()
    approval_path, approval_hash = _approval_artifact(tmp_path / "approved-strategy.json")
    manifest = _manifest(
        fee_rate=0.001,
        slippage_bps=5.0,
        benchmark_suite=_benchmark_contract(approval_path, approval_hash),
    )
    candidate = {
        "parameter_candidate_id": "candidate-1",
        "validation_metrics_v2": {
            "trade_quality": {"closed_trade_count": 3},
            "time_exposure": {"median_holding_time_ms": 60_000},
        },
    }

    first = BenchmarkSuiteRunner(manifest, registry).run((snapshot,), candidates=[candidate])
    second = BenchmarkSuiteRunner(manifest, registry).run((snapshot,), candidates=[candidate])

    assert first == second
    split = first["validation"]
    assert split["random_entry"]["status"] == "PASS"
    assert split["random_entry"]["iterations"] == 8
    assert split["random_entry"]["samples_hash"]
    same_holding = split["same_holding_period_by_candidate"]["candidate-1"]
    assert same_holding["status"] == "PASS"
    assert same_holding["holding_period_bars"] == 1
    assert same_holding["scheduled_trade_count"] >= 1
    assert same_holding["metrics_hash"]
    assert same_holding["schedule_hash"]
    assert split["simpler_strategy"]["status"] == "PASS"
    assert split["approved_strategy"]["status"] == "PASS"
    assert split["approved_strategy"]["approval_evidence"]["approval_artifact_hash"] == approval_hash


def test_validation_benchmark_contract_requires_all_explicit_policy_choices(tmp_path: Path) -> None:
    approval_path, approval_hash = _approval_artifact(tmp_path / "approved-strategy.json")
    contract = _benchmark_contract(approval_path, approval_hash)
    payload = contract.as_dict()
    parsed = parse_benchmark_suite_contract(
        payload,
        research_classification="validated_candidate",
        registry=builtin_strategy_registry(),
    )

    assert parsed == contract


def test_approved_strategy_artifact_tampering_is_rejected(tmp_path: Path) -> None:
    approval_file = tmp_path / "approved-strategy.json"
    approval_path, approval_hash = _approval_artifact(approval_file)
    payload = json.loads(approval_file.read_text(encoding="utf-8"))
    payload["approval_status"] = "revoked"
    approval_file.write_text(json.dumps(payload), encoding="utf-8")
    manifest = _manifest(
        fee_rate=0.0,
        slippage_bps=0.0,
        benchmark_suite=_benchmark_contract(approval_path, approval_hash),
    )

    try:
        BenchmarkSuiteRunner(manifest, builtin_strategy_registry()).run((_snapshot(),), candidates=[])
    except ValueError as exc:
        assert str(exc) == "approved_strategy_artifact_hash_mismatch"
    else:
        raise AssertionError("tampered approval artifact must fail closed")
