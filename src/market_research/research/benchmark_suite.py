from __future__ import annotations

from dataclasses import dataclass
import json
import random
from pathlib import Path
from typing import Any, Iterable

from market_research.paths import ResearchPathManager

from .backtest_types import BacktestRunContext
from .benchmark_schedule import build_internal_schedule_benchmark_plugin
from .dataset_snapshot import DatasetSnapshot
from .execution_model import (
    DepthWalkExecutionModel,
    FixedBpsExecutionModel,
    StressExecutionModel,
)
from .experiment_manifest import ExecutionScenario, ExperimentManifest
from .hashing import content_hash_payload, sha256_prefixed
from .governance import governance_registry_path, validate_strategy_approval
from .strategy_spec import materialize_strategy_parameters
from .intervals import interval_to_milliseconds
from .simulation_engine import run_common_simulation_backtest
from .strategy_registry import StrategyRegistry


@dataclass(frozen=True)
class BenchmarkSuiteRunner:
    """Run executable benchmarks under the candidate's simulation policies."""

    manifest: ExperimentManifest
    strategy_registry: StrategyRegistry
    manager: ResearchPathManager | None = None

    def run(
        self,
        snapshots: dict[str, DatasetSnapshot] | Iterable[DatasetSnapshot],
        *,
        candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        items = (
            snapshots.items()
            if isinstance(snapshots, dict)
            else ((snapshot.split_name, snapshot) for snapshot in snapshots)
        )
        return {
            split_name: self._run_split(snapshot, candidates=candidates or [])
            for split_name, snapshot in sorted(items)
        }

    def _run_split(
        self, snapshot: DatasetSnapshot, *, candidates: list[dict[str, Any]]
    ) -> dict[str, Any]:
        scenario_index, scenario = self._base_scenario()
        scenario_hash = sha256_prefixed(scenario.as_dict())
        scenario_id = f"benchmark_base_{scenario_hash.split(':', 1)[1][:12]}"
        benchmark_candidate_id = "benchmark:buy_and_hold_baseline"
        plugin = self.strategy_registry.resolve("buy_and_hold_baseline")
        run = self._run_strategy(
            plugin=plugin,
            snapshot=snapshot,
            parameter_values={"BUY_HOLD_BUY_INDEX": 0},
            candidate_id=benchmark_candidate_id,
            scenario=scenario,
            scenario_index=scenario_index,
            scenario_id=scenario_id,
            registry=self.strategy_registry,
        )
        metrics = run.metrics.as_dict()
        buy_and_hold_equity_curve = [point.as_dict() for point in run.equity_curve]
        execution_contract = {
            "simulation_policy_hash": self.manifest.simulation_policy_hash(),
            "execution_scenario_hash": scenario_hash,
            "execution_timing_hash": sha256_prefixed(
                self.manifest.execution_timing.as_dict()
            ),
            "portfolio_policy_hash": self.manifest.portfolio_policy_hash(),
            "risk_policy_hash": self.manifest.risk_policy_hash(),
        }
        payload = {
            "cash_return_pct": 0.0,
            "buy_and_hold_return_pct": metrics.get("return_pct"),
            "buy_and_hold_method": "common_simulation_engine",
            "buy_and_hold_strategy_name": plugin.name,
            "buy_and_hold_strategy_version": plugin.version,
            "buy_and_hold_strategy_plugin_contract_hash": plugin.contract_hash(),
            "buy_and_hold_compiled_contract_hash": run.compiled_strategy_contract_hash,
            "buy_and_hold_metrics_hash": run.metrics_hash,
            "buy_and_hold_equity_curve": buy_and_hold_equity_curve,
            "buy_and_hold_equity_curve_hash": sha256_prefixed(
                buy_and_hold_equity_curve
            ),
            "benchmark_execution_contract": execution_contract,
            "benchmark_execution_contract_hash": sha256_prefixed(execution_contract),
            "dataset_snapshot_hash": snapshot.snapshot_fingerprint_hash(),
        }
        contract = getattr(self.manifest, "benchmark_suite", None)
        if contract is not None:
            payload["benchmark_suite_contract"] = contract.as_dict()
            payload["benchmark_suite_contract_hash"] = sha256_prefixed(
                contract.as_dict()
            )
            payload["random_entry"] = self._random_entry_benchmark(
                snapshot=snapshot,
                scenario=scenario,
                scenario_index=scenario_index,
                scenario_id=scenario_id,
            )
            payload["same_holding_period_by_candidate"] = (
                self._same_holding_period_benchmarks(
                    snapshot=snapshot,
                    candidates=candidates,
                    scenario=scenario,
                    scenario_index=scenario_index,
                    scenario_id=scenario_id,
                )
            )
            payload["simpler_strategy"] = self._strategy_reference_benchmark(
                reference=contract.simpler_strategy,
                role="simpler_strategy",
                snapshot=snapshot,
                scenario=scenario,
                scenario_index=scenario_index,
                scenario_id=scenario_id,
            )
            approval_evidence = _load_approval_artifact(
                path=contract.approved_strategy.approval_artifact_path,
                expected_hash=contract.approved_strategy.approval_artifact_hash,
                reference=contract.approved_strategy.strategy,
                registry=self.strategy_registry,
                expected_governance_registry_path=(
                    governance_registry_path(self.manager)
                    if self.manager is not None
                    else None
                ),
            )
            payload["approved_strategy"] = {
                **self._strategy_reference_benchmark(
                    reference=contract.approved_strategy.strategy,
                    role="approved_strategy",
                    snapshot=snapshot,
                    scenario=scenario,
                    scenario_index=scenario_index,
                    scenario_id=scenario_id,
                ),
                "approval_evidence": approval_evidence,
            }
        return payload

    def _strategy_reference_benchmark(
        self,
        *,
        reference: Any,
        role: str,
        snapshot: DatasetSnapshot,
        scenario: ExecutionScenario,
        scenario_index: int,
        scenario_id: str,
    ) -> dict[str, Any]:
        plugin = self.strategy_registry.resolve(reference.strategy_name)
        run = self._run_strategy(
            plugin=plugin,
            snapshot=snapshot,
            parameter_values=dict(reference.parameter_values),
            candidate_id=f"benchmark:{role}:{plugin.name}",
            scenario=scenario,
            scenario_index=scenario_index,
            scenario_id=scenario_id,
            registry=self.strategy_registry,
        )
        equity_curve = [point.as_dict() for point in run.equity_curve]
        return {
            "status": "PASS",
            "role": role,
            "strategy_name": plugin.name,
            "strategy_version": plugin.version,
            "parameter_values": dict(reference.parameter_values),
            "strategy_plugin_contract_hash": plugin.contract_hash(),
            "compiled_contract_hash": run.compiled_strategy_contract_hash,
            "return_pct": run.metrics.return_pct,
            "metrics": run.metrics.as_dict(),
            "metrics_hash": run.metrics_hash,
            "equity_curve": equity_curve,
            "equity_curve_hash": sha256_prefixed(equity_curve),
            "fail_reasons": [],
        }

    def _run_strategy(
        self,
        *,
        plugin: Any,
        snapshot: DatasetSnapshot,
        parameter_values: dict[str, Any],
        candidate_id: str,
        scenario: ExecutionScenario,
        scenario_index: int,
        scenario_id: str,
        registry: StrategyRegistry | None,
    ) -> Any:
        execution_model = _execution_model(
            scenario,
            seed_material={
                "simulation_seed_scope_hash": self.manifest.simulation_seed_scope_hash(),
                "scenario_hash": sha256_prefixed(scenario.as_dict()),
                "candidate_id": candidate_id,
                "split_name": snapshot.split_name,
                "base_seed": scenario.seed,
            },
        )
        return run_common_simulation_backtest(
            plugin=plugin,
            dataset=snapshot,
            parameter_values=parameter_values,
            fee_rate=scenario.fee_rate,
            slippage_bps=scenario.slippage_bps,
            execution_model=execution_model,
            execution_timing_policy=self.manifest.execution_timing,
            portfolio_policy=self.manifest.portfolio_policy,
            risk_policy=self.manifest.risk_policy,
            context=BacktestRunContext(
                experiment_id=self.manifest.experiment_id,
                candidate_id=candidate_id,
                scenario_id=scenario_id,
                scenario_index=scenario_index,
                split_name=snapshot.split_name,
                report_detail="summary",
            ),
            registry=registry,
        )

    def _random_entry_benchmark(
        self,
        *,
        snapshot: DatasetSnapshot,
        scenario: ExecutionScenario,
        scenario_index: int,
        scenario_id: str,
    ) -> dict[str, Any]:
        contract = self.manifest.benchmark_suite.random_entry
        seed_material = {
            "manifest_hash": self.manifest.manifest_hash(),
            "split_name": snapshot.split_name,
            "benchmark_contract_hash": sha256_prefixed(contract.as_dict()),
        }
        seed_hash = sha256_prefixed(seed_material)
        seed = int(seed_hash.split(":", 1)[1][:16], 16)
        rng = random.Random(seed)
        eligible_indices = list(range(max(0, len(snapshot.candles) - 1)))
        if not eligible_indices:
            return {
                "status": "FAIL",
                "fail_reasons": ["random_entry_no_causal_fill_index"],
                "iterations": contract.iterations,
                "seed": seed,
                "seed_material_hash": seed_hash,
            }
        plugin = self.strategy_registry.resolve("buy_and_hold_baseline")
        samples: list[dict[str, Any]] = []
        for iteration in range(contract.iterations):
            entry_index = eligible_indices[rng.randrange(len(eligible_indices))]
            run = self._run_strategy(
                plugin=plugin,
                snapshot=snapshot,
                parameter_values={"BUY_HOLD_BUY_INDEX": entry_index},
                candidate_id=f"benchmark:random_entry:{iteration:06d}",
                scenario=scenario,
                scenario_index=scenario_index,
                scenario_id=scenario_id,
                registry=self.strategy_registry,
            )
            samples.append(
                {
                    "iteration": iteration,
                    "entry_index": entry_index,
                    "return_pct": run.metrics.return_pct,
                    "metrics_hash": run.metrics_hash,
                    "compiled_contract_hash": run.compiled_strategy_contract_hash,
                }
            )
        returns = [float(sample["return_pct"]) for sample in samples]
        return {
            "status": "PASS",
            "method": contract.entry_index_policy,
            "iterations": contract.iterations,
            "seed": seed,
            "seed_material_hash": seed_hash,
            "mean_return_pct": sum(returns) / len(returns),
            "return_pct_p05": _percentile(returns, 5.0),
            "return_pct_median": _percentile(returns, 50.0),
            "return_pct_p95": _percentile(returns, 95.0),
            "positive_return_probability": sum(value > 0.0 for value in returns)
            / len(returns),
            "samples": samples,
            "samples_hash": sha256_prefixed(samples),
            "fail_reasons": [],
        }

    def _same_holding_period_benchmarks(
        self,
        *,
        snapshot: DatasetSnapshot,
        candidates: list[dict[str, Any]],
        scenario: ExecutionScenario,
        scenario_index: int,
        scenario_id: str,
    ) -> dict[str, dict[str, Any]]:
        contract = self.manifest.benchmark_suite.same_holding_period
        interval_ms = interval_to_milliseconds(snapshot.interval)
        plugin = build_internal_schedule_benchmark_plugin()
        out: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            candidate_id = str(
                candidate.get("parameter_candidate_id")
                or candidate.get("candidate_id")
                or ""
            )
            metrics_v2 = candidate.get(f"{snapshot.split_name}_metrics_v2")
            trade_quality = (
                metrics_v2.get("trade_quality")
                if isinstance(metrics_v2, dict)
                else None
            )
            time_exposure = (
                metrics_v2.get("time_exposure")
                if isinstance(metrics_v2, dict)
                else None
            )
            trade_count = (
                int(trade_quality.get("closed_trade_count") or 0)
                if isinstance(trade_quality, dict)
                else 0
            )
            median_ms = (
                time_exposure.get("median_holding_time_ms")
                if isinstance(time_exposure, dict)
                else None
            )
            if trade_count < contract.min_candidate_closed_trades or median_ms is None:
                out[candidate_id] = {
                    "status": "FAIL",
                    "fail_reasons": [
                        "same_holding_period_candidate_evidence_insufficient"
                    ],
                    "candidate_closed_trade_count": trade_count,
                }
                continue
            holding_bars = max(1, round(float(median_ms) / interval_ms))
            entries: list[int] = []
            exits: list[int] = []
            entry = 0
            while entry + holding_bars < len(snapshot.candles) - 1:
                entries.append(entry)
                exits.append(entry + holding_bars)
                entry += holding_bars + 2
            if not entries:
                out[candidate_id] = {
                    "status": "FAIL",
                    "fail_reasons": ["same_holding_period_no_complete_schedule"],
                    "holding_period_bars": holding_bars,
                }
                continue
            run = self._run_strategy(
                plugin=plugin,
                snapshot=snapshot,
                parameter_values={"ENTRY_INDICES": entries, "EXIT_INDICES": exits},
                candidate_id=f"benchmark:same_holding:{candidate_id}",
                scenario=scenario,
                scenario_index=scenario_index,
                scenario_id=scenario_id,
                registry=None,
            )
            schedule = {"entry_indices": entries, "exit_indices": exits}
            out[candidate_id] = {
                "status": "PASS",
                "method": contract.entry_policy,
                "holding_period_source": contract.holding_period_source,
                "candidate_closed_trade_count": trade_count,
                "holding_period_bars": holding_bars,
                "scheduled_trade_count": len(entries),
                "return_pct": run.metrics.return_pct,
                "metrics": run.metrics.as_dict(),
                "metrics_hash": run.metrics_hash,
                "compiled_contract_hash": run.compiled_strategy_contract_hash,
                "schedule_hash": sha256_prefixed(schedule),
                "fail_reasons": [],
            }
        return out

    def _base_scenario(self) -> tuple[int, ExecutionScenario]:
        for index, scenario in enumerate(self.manifest.execution_model.scenarios):
            if scenario.scenario_role == "base":
                return index, scenario
        return 0, self.manifest.execution_model.scenarios[0]


def _execution_model(
    scenario: ExecutionScenario,
    *,
    seed_material: dict[str, Any],
) -> FixedBpsExecutionModel | StressExecutionModel | DepthWalkExecutionModel:
    if scenario.type == "fixed_bps":
        return FixedBpsExecutionModel(scenario.fee_rate, scenario.slippage_bps)
    if scenario.type == "stress":
        return StressExecutionModel(
            fee_rate=scenario.fee_rate,
            slippage_bps=scenario.slippage_bps,
            latency_ms=scenario.latency_ms,
            partial_fill_rate=scenario.partial_fill_rate,
            order_failure_rate=scenario.order_failure_rate,
            market_order_extra_cost_bps=scenario.market_order_extra_cost_bps,
            seed=scenario.seed,
            seed_derivation_inputs=seed_material,
        )
    if scenario.type == "depth_walk":
        return DepthWalkExecutionModel(fee_rate=scenario.fee_rate)
    raise ValueError(f"unsupported benchmark execution scenario:{scenario.type}")


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _load_approval_artifact(
    *,
    path: str,
    expected_hash: str,
    reference: Any,
    registry: StrategyRegistry,
    expected_governance_registry_path: Path | None = None,
) -> dict[str, Any]:
    artifact_path = Path(path).expanduser().resolve(strict=True)
    with artifact_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("approved_strategy_artifact_invalid")
    actual_hash = sha256_prefixed(
        content_hash_payload(
            {key: value for key, value in payload.items() if key != "content_hash"}
        )
    )
    if payload.get("content_hash") != actual_hash or actual_hash != expected_hash:
        raise ValueError("approved_strategy_artifact_hash_mismatch")
    plugin = registry.resolve(reference.strategy_name)
    expected = {
        "artifact_type": "approved_strategy_reference",
        "schema_version": 1,
        "approval_status": "approved",
        "strategy_name": reference.strategy_name,
        "strategy_version": reference.strategy_version,
        "strategy_plugin_contract_hash": plugin.contract_hash(),
        "parameter_values_hash": sha256_prefixed(reference.parameter_values),
    }
    mismatches = sorted(
        key for key, value in expected.items() if payload.get(key) != value
    )
    if mismatches:
        raise ValueError(
            "approved_strategy_artifact_binding_mismatch:" + ",".join(mismatches)
        )
    approval = payload.get("research_approval")
    if not isinstance(approval, dict):
        raise ValueError("approved_strategy_governance_approval_missing")
    approval_reasons = validate_strategy_approval(
        approval,
        source_report_hash=str(approval.get("source_report_hash") or ""),
        selected_candidate_id=str(approval.get("subject_id") or ""),
        final_holdout_confirmation_hash=str(
            approval.get("final_holdout_confirmation_hash") or ""
        ),
        hypothesis_id=str(approval.get("hypothesis_id") or ""),
        hypothesis_version=str(approval.get("hypothesis_version") or ""),
        hypothesis_contract_hash=str(approval.get("hypothesis_contract_hash") or ""),
        strategy_name=reference.strategy_name,
        strategy_version=reference.strategy_version,
        strategy_plugin_contract_hash=plugin.contract_hash(),
        effective_strategy_parameters_hash=sha256_prefixed(
            materialize_strategy_parameters(
                reference.strategy_name,
                dict(reference.parameter_values),
                registry=registry,
            )
        ),
        expected_registry_path=expected_governance_registry_path,
    )
    if approval_reasons:
        raise ValueError(
            "approved_strategy_governance_approval_invalid:"
            + ",".join(approval_reasons)
        )
    return {
        "approval_artifact_path": str(artifact_path),
        "approval_artifact_hash": actual_hash,
        "approval_status": "approved",
        "research_approval_hash": approval["content_hash"],
        "strategy_plugin_contract_hash": plugin.contract_hash(),
        "parameter_values_hash": expected["parameter_values_hash"],
    }
