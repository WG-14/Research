from __future__ import annotations

import os
import subprocess
import time
import json
import inspect
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable, Protocol

from market_research.execution_reality_contract import (
    build_execution_reality_contract,
    build_execution_capability_contract,
    unsupported_capability_reasons,
)
from .execution_calibration_contract import ExecutionCalibrationThresholds
from market_research.paths import ResearchPathManager
from market_research.storage_io import write_json_atomic
from market_research.market_regime import (
    MARKET_REGIME_VERSION,
    evaluate_regime_acceptance_gate,
)

from .dataset_snapshot import (
    DatasetQualityReport,
    DatasetSnapshot,
    build_dataset_quality_report,
    combined_dataset_fingerprint,
    combined_dataset_quality_hash,
    load_dataset_range,
    load_dataset_split,
)
from .data_plane import rolling_walk_forward_windows
from .datasets.registry import default_dataset_adapter_registry
from .datasets.contracts import DatasetRunContext
from .datasets.locators import LocatorValidationError, parse_immutable_locator
from .datasets.artifact_manifest import ArtifactManifestError, load_artifact_manifest
from .datasets.verification import (
    DatasetVerificationResult,
    VerificationStatus,
    verification_allowed,
)
from .backtest_common import execution_event_summary
from .benchmark_suite import BenchmarkSuiteRunner
from .walk_forward_selection import build_walk_forward_selection_evidence
from .simulation_engine import run_common_simulation_backtest
from .decision_stream_perturbation import EntrySignalOmissionTransformer
from .execution_evidence import validate_execution_evidence
from .backtest_types import (
    BacktestHeartbeatPolicy,
    BacktestResourceLimitExceeded,
    BacktestResourceLimits,
    BacktestRun,
    BacktestRunContext,
)
from .audit_trail import (
    AuditTraceScope,
    verify_audit_trail,
    write_trace_manifest,
    trace_manifest_path,
)
from .artifact_store import (
    ArtifactBudget,
    ArtifactBudgetExceeded,
    ResearchArtifactContext,
)
from .research_classification import (
    requires_candidate_validation,
    validate_execution_calibration_policy,
)
from .execution_calibration import compare_calibration_to_scenario
from .execution_model import (
    DepthWalkExecutionModel,
    FixedBpsExecutionModel,
    StressExecutionModel,
    model_params_hash,
)
from .execution_timing import execution_reality_gate, signal_quote_coverage_summary
from .experiment_manifest import (
    DateRange,
    ExecutionScenario,
    ExperimentManifest,
    required_execution_scenarios,
)
from .execution_plan import (
    ResearchExecutionPlan,
    ResearchWorkUnit,
    build_research_execution_plan,
    build_research_work_unit,
    parallel_efficiency_payload,
    parallel_work_task_count,
    precompute_dataset_hashes,
    _estimated_artifact_bytes,
)
from .executor import (
    ResearchWorkResult,
    execute_research_work_units_parallel,
    execute_research_work_units_serial,
    sort_work_results_deterministically,
)
from .final_selection import (
    FINAL_HOLDOUT_CONFIRMATION_SCHEMA_VERSION,
    FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION,
    apply_final_selection_contract,
    build_selection_artifact,
    compute_final_holdout_result_hash,
    validate_selection_artifact,
    validate_selection_artifact_binding,
)
from .hashing import content_hash_payload, observe_hashing, sha256_prefixed
from .hash_chain import append_hash_chained_jsonl
from .family_registry import (
    append_family_trial_registry_row,
    family_trial_registry_path,
    registry_content_hash,
)
from .experiment_registry import (
    EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE,
    append_attempt_completion,
    final_holdout_identity_hash_from_parts,
    final_holdout_hashes_from_manifest,
    objective_metric_from_manifest,
    pre_exposure_reservation_key_hash_from_parts,
    reserve_research_attempt_checked,
    research_freedom_hash,
    research_identity_from_manifest,
)
from .lineage import build_research_lineage, compute_lineage_hash
from .metrics_gate_policy import (
    metrics_gate_policy_from_acceptance_gate,
    metrics_gate_policy_hash,
)
from .metrics_contract import METRICS_SCHEMA_VERSION, ClosedTradeRecord
from .parameter_space import candidate_id, iter_parameter_candidates
from .candidate_profile import build_candidate_behavior_profile, build_candidate_profile
from .profiling import run_with_cprofile
from .report_writer import (
    summarize_candidate_result,
    write_research_report,
)
from .reproduction import create_reproduction_receipt
from .statistical_selection import (
    STATISTICAL_EVIDENCE_GENERATION_UNAVAILABLE_WARNING,
    build_statistical_selection_evidence,
    selection_universe_hash,
    statistical_validation_required,
    validate_statistical_evidence_for_candidate,
    write_statistical_selection_evidence,
)
from .return_panel import build_candidate_return_panel, write_candidate_return_panel
from .stress_suite import (
    StressSuiteContext,
    analyze_stress_suite,
    stress_suite_required,
)
from .strategy_compiler import StrategyCompiler, validate_compiled_strategy_contract
from .strategy_registry import StrategyRegistry, reconstruct_strategy_registry
from .strategy_spec import exit_policy_hash


class ResearchValidationError(ValueError):
    pass


TOP_OF_BOOK_OPTIONAL_COVERAGE_WARNING = "top_of_book_optional_coverage_warning"
TOP_OF_BOOK_OPERATOR_NEXT_ACTION = (
    "prepare a fixed dataset with uv run market-research research-freeze-dataset, "
    "rerun research-backtest, and verify top_of_book_coverage_pct"
)
PORTFOLIO_POLICY_EXECUTION_MISMATCH_REASON = "portfolio_policy_execution_mismatch"
MISSING_EXECUTED_PORTFOLIO_POLICY_EVIDENCE_REASON = (
    "missing_executed_portfolio_policy_evidence"
)
MAX_SIMULATION_INTEGRITY_SMOKE_CANDLES = 1000
ProgressCallback = Callable[[dict[str, Any]], None]
_CANDIDATE_SCENARIO_WORKER_CONTEXT: dict[str, Any] | None = None
FAST_TEST_TIER_ENV = "RESEARCH_TEST_TIER"
FAST_TEST_TIER_VALUE = "fast"
PARENT_SERIAL_TIMING_STAGES = {
    "pre_parallel_run_dataset_fingerprint",
    "pre_parallel_hash_materialization",
    "build_work_tasks",
    "append_candidate_start_events",
    "parallel_worker_pool_start",
    "report_write",
}
_WORKER_LOCAL_SNAPSHOT_CACHE: dict[str, dict[str, DatasetSnapshot]] = {}


@dataclass(frozen=True)
class EvaluationContext:
    manifest: ExperimentManifest
    manager: ResearchPathManager | None
    snapshots: dict[str, DatasetSnapshot]
    manifest_hash: str
    simulation_seed_scope_hash: str
    include_walk_forward: bool
    raw_candidate_count: int
    params: dict[str, Any]
    candidate_index: int
    scenario: ExecutionScenario
    scenario_index: int
    scenario_id: str
    progress_callback: ProgressCallback | None = None
    artifact_context: ResearchArtifactContext | None = None
    worker_pid: int | None = None
    compiled_contract: dict[str, Any] | None = None
    strategy_registry: Any | None = None


@dataclass(frozen=True)
class CandidateEvaluationResult:
    candidates: list[dict[str, Any]]
    execution_boundary: dict[str, Any]
    substage_timings: list[dict[str, Any]]
    candidate_artifact_observability: dict[str, Any]
    candidate_profile_hash_observability: dict[str, Any]


@dataclass(frozen=True)
class StatisticalSelectionAttachmentObservability:
    substage_timings: list[dict[str, Any]]
    candidate_profile_hash_observability: dict[str, Any]


class CandidateScenarioEvaluator(Protocol):
    def evaluate(
        self, work_unit: ResearchWorkUnit, context: EvaluationContext
    ) -> ResearchWorkResult: ...


class ResearchCandidateScenarioEvaluator:
    def evaluate(
        self, work_unit: ResearchWorkUnit, context: EvaluationContext
    ) -> ResearchWorkResult:
        task = _task_from_evaluation_context(work_unit=work_unit, context=context)
        return _evaluate_candidate_scenario_task(
            task=task,
            manager=context.manager,
            progress_callback=context.progress_callback,
            worker_pid=context.worker_pid,
        )


def _task_from_evaluation_context(
    *, work_unit: ResearchWorkUnit, context: EvaluationContext
) -> dict[str, Any]:
    return {
        "manifest": context.manifest,
        "snapshots": context.snapshots,
        "manifest_hash": context.manifest_hash,
        "simulation_seed_scope_hash": context.simulation_seed_scope_hash,
        "include_walk_forward": context.include_walk_forward,
        "raw_candidate_count": context.raw_candidate_count,
        "params": context.params,
        "candidate_index": context.candidate_index,
        "scenario": context.scenario,
        "scenario_index": context.scenario_index,
        "scenario_id": context.scenario_id,
        "artifact_context": context.artifact_context,
        "work_unit": work_unit,
        "compiled_contract": context.compiled_contract,
        "strategy_registry_descriptor": (
            context.strategy_registry.descriptor()
            if isinstance(context.strategy_registry, StrategyRegistry)
            else context.strategy_registry
        ),
    }


def _candidate_scenario_worker(task: dict[str, Any]) -> ResearchWorkResult:
    return _evaluate_candidate_scenario_task(
        task=task,
        manager=None,
        progress_callback=None,
        worker_pid=os.getpid(),
    )


def _initialize_candidate_scenario_worker_context(context: dict[str, Any]) -> None:
    global _CANDIDATE_SCENARIO_WORKER_CONTEXT
    material = dict(context)
    descriptor = material.pop("strategy_registry_descriptor", None)
    if descriptor is None:
        raise RuntimeError("candidate_scenario_worker_registry_descriptor_missing")
    material["strategy_registry"] = reconstruct_strategy_registry(descriptor)
    _CANDIDATE_SCENARIO_WORKER_CONTEXT = material


def _candidate_scenario_worker_from_context(task: dict[str, Any]) -> ResearchWorkResult:
    if _CANDIDATE_SCENARIO_WORKER_CONTEXT is None:
        raise RuntimeError("candidate_scenario_worker_context_missing")
    return _candidate_scenario_worker({**_CANDIDATE_SCENARIO_WORKER_CONTEXT, **task})


def _execute_parallel_candidate_work_units(
    *,
    tasks: list[dict[str, Any]],
    max_workers: int,
    process_start_method: str,
    worker_context: dict[str, Any],
    process_runtime_observability: list[dict[str, Any]],
    result_callback: Callable[[ResearchWorkResult], None],
) -> list[ResearchWorkResult]:
    kwargs: dict[str, Any] = {
        "tasks": tasks,
        "worker": _candidate_scenario_worker_from_context,
        "max_workers": max_workers,
        "process_start_method": process_start_method,
        "initializer": _initialize_candidate_scenario_worker_context,
        "initargs": (worker_context,),
        "runtime_observability_sink": process_runtime_observability,
    }
    optional_kwargs = {
        "max_in_flight_tasks": max(1, int(max_workers) * 2),
        "result_callback": result_callback,
    }
    try:
        signature = inspect.signature(execute_research_work_units_parallel)
    except (TypeError, ValueError):
        kwargs.update(optional_kwargs)
    else:
        accepted = set(signature.parameters)
        if any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        ):
            kwargs.update(optional_kwargs)
        else:
            kwargs.update(
                {
                    key: value
                    for key, value in optional_kwargs.items()
                    if key in accepted
                }
            )
    results = execute_research_work_units_parallel(**kwargs)
    if "result_callback" not in kwargs:
        for result in results:
            result_callback(result)
    return results


def _evaluate_candidate_scenario_task(
    *,
    task: dict[str, Any],
    manager: ResearchPathManager | None,
    progress_callback: ProgressCallback | None,
    worker_pid: int | None,
) -> ResearchWorkResult:
    manifest = task["manifest"]
    snapshots = task.get("snapshots")
    if snapshots is None:
        snapshots = _load_worker_task_snapshots(task=task, manifest=manifest)
    params = dict(task["params"])
    index = int(task["candidate_index"])
    scenario = task["scenario"]
    scenario_index = int(task["scenario_index"])
    scenario_id = str(task["scenario_id"])
    manifest_hash = str(task["manifest_hash"])
    simulation_seed_scope_hash = str(
        task.get("simulation_seed_scope_hash") or manifest_hash
    )
    include_walk_forward = bool(task["include_walk_forward"])
    work_unit = task["work_unit"]
    artifact_context = task.get("artifact_context")
    raw_candidate_count = int(task["raw_candidate_count"])
    param_candidate_id = candidate_id(params, index)
    compiled_payload = task.get("compiled_contract")
    registry = task.get("strategy_registry")
    if registry is None and task.get("strategy_registry_descriptor") is not None:
        registry = reconstruct_strategy_registry(task["strategy_registry_descriptor"])
    if not isinstance(registry, StrategyRegistry):
        raise ResearchValidationError("strategy_registry_required")
    compiled_contract = (
        validate_compiled_strategy_contract(dict(compiled_payload))
        if isinstance(compiled_payload, dict)
        else StrategyCompiler(registry).compile(
            strategy_name=manifest.strategy_name,
            raw_parameters=params,
            fee_rate=scenario.fee_rate,
            slippage_bps=float(scenario.slippage_bps),
            context=BacktestRunContext(
                policy_materialization_mode="research_validation"
            ),
        )
    )
    worker_observability: list[dict[str, Any]] = []
    try:
        base = _evaluate_candidate_base_result(
            manifest=manifest,
            manager=manager,
            plugin=registry.resolve(manifest.strategy_name),
            snapshots=snapshots,
            params=params,
            index=index,
            raw_candidate_count=raw_candidate_count,
            scenario=scenario,
            scenario_index=scenario_index,
            scenario_id=scenario_id,
            manifest_hash=manifest_hash,
            simulation_seed_scope_hash=simulation_seed_scope_hash,
            include_walk_forward=include_walk_forward,
            work_unit=work_unit,
            work_unit_observability=worker_observability,
            progress_callback=progress_callback,
            artifact_context=artifact_context
            if isinstance(artifact_context, ResearchArtifactContext)
            else None,
            compiled_contract=compiled_contract,
        )
        observability = worker_observability[-1] if worker_observability else {}
        if worker_pid is not None:
            observability = {**observability, "worker_pid": worker_pid}
        if isinstance(task.get("data_plane_policy"), dict):
            observability = {
                **observability,
                "data_plane_policy": dict(task["data_plane_policy"]),
            }
        return ResearchWorkResult(
            work_unit=work_unit,
            work_unit_hash=work_unit.work_unit_hash,
            candidate_index=index,
            candidate_id=param_candidate_id,
            scenario_index=scenario_index,
            scenario_id=scenario_id,
            status="completed",
            base_result=base,
            observability=observability,
        )
    except BacktestResourceLimitExceeded as exc:
        _record_failed_work_unit(
            work_unit_observability=worker_observability,
            work_unit=work_unit,
            reason=exc.reason,
            resource_guard=exc.evidence,
            limits=manifest.research_run.resource_limits,
        )

        base = _failed_candidate_base_result(
            manifest=manifest,
            work_unit=work_unit,
            candidate_index=index,
            candidate_id=param_candidate_id,
            params=params,
            scenario=scenario,
            scenario_index=scenario_index,
            scenario_id=scenario_id,
            reason=exc.reason,
            resource_guard=exc.evidence,
        )
        observability = worker_observability[-1] if worker_observability else {}
        if worker_pid is not None:
            observability = {**observability, "worker_pid": worker_pid}
        return ResearchWorkResult(
            work_unit=work_unit,
            work_unit_hash=work_unit.work_unit_hash,
            candidate_index=index,
            candidate_id=param_candidate_id,
            scenario_index=scenario_index,
            scenario_id=scenario_id,
            status="failed",
            base_result=base,
            failure_reason=exc.reason,
            failure_evidence=exc.evidence,
            observability=observability,
        )
    except ArtifactBudgetExceeded:
        raise
    except Exception as exc:
        evidence = {
            "status": "ERROR",
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "split": str(getattr(exc, "failed_split", "unknown")),
            **(
                {"audit_trace_index": getattr(exc, "audit_trace_index")}
                if isinstance(getattr(exc, "audit_trace_index", None), dict)
                else {}
            ),
        }
        _record_failed_work_unit(
            work_unit_observability=worker_observability,
            work_unit=work_unit,
            reason="candidate_exception",
            resource_guard=evidence,
            limits=manifest.research_run.resource_limits,
        )
        base = _failed_candidate_base_result(
            manifest=manifest,
            work_unit=work_unit,
            candidate_index=index,
            candidate_id=param_candidate_id,
            params=params,
            scenario=scenario,
            scenario_index=scenario_index,
            scenario_id=scenario_id,
            reason="candidate_exception",
            resource_guard=evidence,
        )
        observability = worker_observability[-1] if worker_observability else {}
        if worker_pid is not None:
            observability = {**observability, "worker_pid": worker_pid}
        return ResearchWorkResult(
            work_unit=work_unit,
            work_unit_hash=work_unit.work_unit_hash,
            candidate_index=index,
            candidate_id=param_candidate_id,
            scenario_index=scenario_index,
            scenario_id=scenario_id,
            status="failed",
            base_result=base,
            failure_reason="candidate_exception",
            failure_evidence=evidence,
            observability=observability,
        )


def _load_worker_task_snapshots(
    *, task: dict[str, Any], manifest: ExperimentManifest
) -> dict[str, DatasetSnapshot]:
    db_path = task.get("db_path")
    adapter = default_dataset_adapter_registry().resolve(manifest.dataset.source)
    if db_path is None and bool(getattr(adapter, "requires_runtime_db", False)):
        raise ResearchValidationError(
            f"runtime_context_missing:{manifest.dataset.source}:requires_runtime_db"
        )
    split_names = tuple(
        str(name) for name in task.get("split_names") or ("train", "validation")
    )
    data_plane_policy = (
        task.get("data_plane_policy")
        if isinstance(task.get("data_plane_policy"), dict)
        else {}
    )
    requested_policy = str(
        data_plane_policy.get("worker_snapshot_load_policy") or "db_reload"
    )
    if requested_policy == "db_reload":
        applied_policy = "db_reload"
    elif requested_policy == "worker_local_lazy_cache":
        cache_key = _worker_local_snapshot_cache_key(
            data_plane_policy=data_plane_policy,
            db_path=db_path,
            split_names=split_names,
            manifest=manifest,
        )
        cached = _WORKER_LOCAL_SNAPSHOT_CACHE.get(cache_key)
        if cached is not None:
            data_plane_policy["applied_snapshot_load_policy"] = (
                "worker_local_lazy_cache"
            )
            data_plane_policy["worker_local_lazy_cache_status"] = "hit"
            task["data_plane_policy"] = data_plane_policy
            return dict(cached)
        snapshots = _load_worker_task_snapshots_from_db(
            db_path=db_path,
            manifest=manifest,
            split_names=split_names,
        )
        _WORKER_LOCAL_SNAPSHOT_CACHE[cache_key] = dict(snapshots)
        data_plane_policy["applied_snapshot_load_policy"] = "worker_local_lazy_cache"
        data_plane_policy["worker_local_lazy_cache_status"] = "miss_stored"
        task["data_plane_policy"] = data_plane_policy
        return snapshots
    elif requested_policy == "memory_mapped_readonly":
        raise ResearchValidationError(
            "worker_snapshot_load_policy_unsupported:memory_mapped_readonly"
        )
    else:
        raise ResearchValidationError(
            f"worker_snapshot_load_policy_unsupported:{requested_policy}"
        )
    data_plane_policy["applied_snapshot_load_policy"] = applied_policy
    task["data_plane_policy"] = data_plane_policy
    return _load_worker_task_snapshots_from_db(
        db_path=db_path,
        manifest=manifest,
        split_names=split_names,
    )


def _worker_local_snapshot_cache_key(
    *,
    data_plane_policy: dict[str, Any],
    db_path: object,
    split_names: tuple[str, ...],
    manifest: ExperimentManifest,
) -> str:
    material = data_plane_policy.get("cache_key_material")
    if not isinstance(material, dict):
        raise ResearchValidationError("worker_local_lazy_cache_key_material_missing")
    manifest_hash = str(material.get("manifest_hash") or "")
    dataset_hashes = material.get("dataset_hashes")
    policy_split_names = material.get("split_names")
    if (
        not manifest_hash
        or not isinstance(dataset_hashes, dict)
        or not isinstance(policy_split_names, list)
    ):
        raise ResearchValidationError("worker_local_lazy_cache_key_material_incomplete")
    requested_split_names = tuple(str(name) for name in split_names)
    requested_dataset_hashes = {
        name: str(dataset_hashes.get(name) or "") for name in requested_split_names
    }
    if not requested_dataset_hashes or any(
        not value for value in requested_dataset_hashes.values()
    ):
        raise ResearchValidationError("worker_local_lazy_cache_dataset_hash_missing")
    return sha256_prefixed(
        {
            "policy": "worker_local_lazy_cache",
            "manifest_hash": manifest_hash,
            "dataset_hashes": requested_dataset_hashes,
            "policy_split_names": [str(name) for name in policy_split_names],
            "requested_split_names": list(requested_split_names),
            "db_path": str(db_path) if db_path is not None else None,
            "artifact_ref": (
                {
                    "uri": manifest.dataset.artifact_ref.artifact_manifest_uri,
                    "hash": manifest.dataset.artifact_ref.artifact_manifest_hash,
                }
                if manifest.dataset.artifact_ref is not None
                else None
            ),
        }
    )


def _load_worker_task_snapshots_from_db(
    *,
    db_path: object,
    manifest: ExperimentManifest,
    split_names: tuple[str, ...],
) -> dict[str, DatasetSnapshot]:
    run_context = DatasetRunContext()
    if any(name.startswith("window_") for name in split_names):
        if manifest.walk_forward is None:
            raise ResearchValidationError(
                "parallel_worker_walk_forward_manifest_missing"
            )
        return _load_walk_forward_snapshots(
            db_path=db_path,
            manifest=manifest,
            windows=rolling_walk_forward_windows(manifest),
            run_context=run_context,
        )
    return {
        split_name: load_dataset_split(
            db_path=db_path,
            manifest=manifest,
            split_name=split_name,
            run_context=run_context,
        )
        for split_name in split_names
    }


def _emit_progress(callback: ProgressCallback | None, **payload: Any) -> None:
    if callback is None:
        return
    callback(payload)


def _apply_memory_admission_policy(
    *,
    manifest: ExperimentManifest,
    execution_plan: ResearchExecutionPlan,
) -> tuple[ExperimentManifest, dict[str, Any]]:
    estimate = dict((execution_plan.payload.get("workload_estimate") or {}))
    status = str(estimate.get("memory_budget_status") or "NOT_EVALUATED")
    safe_workers = int(
        estimate.get("safe_max_workers_by_memory_budget")
        or manifest.research_run.execution.max_workers
    )
    requested_workers = int(manifest.research_run.execution.max_workers)
    policy = str(
        getattr(
            manifest.research_run.resource_limits,
            "memory_admission_policy",
            "fail_fast",
        )
    )
    payload = {
        "policy": policy,
        "status": status,
        "requested_max_workers": requested_workers,
        "safe_max_workers_by_memory_budget": safe_workers,
        "memory_budget_reasons": list(estimate.get("memory_budget_reasons") or []),
        "effective_max_workers": requested_workers,
        "max_in_flight_tasks": max(1, requested_workers * 2),
    }
    if status != "WARN":
        return manifest, payload
    if policy == "fail_fast":
        payload["action"] = "fail_fast"
        raise ResearchValidationError("memory_admission_budget_exceeded")
    if policy in {"cap_workers", "batch_candidates"}:
        capped_workers = max(1, min(requested_workers, safe_workers))
        payload["action"] = (
            "cap_workers" if policy == "cap_workers" else "batch_candidates"
        )
        payload["effective_max_workers"] = capped_workers
        payload["max_in_flight_tasks"] = max(1, capped_workers * 2)
        adjusted_execution = replace(
            manifest.research_run.execution, max_workers=capped_workers
        )
        adjusted_run = replace(manifest.research_run, execution=adjusted_execution)
        return replace(manifest, research_run=adjusted_run), payload
    raise ResearchValidationError(f"memory_admission_policy_unsupported:{policy}")


def _execution_plan_with_memory_admitted_data_plane_policy(
    *,
    execution_plan: ResearchExecutionPlan,
    memory_admission: dict[str, Any],
) -> ResearchExecutionPlan:
    effective_workers = memory_admission.get("effective_max_workers")
    if effective_workers is None:
        return execution_plan
    data_plane_policy = execution_plan.payload.get("data_plane_policy")
    if not isinstance(data_plane_policy, dict):
        return execution_plan

    effective_workers = max(1, int(effective_workers))
    if int(data_plane_policy.get("effective_max_workers") or 0) == effective_workers:
        return execution_plan

    payload = dict(execution_plan.payload)
    adjusted_policy = dict(data_plane_policy)
    adjusted_policy["effective_max_workers"] = effective_workers
    payload["data_plane_policy"] = adjusted_policy

    workload_estimate = payload.get("workload_estimate")
    if isinstance(workload_estimate, dict):
        adjusted_workload = dict(workload_estimate)
        adjusted_workload["data_plane_policy"] = adjusted_policy
        payload["workload_estimate"] = adjusted_workload

    return ResearchExecutionPlan(payload=payload)


def _apply_execution_plan_resource_policy(
    *,
    manifest: ExperimentManifest,
    execution_plan: ResearchExecutionPlan,
) -> ExperimentManifest:
    plan = execution_plan.payload
    resource_plan = (
        plan.get("resource_plan") if isinstance(plan.get("resource_plan"), dict) else {}
    )
    effective_mode = str(
        resource_plan.get("effective_execution_mode")
        or plan.get("effective_execution_mode")
        or manifest.research_run.execution.mode
    )
    if (
        str(
            resource_plan.get("requested_execution_mode")
            or plan.get("requested_execution_mode")
            or ""
        )
        == "auto"
    ):
        effective_workers = int(
            resource_plan.get("effective_max_workers")
            or plan.get("max_workers")
            or manifest.research_run.execution.max_workers
        )
        effective_work_unit = str(
            resource_plan.get("work_unit_type")
            or plan.get("work_unit_type")
            or manifest.research_run.execution.work_unit
        )
        adjusted_execution = replace(
            manifest.research_run.execution,
            mode=effective_mode,
            max_workers=effective_workers,
            work_unit=effective_work_unit,
        )
        if adjusted_execution == manifest.research_run.execution:
            return manifest
        return replace(
            manifest,
            research_run=replace(manifest.research_run, execution=adjusted_execution),
        )
    effective_workers = int(
        resource_plan.get("effective_max_workers")
        or plan.get("max_workers")
        or manifest.research_run.execution.max_workers
    )
    effective_work_unit = str(
        resource_plan.get("work_unit_type")
        or plan.get("work_unit_type")
        or manifest.research_run.execution.work_unit
    )
    changed = (
        effective_workers != int(manifest.research_run.execution.max_workers)
        or effective_work_unit != manifest.research_run.execution.work_unit
    )
    if not changed:
        return manifest
    adjusted_execution = replace(
        manifest.research_run.execution,
        max_workers=effective_workers,
        work_unit=effective_work_unit,
    )
    return replace(
        manifest,
        research_run=replace(manifest.research_run, execution=adjusted_execution),
    )


def _canonicalize_runner_default_execution(
    manifest: ExperimentManifest,
) -> ExperimentManifest:
    raw = manifest.raw if isinstance(manifest.raw, dict) else {}
    research_run = raw.get("research_run")
    if isinstance(research_run, dict) and isinstance(
        research_run.get("execution"), dict
    ):
        return manifest
    return manifest


def _stage_timing(stage: str, started_at: float, **details: Any) -> dict[str, Any]:
    payload = {
        "stage": stage,
        "wall_seconds": round(time.perf_counter() - started_at, 6),
    }
    payload.update(details)
    return payload


def _prefixed_stage_timings(
    prefix: str, timings: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    prefixed: list[dict[str, Any]] = []
    for item in timings:
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "").strip()
        if not stage:
            continue
        if stage == "candidate_profile_hash" or stage.startswith(
            "candidate_profile_hash."
        ):
            prefixed.append(
                {"stage": stage, **{k: v for k, v in item.items() if k != "stage"}}
            )
        if stage in PARENT_SERIAL_TIMING_STAGES:
            prefixed.append(
                {"stage": stage, **{k: v for k, v in item.items() if k != "stage"}}
            )
        prefixed.append(
            {
                "stage": f"{prefix}.{stage}",
                **{k: v for k, v in item.items() if k != "stage"},
            }
        )
    return prefixed


def _empty_hash_observability() -> dict[str, Any]:
    return {
        "hash_call_count": 0,
        "observed_hash_payload_bytes": 0,
        "largest_hash_payload_bytes": 0,
        "largest_hash_label": None,
    }


def _merge_hash_observability(target: dict[str, Any], observed: dict[str, Any]) -> None:
    target["hash_call_count"] = int(target.get("hash_call_count") or 0) + int(
        observed.get("hash_call_count") or 0
    )
    target["observed_hash_payload_bytes"] = int(
        target.get("observed_hash_payload_bytes") or 0
    ) + int(observed.get("observed_hash_payload_bytes") or 0)
    observed_largest = int(observed.get("largest_hash_payload_bytes") or 0)
    if observed_largest > int(target.get("largest_hash_payload_bytes") or 0):
        target["largest_hash_payload_bytes"] = observed_largest
        target["largest_hash_label"] = observed.get("largest_hash_label")


def _estimated_strategy_runs(
    *,
    candidate_count: int,
    scenario_count: int,
    split_count: int,
    include_walk_forward: bool,
    walk_forward_split_count: int,
) -> int:
    base_split_count = split_count
    if include_walk_forward:
        base_split_count = max(0, split_count - walk_forward_split_count)
    return (
        int(candidate_count)
        * int(scenario_count)
        * int(base_split_count + walk_forward_split_count)
    )


def _parameter_grid_size(manifest: ExperimentManifest) -> int:
    size = 1
    for values in manifest.parameter_space.values():
        size *= len(values)
    return size


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _validation_registry_required(manifest: ExperimentManifest) -> bool:
    return manifest.research_classification in {
        "exploratory",
        "validated_candidate",
        "validated_candidate",
    }


def _research_artifact_root(manager: ResearchPathManager, experiment_id: str) -> Path:
    root = manager.research_artifact_path(experiment_id)
    project_root = manager.project_root.resolve()
    if ResearchPathManager.is_within(root.resolve(), project_root):
        raise ResearchValidationError(
            f"research derived artifact path must be outside repository: {root.resolve()}"
        )
    return root


def _candidate_events_path(manager: ResearchPathManager, experiment_id: str) -> Path:
    return _research_artifact_root(manager, experiment_id) / "candidate_events.jsonl"


def _candidate_result_path(
    manager: ResearchPathManager, experiment_id: str, candidate_id: str
) -> Path:
    return (
        _research_artifact_root(manager, experiment_id)
        / "candidate_results"
        / f"{candidate_id}.json"
    )


def _candidate_detail_result_path(
    manager: ResearchPathManager,
    experiment_id: str,
    *,
    candidate_id: str,
    scenario_id: str,
    work_unit_hash: str,
) -> Path:
    safe_work_hash = str(work_unit_hash).replace(":", "_")
    return (
        _research_artifact_root(manager, experiment_id)
        / "candidate_detail_results"
        / str(candidate_id)
        / f"{scenario_id}_{safe_work_hash}.json"
    )


def _candidate_failure_path(
    manager: ResearchPathManager, experiment_id: str, candidate_id: str
) -> Path:
    return (
        _research_artifact_root(manager, experiment_id)
        / "candidate_failures"
        / f"{candidate_id}.json"
    )


def _reserve_experiment_attempt(
    *,
    manifest: ExperimentManifest,
    manager: ResearchPathManager,
    snapshots: dict[str, DatasetSnapshot],
    quality_reports: dict[str, DatasetQualityReport],
    manifest_path: str | None,
    command_name: str,
    command_args: dict[str, Any] | None,
    repository_version: str | None,
    created_at: str | None,
) -> dict[str, Any] | None:
    has_final_holdout = manifest.dataset.split.final_holdout is not None
    if not has_final_holdout:
        return None
    if not _validation_registry_required(manifest):
        return None
    parameter_grid_size = _parameter_grid_size(manifest)
    declared_attempt = _optional_int(manifest.raw.get("attempt_index"))
    declared_reuse = _optional_int(manifest.raw.get("holdout_reuse_count"))
    identity = research_identity_from_manifest(manifest)
    experiment_family_id = str(identity["experiment_family_id"])
    hypothesis_id = str(identity["hypothesis_id"])
    hypothesis_status = str(identity["hypothesis_status"])
    split_hashes = {
        name: snapshot.snapshot_fingerprint_hash()
        for name, snapshot in snapshots.items()
    }
    final_holdout_loaded = "final_holdout" in snapshots
    dataset_quality_hash = (
        combined_dataset_quality_hash(tuple(quality_reports.values()))
        if final_holdout_loaded
        else None
    )
    artifact_snapshot = next(
        (item for item in snapshots.values() if item.artifact_id), None
    )
    artifact_evidence = (
        {
            "artifact_id": artifact_snapshot.artifact_id,
            "artifact_manifest_hash": artifact_snapshot.artifact_manifest_hash,
            "artifact_content_hash": artifact_snapshot.artifact_content_hash,
            "artifact_schema_hash": artifact_snapshot.artifact_schema_hash,
            "verification_status": artifact_snapshot.verification.overall_status.value
            if artifact_snapshot.verification
            else "UNAVAILABLE",
        }
        if artifact_snapshot is not None
        else {}
    )
    holdout_snapshot = snapshots.get("final_holdout")
    holdout_evidence = (
        {
            "requested_range": holdout_snapshot.date_range.as_dict(),
            "snapshot_query_hash": holdout_snapshot.snapshot_query_hash(),
            "snapshot_data_hash": holdout_snapshot.snapshot_data_hash(),
            "snapshot_fingerprint_hash": holdout_snapshot.snapshot_fingerprint_hash(),
            "quality_hash": quality_reports["final_holdout"].content_hash,
        }
        if holdout_snapshot is not None and "final_holdout" in quality_reports
        else {}
    )
    if final_holdout_loaded:
        holdout_hashes = final_holdout_hashes_from_manifest(
            manifest=manifest,
            final_holdout_split_hash=split_hashes.get("final_holdout"),
            dataset_quality_hash=dataset_quality_hash,
            dataset_artifact=artifact_evidence,
            final_holdout_evidence=holdout_evidence,
        )
    else:
        holdout_payload = (
            manifest.dataset.split.final_holdout.as_dict()
            if manifest.dataset.split.final_holdout is not None
            else None
        )
        identity_hash = final_holdout_identity_hash_from_parts(
            dataset_source=manifest.dataset.source,
            market=manifest.market,
            interval=manifest.interval,
            final_holdout=holdout_payload,
        )
        objective_metric = objective_metric_from_manifest(manifest)
        pre_exposure_key = pre_exposure_reservation_key_hash_from_parts(
            strategy_name=manifest.strategy_name,
            market=manifest.market,
            interval=manifest.interval,
            final_holdout=holdout_payload,
            objective_metric=objective_metric,
            dataset_artifact_evidence_hash=(
                sha256_prefixed(
                    {
                        "artifact_id": artifact_evidence.get("artifact_id"),
                        "artifact_manifest_hash": artifact_evidence.get(
                            "artifact_manifest_hash"
                        ),
                        "artifact_content_hash": artifact_evidence.get(
                            "artifact_content_hash"
                        ),
                        "artifact_schema_hash": artifact_evidence.get(
                            "artifact_schema_hash"
                        ),
                        "verification_status": artifact_evidence.get(
                            "verification_status"
                        ),
                    }
                )
                if artifact_evidence
                else None
            ),
        )
        holdout_hashes = {
            "final_holdout_identity_hash": identity_hash,
            "final_holdout_content_hash": None,
            "final_holdout_reuse_key_hash_v1": identity_hash,
            "final_holdout_reuse_key_hash": None,
            "final_holdout_reuse_key_schema_version": None,
            "final_holdout_reuse_key_hash_v2": None,
            "pre_exposure_reservation_key_hash": pre_exposure_key,
            "pre_exposure_reservation_key_schema_version": 1,
            "objective_metric": objective_metric,
            "final_holdout_fingerprint": identity_hash,
        }
    base_payload = {
        "run_id": manifest.experiment_id,
        "experiment_family_id": experiment_family_id,
        "hypothesis_id": hypothesis_id,
        "hypothesis_version": identity["hypothesis_version"],
        "hypothesis_contract_hash": identity["hypothesis_contract_hash"],
        "hypothesis_semantic_fingerprint": identity["hypothesis_semantic_fingerprint"],
        "hypothesis_status": hypothesis_status,
        "hypothesis_identity_source": identity["hypothesis_identity_source"],
        "experiment_family_identity_source": identity[
            "experiment_family_identity_source"
        ],
        "pre_registered_at": identity["pre_registered_at"],
        "registration_evidence_hash": identity["registration_evidence_hash"],
        "experiment_id": manifest.experiment_id,
        "manifest_hash": manifest.manifest_hash(),
        "manifest_metadata_hash": sha256_prefixed(
            {
                "experiment_family_id": experiment_family_id,
                "hypothesis_id": hypothesis_id,
                "hypothesis_version": identity["hypothesis_version"],
                "hypothesis_contract_hash": identity["hypothesis_contract_hash"],
                "hypothesis_semantic_fingerprint": identity[
                    "hypothesis_semantic_fingerprint"
                ],
                "hypothesis_status": hypothesis_status,
                "attempt_index": manifest.raw.get("attempt_index"),
                "holdout_reuse_count": manifest.raw.get("holdout_reuse_count"),
                "pre_registered_at": identity["pre_registered_at"],
                "registration_evidence_hash": identity["registration_evidence_hash"],
            }
        ),
        "dataset_snapshot_id": manifest.dataset.snapshot_id,
        "dataset_content_hash": combined_dataset_fingerprint(tuple(snapshots.values()))
        if final_holdout_loaded
        else None,
        "dataset_quality_hash": dataset_quality_hash,
        "dataset_artifact": artifact_evidence,
        "dataset_split_evidence": {
            name: {
                "requested_range": snapshot.date_range.as_dict(),
                "snapshot_data_hash": snapshot.snapshot_data_hash(),
                "snapshot_query_hash": snapshot.snapshot_query_hash(),
                "snapshot_fingerprint_hash": snapshot.snapshot_fingerprint_hash(),
                "quality_hash": quality_reports[name].content_hash,
                "verification_status": snapshot.verification.overall_status.value
                if snapshot.verification
                else "UNAVAILABLE",
            }
            for name, snapshot in snapshots.items()
        },
        "train_split_hash": split_hashes.get("train"),
        "validation_split_hash": split_hashes.get("validation"),
        "final_holdout_split_hash": split_hashes.get("final_holdout"),
        "final_holdout_content_pending_until_completion": not final_holdout_loaded,
        **holdout_hashes,
        "parameter_space_hash": sha256_prefixed(manifest.parameter_space),
        "parameter_grid_size": parameter_grid_size,
        "candidate_count": None,
        "declared_attempt_index": declared_attempt,
        "declared_holdout_reuse_count": declared_reuse,
        "statistical_evidence_hash": None,
        "return_panel_hash": None,
        "validation_artifact_hash": None,
        "selected_candidate_id": None,
        "repository_version": repository_version,
        "manifest_path": manifest_path,
        "command_name": command_name,
        "command_args_hash": sha256_prefixed(command_args or {}),
    }
    reservation = reserve_research_attempt_checked(
        manager=manager,
        base_payload=base_payload,
        statistical_validation_contract=(
            manifest.statistical_validation.as_dict()
            if manifest.statistical_validation is not None
            else None
        ),
        created_at=created_at,
    )
    if not reservation.get("accepted", True):
        reasons = [str(item) for item in reservation.get("reasons") or []]
        raise ResearchValidationError(
            "experiment_registry_preflight_failed: " + ",".join(sorted(reasons))
        )
    # Completion binding intentionally cannot run before final-holdout exposure:
    # this row has only the explicit pre-exposure reservation identity.
    reasons = []
    reservation["gate_fail_reasons"] = reasons
    reservation["gate_result"] = "FAIL" if reasons else "PASS"
    return reservation


def _data_dir_relative_ref(manager: ResearchPathManager, path: Path) -> str:
    return path.resolve().relative_to(manager.data_dir().resolve()).as_posix()


def _closed_trades_for_stress_suite(
    *,
    manager: ResearchPathManager,
    base: dict[str, Any],
    split_name: str,
) -> tuple[ClosedTradeRecord, ...]:
    key = f"{split_name}_closed_trades"
    existing = base.get(key)
    if existing:
        return _closed_trade_records_from_payload(existing, source=key)

    expected_count = int(base.get(f"{split_name}_closed_trade_count") or 0)
    if expected_count <= 0:
        return ()

    detail = _load_candidate_detail_base_result(manager=manager, compact=base)
    payload = detail.get(key) if isinstance(detail, dict) else None
    records = _closed_trade_records_from_payload(payload or (), source=key)
    if len(records) != expected_count:
        raise ResearchValidationError(
            f"{split_name}_closed_trades_detail_count_mismatch: "
            f"expected={expected_count} actual={len(records)}"
        )
    return records


def _load_candidate_detail_base_result(
    *, manager: ResearchPathManager, compact: dict[str, Any]
) -> dict[str, Any]:
    detail_ref = str(compact.get("detail_artifact_ref") or "").strip()
    detail_path_value = str(compact.get("detail_artifact_path") or "").strip()
    if detail_ref:
        detail_path = manager.data_dir().resolve() / detail_ref
    elif detail_path_value:
        detail_path = Path(detail_path_value)
    else:
        raise ResearchValidationError(
            "candidate_detail_artifact_missing_for_stress_suite"
        )

    resolved = detail_path.resolve()
    data_dir = manager.data_dir().resolve()
    if not ResearchPathManager.is_within(resolved, data_dir):
        raise ResearchValidationError(
            f"candidate_detail_artifact_outside_data_dir: {resolved}"
        )
    if not resolved.is_file():
        raise ResearchValidationError(
            f"candidate_detail_artifact_not_found: {resolved}"
        )

    artifact = json.loads(resolved.read_text(encoding="utf-8"))
    if (
        not isinstance(artifact, dict)
        or artifact.get("artifact_type") != "candidate_detail_result"
    ):
        raise ResearchValidationError("candidate_detail_artifact_malformed")
    detail_payload = artifact.get("base_result")
    if not isinstance(detail_payload, dict):
        raise ResearchValidationError("candidate_detail_base_result_missing")
    expected_hash = str(compact.get("detail_artifact_hash") or "")
    actual_hash = sha256_prefixed(
        detail_payload, label="candidate_detail_artifact_hash"
    )
    if expected_hash and actual_hash != expected_hash:
        raise ResearchValidationError("candidate_detail_artifact_hash_mismatch")
    embedded_hash = str(artifact.get("detail_artifact_hash") or "")
    if embedded_hash and embedded_hash != actual_hash:
        raise ResearchValidationError(
            "candidate_detail_artifact_embedded_hash_mismatch"
        )
    return detail_payload


def _closed_trade_records_from_payload(
    payload: Any, *, source: str
) -> tuple[ClosedTradeRecord, ...]:
    if not isinstance(payload, (list, tuple)):
        raise ResearchValidationError(f"{source}_malformed")
    allowed = {field.name for field in fields(ClosedTradeRecord)}
    records: list[ClosedTradeRecord] = []
    for item in payload:
        if isinstance(item, ClosedTradeRecord):
            records.append(item)
            continue
        if not isinstance(item, dict):
            raise ResearchValidationError(f"{source}_item_malformed")
        values = {key: item[key] for key in allowed if key in item}
        records.append(ClosedTradeRecord(**values))
    return tuple(records)


def _append_candidate_event(
    *,
    manager: ResearchPathManager,
    manifest: ExperimentManifest,
    event: dict[str, Any],
    artifact_context: ResearchArtifactContext | None = None,
) -> None:
    if not manifest.research_run.artifact_policy.candidate_journal:
        return
    store = artifact_context or ResearchArtifactContext(
        manager=manager,
        experiment_id=manifest.experiment_id,
        budget=_artifact_budget_from_limits(manifest.research_run.resource_limits),
    )
    append_hash_chained_jsonl(
        store=store,
        path=_candidate_events_path(manager, manifest.experiment_id),
        payload={
            "experiment_id": manifest.experiment_id,
            "manifest_hash": manifest.manifest_hash(),
            **event,
        },
        label="candidate_event",
    )


def _backtest_context(
    *,
    manifest: ExperimentManifest,
    manager: ResearchPathManager | None,
    candidate_id: str,
    scenario_id: str,
    scenario_index: int,
    split_name: str,
    dataset_content_hash: str,
    parameter_values: dict[str, Any],
    progress_callback: ProgressCallback | None,
    artifact_context: ResearchArtifactContext | None = None,
) -> BacktestRunContext:
    limits = manifest.research_run.resource_limits
    heartbeat = manifest.research_run.heartbeat
    audit_trace = None
    if manifest.research_run.audit_trail.complete_external:
        if manager is None:
            raise ResearchValidationError(
                "audit_trace_requires_main_process_artifact_manager"
            )
        audit_trace = AuditTraceScope(
            manager=manager,
            experiment_id=manifest.experiment_id,
            manifest_hash=manifest.manifest_hash(),
            dataset_content_hash=dataset_content_hash,
            candidate_id=candidate_id,
            scenario_id=scenario_id,
            scenario_index=scenario_index,
            split=split_name,
            parameter_values=parameter_values,
            artifact_budget=_artifact_budget_from_limits(limits),
            artifact_context=artifact_context,
        )
    context_progress_callback = None
    if progress_callback is not None or manager is not None:

        def journal_progress(event: dict[str, Any]) -> None:
            _progress_and_journal(
                callback=progress_callback,
                manager=manager,
                manifest=manifest,
                event=event,
                artifact_context=artifact_context,
            )

        context_progress_callback = journal_progress
    return BacktestRunContext(
        experiment_id=manifest.experiment_id,
        candidate_id=candidate_id,
        scenario_id=scenario_id,
        scenario_index=scenario_index,
        split_name=split_name,
        report_detail=manifest.research_run.report_detail,
        diagnostic_mode=manifest.research_run.diagnostic_mode,
        audit_trail_policy=manifest.research_run.audit_trail,
        resource_limits=BacktestResourceLimits(
            max_runtime_s_per_candidate_split=limits.max_runtime_s_per_candidate_split,
            max_decisions_retained=limits.max_decisions_retained,
            max_trades=limits.max_trades,
            max_equity_points_retained=limits.max_equity_points_retained,
            max_rss_mb=limits.max_rss_mb,
        ),
        heartbeat=BacktestHeartbeatPolicy(
            interval_s=heartbeat.interval_s,
            bar_interval=heartbeat.bar_interval,
        ),
        progress_callback=context_progress_callback,
        audit_trace=audit_trace,
        participation_count_basis=manifest.acceptance_gate.participation_count_basis
        or "filled",
    )


def _artifact_budget_from_limits(limits) -> ArtifactBudget:
    return ArtifactBudget(
        max_artifact_bytes=limits.max_artifact_bytes,
        max_audit_stream_rows=limits.max_audit_stream_rows,
        max_audit_stream_bytes=limits.max_audit_stream_bytes,
        max_artifact_file_count=limits.max_artifact_file_count,
    )


def _validate_run_purpose_dataset_scope(
    *,
    manifest: ExperimentManifest,
    snapshots: dict[str, DatasetSnapshot],
) -> None:
    if manifest.research_run.run_purpose != "simulation_integrity_smoke":
        return
    oversized = {
        split_name: len(snapshot.candles)
        for split_name, snapshot in sorted(snapshots.items())
        if len(snapshot.candles) > MAX_SIMULATION_INTEGRITY_SMOKE_CANDLES
    }
    if oversized:
        details = ",".join(f"{split}:{count}" for split, count in oversized.items())
        raise ResearchValidationError(
            "simulation_integrity_smoke_split_too_large:"
            f"max_candles={MAX_SIMULATION_INTEGRITY_SMOKE_CANDLES}:{details}"
        )


def _progress_and_journal(
    *,
    callback: ProgressCallback | None,
    manager: ResearchPathManager | None,
    manifest: ExperimentManifest | None,
    event: dict[str, Any],
    artifact_context: ResearchArtifactContext | None = None,
) -> None:
    _emit_progress(callback, **event)
    if (
        manager is not None
        and manifest is not None
        and event.get("stage")
        in {"heartbeat", "candidate_start", "candidate_failure", "candidate_complete"}
    ):
        _append_candidate_event(
            manager=manager,
            manifest=manifest,
            event=event,
            artifact_context=artifact_context,
        )


def _validate_parallel_research_run_policy(manifest: ExperimentManifest) -> None:
    if manifest.research_run.execution.mode != "parallel":
        return
    if manifest.research_run.audit_trail.complete_external:
        raise ResearchValidationError(
            "parallel_execution_complete_external_audit_trail_not_supported"
        )
    if manifest.research_run.artifact_policy.full_decisions_external_jsonl:
        raise ResearchValidationError(
            "parallel_execution_full_decisions_external_jsonl_not_supported"
        )


def _candidate_evaluator_kind(
    candidate_evaluator: CandidateScenarioEvaluator | None,
) -> str:
    return (
        "validation_evaluator"
        if candidate_evaluator is None
        else "injected_contract_evaluator"
    )


def _fast_test_tier_active() -> bool:
    return (
        os.environ.get(FAST_TEST_TIER_ENV, "").strip().lower() == FAST_TEST_TIER_VALUE
    )


def _enforce_fast_tier_research_runner_policy(
    *,
    candidate_evaluator: CandidateScenarioEvaluator | None,
    entrypoint: str,
) -> None:
    if not _fast_test_tier_active() or candidate_evaluator is not None:
        return
    raise ResearchValidationError(
        f"{entrypoint}_validation_evaluator_blocked_in_fast_test_tier"
    )


def _execution_boundary_observability(
    *,
    manifest: ExperimentManifest,
    candidate_evaluator: CandidateScenarioEvaluator | None,
    parallel_executor_used: bool,
    process_runtime_observability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evaluator_kind = _candidate_evaluator_kind(candidate_evaluator)
    requested_mode = manifest.research_run.execution.mode
    if parallel_executor_used:
        actual_execution_mode = "parallel_worker_initializer"
        actual_worker_context_mode = "worker_initializer"
    elif candidate_evaluator is None:
        actual_execution_mode = "serial_validation_evaluator"
        actual_worker_context_mode = "in_process_validation"
    else:
        actual_execution_mode = "contract_evaluator_in_process"
        actual_worker_context_mode = "in_process_contract"
    execution_scenarios = required_execution_scenarios(
        manifest.execution_model.scenarios
    )
    requested_task_count = 0
    if requested_mode == "parallel":
        requested_task_count = parallel_work_task_count(
            candidate_count=len(iter_parameter_candidates(manifest.parameter_space)),
            scenario_count=len(execution_scenarios),
            split_count=1,
            work_unit=manifest.research_run.execution.work_unit,
        )
    payload = {
        "requested_execution_mode": requested_mode,
        "requested_max_workers": manifest.research_run.execution.max_workers,
        "requested_process_start_method": manifest.research_run.execution.process_start_method,
        "requested_work_unit_type": manifest.research_run.execution.work_unit,
        "candidate_evaluator_kind": evaluator_kind,
        "actual_execution_mode": actual_execution_mode,
        "actual_worker_context_mode": actual_worker_context_mode,
        "parallel_executor_used": parallel_executor_used,
        "validation_evaluator_used": candidate_evaluator is None,
        "contract_evaluator_used": candidate_evaluator is not None,
        "requested_parallel_task_count": requested_task_count,
        "actual_parallel_task_count": requested_task_count
        if parallel_executor_used
        else 0,
    }
    if process_runtime_observability is not None:
        payload.update(process_runtime_observability)
    return payload


def _execution_observability_payload(
    *,
    manifest: ExperimentManifest,
    stage_timings: list[dict[str, Any]],
    work_unit_observability: list[dict[str, Any]],
    execution_boundary: dict[str, Any],
    snapshots: dict[str, DatasetSnapshot],
) -> dict[str, Any]:
    worker_pid_set = _observed_worker_pid_set(work_unit_observability)
    parallel_executor_used = bool(execution_boundary.get("parallel_executor_used"))
    parallel_worker_timing = _last_stage_timing(
        stage_timings, "parallel_worker_execution"
    )
    requested_workers = int(
        execution_boundary.get("research_max_workers_requested")
        or manifest.research_run.execution.max_workers
    )
    effective_workers = int(
        execution_boundary.get("research_max_workers_effective")
        or (
            manifest.research_run.execution.max_workers if parallel_executor_used else 1
        )
    )
    observed_worker_count = len(worker_pid_set) if parallel_executor_used else 0
    work_unit_wall_seconds = _work_unit_wall_seconds(work_unit_observability)
    worker_warning_reasons: list[str] = []
    observation_warning_reasons: list[str] = []
    if effective_workers < requested_workers:
        worker_warning_reasons.append("effective_workers_below_requested")
    if parallel_executor_used and observed_worker_count < effective_workers:
        observation_warning_reasons.append("observed_workers_below_effective")
    parent_serial_stage_timings = [
        dict(item)
        for item in stage_timings
        if str(item.get("stage") or "") in PARENT_SERIAL_TIMING_STAGES
    ]
    parent_serial_seconds = sum(
        float(item.get("wall_seconds") or 0.0) for item in parent_serial_stage_timings
    )
    worker_seconds = (
        float(parallel_worker_timing.get("wall_seconds") or 0.0)
        if parallel_worker_timing
        else 0.0
    )
    if (
        parallel_executor_used
        and worker_seconds > 0.0
        and parent_serial_seconds > worker_seconds
    ):
        observation_warning_reasons.append("parent_serial_stage_dominates_wall_time")
    bottleneck_reasons: list[str] = []
    if (
        worker_seconds > 0.0
        and parent_serial_seconds > worker_seconds
        and parent_serial_stage_timings
    ):
        dominant = max(
            parent_serial_stage_timings,
            key=lambda item: float(item.get("wall_seconds") or 0.0),
        )
        bottleneck_reasons.append(
            f"parent_serial_stage_dominates_wall_time:{dominant.get('stage')}"
        )
    tail_skew_ratio = _tail_skew_ratio(work_unit_wall_seconds)
    if tail_skew_ratio is not None and tail_skew_ratio >= 2.0:
        observation_warning_reasons.append("parallel_tail_skew_detected")
    available_parallel_work_tasks = int(
        execution_boundary.get("available_parallel_work_tasks")
        or execution_boundary.get("actual_parallel_task_count")
        or 0
    )
    parallel_efficiency = parallel_efficiency_payload(
        available_work_tasks=available_parallel_work_tasks,
        requested_max_workers=requested_workers,
        effective_max_workers=effective_workers,
        work_unit=manifest.research_run.execution.work_unit,
        effective_worker_source=(
            "runtime_process_policy"
            if execution_boundary.get("research_max_workers_effective") is not None
            else "manifest"
        ),
        observed_worker_count=observed_worker_count if parallel_executor_used else None,
        worker_warning_reasons=worker_warning_reasons,
        worker_observation_warning_reasons=observation_warning_reasons,
    )
    return {
        "schema_version": 2,
        "stage_timings": stage_timings,
        "work_units": work_unit_observability,
        "worker_context_mode": execution_boundary["actual_worker_context_mode"],
        "parallel_task_count": execution_boundary["actual_parallel_task_count"],
        "max_workers": manifest.research_run.execution.max_workers,
        "work_unit_type": manifest.research_run.execution.work_unit,
        "approx_snapshot_candle_count": sum(
            len(snapshot.candles) for snapshot in snapshots.values()
        ),
        "parallel_executor_used": parallel_executor_used,
        "requested_max_workers": requested_workers,
        "research_max_workers_requested": requested_workers,
        "research_max_workers_effective": effective_workers,
        "effective_process_start_method": execution_boundary.get(
            "effective_process_start_method"
        ),
        "worker_pid_set": worker_pid_set,
        "observed_worker_count": observed_worker_count,
        "worker_budget_warning_reasons": sorted(set(worker_warning_reasons)),
        "worker_observation_warning_reasons": sorted(set(observation_warning_reasons)),
        "parallel_efficiency": parallel_efficiency,
        "memory_admission": dict(execution_boundary.get("memory_admission") or {}),
        "resource_plan": dict(execution_boundary.get("resource_plan") or {}),
        "work_unit_selection": dict(
            execution_boundary.get("work_unit_selection") or {}
        ),
        "data_plane_policy": dict(execution_boundary.get("data_plane_policy") or {}),
        "parent_serial_stage_timings": parent_serial_stage_timings,
        "parent_serial_wall_seconds": round(parent_serial_seconds, 6),
        "parent_serial_to_worker_wall_ratio": (
            round(parent_serial_seconds / worker_seconds, 6)
            if worker_seconds > 0.0
            else None
        ),
        "parent_serial_bottleneck_reasons": bottleneck_reasons,
        "work_unit_wall_seconds_distribution": _distribution(work_unit_wall_seconds),
        "tail_skew_ratio": tail_skew_ratio,
        "parallel_worker_execution_wall_seconds": (
            parallel_worker_timing.get("wall_seconds")
            if parallel_worker_timing is not None
            else None
        ),
        **execution_boundary,
    }


def _observed_worker_pid_set(
    work_unit_observability: list[dict[str, Any]],
) -> list[int]:
    pids: set[int] = set()
    for item in work_unit_observability:
        if not isinstance(item, dict):
            continue
        evidence = item.get("worker_process_evidence")
        worker_pid = None
        if isinstance(evidence, dict):
            worker_pid = evidence.get("worker_pid")
        if worker_pid is None:
            worker_pid = item.get("worker_pid")
        if isinstance(worker_pid, bool) or worker_pid in (None, ""):
            continue
        try:
            pids.add(int(worker_pid))
        except (TypeError, ValueError):
            continue
    return sorted(pids)


def _last_stage_timing(
    stage_timings: list[dict[str, Any]], stage: str
) -> dict[str, Any] | None:
    for item in reversed(stage_timings):
        if not isinstance(item, dict):
            continue
        item_stage = str(item.get("stage") or "")
        if item_stage == stage or item_stage.endswith(f".{stage}"):
            return item
    return None


def _work_unit_wall_seconds(
    work_unit_observability: list[dict[str, Any]],
) -> list[float]:
    values: list[float] = []
    for item in work_unit_observability:
        if not isinstance(item, dict):
            continue
        value = item.get("wall_seconds")
        if value is None and isinstance(item.get("resource_guard"), dict):
            value = item["resource_guard"].get("elapsed_s")
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return values


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
    }


def _tail_skew_ratio(values: list[float]) -> float | None:
    if not values:
        return None
    fastest = min(value for value in values if value >= 0.0)
    if fastest <= 0.0:
        return None
    return max(values) / fastest


def compute_run_dataset_fingerprint_stage(
    *,
    snapshots: dict[str, DatasetSnapshot],
    candidate_count: int,
    scenario_count: int,
    progress_callback: ProgressCallback | None,
) -> tuple[str, dict[str, Any]]:
    started = time.perf_counter()
    split_count = len(snapshots)
    _emit_progress(
        progress_callback,
        stage="pre_parallel_run_dataset_fingerprint_start",
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        split_count=split_count,
    )
    dataset_hash = combined_dataset_fingerprint(tuple(snapshots.values()))
    timing = _stage_timing(
        "pre_parallel_run_dataset_fingerprint",
        started,
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        split_count=split_count,
    )
    _emit_progress(
        progress_callback,
        stage="pre_parallel_run_dataset_fingerprint_complete",
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        split_count=split_count,
        elapsed_s=round(float(timing["wall_seconds"]), 3),
    )
    return dataset_hash, timing


def precompute_dataset_hashes_stage(
    *,
    snapshots: dict[str, DatasetSnapshot],
    candidate_count: int,
    scenario_count: int,
    work_task_count: int,
    progress_callback: ProgressCallback | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    started = time.perf_counter()
    split_count = len(snapshots)
    _emit_progress(
        progress_callback,
        stage="pre_parallel_hash_materialization_start",
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        work_task_count=work_task_count,
        split_count=split_count,
    )
    dataset_hashes = precompute_dataset_hashes(snapshots)
    timing = _stage_timing(
        "pre_parallel_hash_materialization",
        started,
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        work_task_count=work_task_count,
        split_count=split_count,
        dataset_hash_call_count=split_count,
    )
    _emit_progress(
        progress_callback,
        stage="pre_parallel_hash_materialization_complete",
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        work_task_count=work_task_count,
        split_count=split_count,
        elapsed_s=round(float(timing["wall_seconds"]), 3),
    )
    return dataset_hashes, timing


def build_research_work_tasks_stage(
    *,
    manifest: ExperimentManifest,
    snapshots: dict[str, DatasetSnapshot],
    raw_candidates: list[dict[str, Any]],
    execution_scenarios: list[tuple[int, ExecutionScenario]],
    dataset_hashes: dict[str, str],
    manifest_hash: str,
    simulation_seed_scope_hash: str,
    include_walk_forward: bool,
    progress_callback: ProgressCallback | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    started = time.perf_counter()
    expected_work_task_count = parallel_work_task_count(
        candidate_count=len(raw_candidates),
        scenario_count=len(execution_scenarios),
        split_count=len(snapshots),
        work_unit=manifest.research_run.execution.work_unit,
    )
    _emit_progress(
        progress_callback,
        stage="build_work_tasks_start",
        candidate_count=len(raw_candidates),
        scenario_count=len(execution_scenarios),
        work_task_count=expected_work_task_count,
        split_count=len(snapshots),
    )
    work_tasks: list[dict[str, Any]] = []
    split_work_unit_names = _work_unit_split_names(
        manifest=manifest,
        snapshots=snapshots,
        include_walk_forward=include_walk_forward,
    )
    for scenario_index, scenario in execution_scenarios:
        scenario_id = _scenario_id(scenario, scenario_index)
        for index, params in enumerate(raw_candidates):
            for split_name in split_work_unit_names:
                work_unit = build_research_work_unit(
                    manifest=manifest,
                    dataset_hashes=dataset_hashes,
                    params=params,
                    candidate_index=index,
                    scenario=scenario,
                    scenario_index=scenario_index,
                    scenario_id=scenario_id,
                    manifest_hash=manifest_hash,
                    simulation_seed_scope_hash=simulation_seed_scope_hash,
                    split_name=split_name,
                )
                work_tasks.append(
                    {
                        "params": params,
                        "candidate_index": index,
                        "scenario": scenario,
                        "scenario_index": scenario_index,
                        "scenario_id": scenario_id,
                        "work_unit": work_unit,
                    }
                )
    timing = _stage_timing(
        "build_work_tasks",
        started,
        candidate_count=len(raw_candidates),
        scenario_count=len(execution_scenarios),
        work_task_count=len(work_tasks),
        split_count=len(snapshots),
        task_count=len(work_tasks),
    )
    _emit_progress(
        progress_callback,
        stage="build_work_tasks_complete",
        candidate_count=len(raw_candidates),
        scenario_count=len(execution_scenarios),
        work_task_count=len(work_tasks),
        split_count=len(snapshots),
        elapsed_s=round(float(timing["wall_seconds"]), 3),
    )
    return work_tasks, timing, list(split_work_unit_names)


def append_candidate_start_events_stage(
    *,
    manager: ResearchPathManager,
    manifest: ExperimentManifest,
    work_tasks: list[dict[str, Any]],
    candidate_count: int,
    scenario_count: int,
    split_count: int,
    artifact_context: ResearchArtifactContext | None,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    started = time.perf_counter()
    bytes_before = int(getattr(artifact_context, "total_bytes", 0) or 0)
    _emit_progress(
        progress_callback,
        stage="candidate_start_journal_append_start",
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        work_task_count=len(work_tasks),
        split_count=split_count,
    )
    for task in work_tasks:
        _append_candidate_event(
            manager=manager,
            manifest=manifest,
            artifact_context=artifact_context,
            event={
                "stage": "candidate_start",
                "candidate_id": candidate_id(
                    dict(task["params"]), int(task["candidate_index"])
                ),
                "scenario_id": task["scenario_id"],
                "scenario_index": task["scenario_index"],
                "parameter_values": task["params"],
                "work_unit_hash": task["work_unit"].work_unit_hash,
            },
        )
    bytes_after = int(getattr(artifact_context, "total_bytes", 0) or bytes_before)
    timing = _stage_timing(
        "append_candidate_start_events",
        started,
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        work_task_count=len(work_tasks),
        split_count=split_count,
        event_count=len(work_tasks),
        bytes_written=max(0, bytes_after - bytes_before),
    )
    _emit_progress(
        progress_callback,
        stage="candidate_start_journal_append_complete",
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        work_task_count=len(work_tasks),
        split_count=split_count,
        event_count=len(work_tasks),
        bytes_written=timing["bytes_written"],
        elapsed_s=round(float(timing["wall_seconds"]), 3),
    )
    return timing


def collect_parent_serial_stage_summary(
    stage_timings: list[dict[str, Any]],
) -> dict[str, Any]:
    timings = [
        dict(item)
        for item in stage_timings
        if str(item.get("stage") or "") in PARENT_SERIAL_TIMING_STAGES
    ]
    wall_seconds = sum(float(item.get("wall_seconds") or 0.0) for item in timings)
    return {
        "parent_serial_stage_timings": timings,
        "parent_serial_wall_seconds": round(wall_seconds, 6),
    }


def run_research_backtest(
    *,
    manifest: ExperimentManifest,
    db_path: str | Path | None,
    manager: ResearchPathManager,
    generated_at: str | None = None,
    execution_calibration: dict[str, Any] | None = None,
    manifest_path: str | None = None,
    command_args: dict[str, Any] | None = None,
    progress_callback: ProgressCallback | None = None,
    candidate_evaluator: CandidateScenarioEvaluator | None = None,
    strategy_registry: StrategyRegistry,
    governance_authority_manager: ResearchPathManager | None = None,
) -> dict[str, Any]:
    if not strategy_registry.accepts_execution_hash(
        manifest.strategy_name, str(manifest.validated_strategy_registry_hash or "")
    ):
        raise ResearchValidationError(
            "manifest_runtime_strategy_registry_hash_mismatch"
        )
    strategy_registry.resolve(manifest.strategy_name)
    _enforce_fast_tier_research_runner_policy(
        candidate_evaluator=candidate_evaluator,
        entrypoint="run_research_backtest",
    )
    started = time.perf_counter()
    stage_timings: list[dict[str, Any]] = []
    work_unit_observability: list[dict[str, Any]] = []
    manifest_hash = manifest.manifest_hash()
    _emit_progress(
        progress_callback,
        stage="start",
        manifest_hash=manifest_hash,
        db_path=str(db_path),
        research_classification=manifest.research_classification,
    )
    _validate_parallel_research_run_policy(manifest)
    _validate_strategy_data_requirements(manifest, strategy_registry)
    artifact_context = ResearchArtifactContext(
        manager=manager,
        experiment_id=manifest.experiment_id,
        budget=_artifact_budget_from_limits(manifest.research_run.resource_limits),
    )
    run_context = DatasetRunContext()
    snapshots = {}
    for split_name in ("train", "validation"):
        stage_started = time.perf_counter()
        snapshot = load_dataset_split(
            db_path=db_path,
            manifest=manifest,
            split_name=split_name,
            run_context=run_context,
        )
        snapshots[split_name] = snapshot
        stage_timings.append(
            _stage_timing(
                "load_split",
                stage_started,
                split=split_name,
                candles=len(snapshot.candles),
            )
        )
        _emit_progress(
            progress_callback,
            stage="load_split",
            split=split_name,
            candles=len(snapshot.candles),
        )
    stage_started = time.perf_counter()
    quality_reports = _quality_reports(db_path=db_path, snapshots=snapshots)
    _validate_dataset_adapter_provenance(
        manifest=manifest, quality_reports=quality_reports
    )
    stage_timings.append(
        _stage_timing("quality_report", stage_started, split="train,validation")
    )
    for split_name, report in sorted(quality_reports.items()):
        _emit_progress(
            progress_callback,
            stage="quality_report",
            split=split_name,
            status=report.quality_gate_status,
            reasons=",".join(report.quality_gate_reasons)
            if report.quality_gate_reasons
            else "none",
        )
    _validate_run_purpose_dataset_scope(manifest=manifest, snapshots=snapshots)
    experiment_registry_reservation = None
    _require_enough_candles(snapshots.values())
    manifest = _canonicalize_runner_default_execution(manifest)

    execution_plan = build_research_execution_plan(
        manifest=manifest,
        snapshots=snapshots,
        quality_reports=quality_reports,
        db_path=db_path,
        repository_version=_repository_version(),
        created_at=generated_at,
        include_walk_forward=False,
        strategy_registry=strategy_registry,
    )
    manifest = _apply_execution_plan_resource_policy(
        manifest=manifest, execution_plan=execution_plan
    )
    manifest, memory_admission = _apply_memory_admission_policy(
        manifest=manifest,
        execution_plan=execution_plan,
    )
    execution_plan = _execution_plan_with_memory_admitted_data_plane_policy(
        execution_plan=execution_plan,
        memory_admission=memory_admission,
    )
    if memory_admission.get("action") not in {
        "cap_workers",
        "batch_candidates",
    } and int(
        memory_admission.get("effective_max_workers")
        or manifest.research_run.execution.max_workers
    ) != int(execution_plan.payload["max_workers"]):
        execution_plan = build_research_execution_plan(
            manifest=manifest,
            snapshots=snapshots,
            quality_reports=quality_reports,
            db_path=db_path,
            repository_version=_repository_version(),
            created_at=generated_at,
            include_walk_forward=False,
            strategy_registry=strategy_registry,
        )
        manifest = _apply_execution_plan_resource_policy(
            manifest=manifest, execution_plan=execution_plan
        )
    _emit_progress(
        progress_callback,
        stage="execution_plan",
        execution_mode=execution_plan.payload["execution_mode"],
        max_workers=execution_plan.payload["max_workers"],
        work_unit_type=execution_plan.payload["work_unit_type"],
        estimated_strategy_runs=execution_plan.payload["estimated_strategy_runs"],
    )
    stage_started = time.perf_counter()
    evaluation = _evaluate_candidates(
        manifest=manifest,
        manager=manager,
        db_path=db_path,
        snapshots=snapshots,
        quality_reports=quality_reports,
        include_walk_forward=False,
        execution_calibration=execution_calibration,
        execution_plan=execution_plan,
        work_unit_observability=work_unit_observability,
        progress_callback=progress_callback,
        candidate_evaluator=candidate_evaluator,
        artifact_context=artifact_context,
        strategy_registry=strategy_registry,
    )
    candidates = evaluation.candidates
    stage_timings.append(
        _stage_timing(
            "candidate_evaluation", stage_started, candidate_count=len(candidates)
        )
    )
    stage_timings.extend(
        _prefixed_stage_timings("candidate_evaluation", evaluation.substage_timings)
    )
    execution_observability = _execution_observability_payload(
        manifest=manifest,
        stage_timings=stage_timings,
        work_unit_observability=work_unit_observability,
        execution_boundary=evaluation.execution_boundary,
        snapshots=snapshots,
    )
    execution_observability["memory_admission"] = dict(memory_admission)
    execution_observability["candidate_artifact_write"] = dict(
        evaluation.candidate_artifact_observability
    )
    execution_observability["candidate_profile_hash_observability"] = dict(
        evaluation.candidate_profile_hash_observability
    )
    report = _report_payload(
        manifest=manifest,
        snapshots=tuple(snapshots.values()),
        quality_reports=tuple(quality_reports.values()),
        candidates=candidates,
        report_kind="backtest",
        generated_at=generated_at,
        manifest_path=manifest_path,
        command_name="research-backtest",
        command_args=command_args,
        execution_calibration=execution_calibration,
        manager=manager,
        experiment_registry_reservation=experiment_registry_reservation,
        execution_plan=execution_plan,
        execution_observability=execution_observability,
        strategy_registry=strategy_registry,
        artifact_context=artifact_context,
        governance_authority_manager=governance_authority_manager,
    )
    report.setdefault("artifact_observability", {})["candidate_results"] = dict(
        evaluation.candidate_artifact_observability
    )
    _emit_progress(
        progress_callback,
        stage="report_write",
        experiment_id=manifest.experiment_id,
        candidate_count=len(candidates),
    )
    stage_started = time.perf_counter()
    write_result = write_research_report(
        manager=manager,
        experiment_id=manifest.experiment_id,
        report_name="backtest",
        payload=report,
        artifact_context=artifact_context,
    )
    paths = write_result.paths
    stage_timings.extend(
        _prefixed_stage_timings("report_write", write_result.substage_timings or [])
    )
    stage_timings.append(
        _stage_timing(
            "report_write",
            stage_started,
            candidate_count=len(candidates),
            artifact_total_bytes=write_result.artifact_write_summary[
                "artifact_total_bytes"
            ],
            artifact_file_count=write_result.artifact_write_summary[
                "artifact_file_count"
            ],
            derived_candidates_bytes=write_result.artifact_write_summary[
                "derived_candidates_bytes"
            ],
            report_bytes=write_result.artifact_write_summary["report_bytes"],
        )
    )
    full_candidates = report.get("candidates")
    report.clear()
    report.update(write_result.report_payload or {})
    if manifest.research_run.report_detail in {"index", "summary", "standard"}:
        report["candidates"] = full_candidates
    _attach_authoritative_reproduction_receipt(
        report=report,
        full_candidates=full_candidates or [],
        manifest=manifest,
        report_path=paths.report_path,
    )
    _emit_progress(
        progress_callback,
        stage="complete",
        experiment_id=manifest.experiment_id,
        candidate_count=len(candidates),
        elapsed_s=round(time.perf_counter() - started, 3),
    )
    return report


def run_research_walk_forward(
    *,
    manifest: ExperimentManifest,
    db_path: str | Path | None,
    manager: ResearchPathManager,
    generated_at: str | None = None,
    execution_calibration: dict[str, Any] | None = None,
    manifest_path: str | None = None,
    command_args: dict[str, Any] | None = None,
    progress_callback: ProgressCallback | None = None,
    candidate_evaluator: CandidateScenarioEvaluator | None = None,
    strategy_registry: StrategyRegistry,
    governance_authority_manager: ResearchPathManager | None = None,
) -> dict[str, Any]:
    if not strategy_registry.accepts_execution_hash(
        manifest.strategy_name, str(manifest.validated_strategy_registry_hash or "")
    ):
        raise ResearchValidationError(
            "manifest_runtime_strategy_registry_hash_mismatch"
        )
    strategy_registry.resolve(manifest.strategy_name)
    _enforce_fast_tier_research_runner_policy(
        candidate_evaluator=candidate_evaluator,
        entrypoint="run_research_walk_forward",
    )
    started = time.perf_counter()
    stage_timings: list[dict[str, Any]] = []
    work_unit_observability: list[dict[str, Any]] = []
    _emit_progress(
        progress_callback,
        stage="start",
        manifest_hash=manifest.manifest_hash(),
        db_path=str(db_path),
        research_classification=manifest.research_classification,
    )
    if manifest.walk_forward is None:
        raise ResearchValidationError("walk_forward_missing")
    _validate_parallel_research_run_policy(manifest)
    _validate_strategy_data_requirements(manifest, strategy_registry)
    artifact_context = ResearchArtifactContext(
        manager=manager,
        experiment_id=manifest.experiment_id,
        budget=_artifact_budget_from_limits(manifest.research_run.resource_limits),
    )
    windows = rolling_walk_forward_windows(manifest)
    if len(windows) < manifest.walk_forward.min_windows:
        raise ResearchValidationError(
            f"walk_forward_insufficient_windows: available={len(windows)} min_windows={manifest.walk_forward.min_windows}"
        )
    stage_started = time.perf_counter()
    run_context = DatasetRunContext()
    snapshots = _load_walk_forward_snapshots(
        db_path=db_path, manifest=manifest, windows=windows, run_context=run_context
    )
    stage_timings.append(
        _stage_timing(
            "load_split",
            stage_started,
            split="walk_forward",
            candles=sum(len(snapshot.candles) for snapshot in snapshots.values()),
        )
    )
    for split_name, snapshot in sorted(snapshots.items()):
        _emit_progress(
            progress_callback,
            stage="load_split",
            split=split_name,
            candles=len(snapshot.candles),
        )
    stage_started = time.perf_counter()
    quality_reports = _quality_reports(db_path=db_path, snapshots=snapshots)
    _validate_dataset_adapter_provenance(
        manifest=manifest, quality_reports=quality_reports
    )
    stage_timings.append(
        _stage_timing("quality_report", stage_started, split="walk_forward")
    )
    for split_name, report in sorted(quality_reports.items()):
        _emit_progress(
            progress_callback,
            stage="quality_report",
            split=split_name,
            status=report.quality_gate_status,
            reasons=",".join(report.quality_gate_reasons)
            if report.quality_gate_reasons
            else "none",
        )
    experiment_registry_reservation = None
    _require_enough_candles(snapshots.values())
    manifest = _canonicalize_runner_default_execution(manifest)
    execution_plan = build_research_execution_plan(
        manifest=manifest,
        snapshots=snapshots,
        quality_reports=quality_reports,
        db_path=db_path,
        repository_version=_repository_version(),
        created_at=generated_at,
        include_walk_forward=True,
        strategy_registry=strategy_registry,
    )
    manifest = _apply_execution_plan_resource_policy(
        manifest=manifest, execution_plan=execution_plan
    )
    manifest, memory_admission = _apply_memory_admission_policy(
        manifest=manifest,
        execution_plan=execution_plan,
    )
    execution_plan = _execution_plan_with_memory_admitted_data_plane_policy(
        execution_plan=execution_plan,
        memory_admission=memory_admission,
    )
    if memory_admission.get("action") not in {
        "cap_workers",
        "batch_candidates",
    } and int(
        memory_admission.get("effective_max_workers")
        or manifest.research_run.execution.max_workers
    ) != int(execution_plan.payload["max_workers"]):
        execution_plan = build_research_execution_plan(
            manifest=manifest,
            snapshots=snapshots,
            quality_reports=quality_reports,
            db_path=db_path,
            repository_version=_repository_version(),
            created_at=generated_at,
            include_walk_forward=True,
            strategy_registry=strategy_registry,
        )
        manifest = _apply_execution_plan_resource_policy(
            manifest=manifest, execution_plan=execution_plan
        )
    _emit_progress(
        progress_callback,
        stage="execution_plan",
        execution_mode=execution_plan.payload["execution_mode"],
        max_workers=execution_plan.payload["max_workers"],
        work_unit_type=execution_plan.payload["work_unit_type"],
        estimated_strategy_runs=execution_plan.payload["estimated_strategy_runs"],
    )
    stage_started = time.perf_counter()
    evaluation = _evaluate_candidates(
        manifest=manifest,
        manager=manager,
        db_path=db_path,
        snapshots=snapshots,
        quality_reports=quality_reports,
        include_walk_forward=True,
        execution_calibration=execution_calibration,
        execution_plan=execution_plan,
        work_unit_observability=work_unit_observability,
        progress_callback=progress_callback,
        candidate_evaluator=candidate_evaluator,
        artifact_context=artifact_context,
        strategy_registry=strategy_registry,
    )
    candidates = evaluation.candidates
    stage_timings.append(
        _stage_timing(
            "candidate_evaluation", stage_started, candidate_count=len(candidates)
        )
    )
    stage_timings.extend(
        _prefixed_stage_timings("candidate_evaluation", evaluation.substage_timings)
    )
    execution_observability = _execution_observability_payload(
        manifest=manifest,
        stage_timings=stage_timings,
        work_unit_observability=work_unit_observability,
        execution_boundary=evaluation.execution_boundary,
        snapshots=snapshots,
    )
    execution_observability["memory_admission"] = dict(memory_admission)
    execution_observability["candidate_artifact_write"] = dict(
        evaluation.candidate_artifact_observability
    )
    execution_observability["candidate_profile_hash_observability"] = dict(
        evaluation.candidate_profile_hash_observability
    )
    report = _report_payload(
        manifest=manifest,
        snapshots=tuple(snapshots.values()),
        quality_reports=tuple(quality_reports.values()),
        candidates=candidates,
        report_kind="walk_forward",
        generated_at=generated_at,
        manifest_path=manifest_path,
        command_name="research-walk-forward",
        command_args=command_args,
        execution_calibration=execution_calibration,
        manager=manager,
        experiment_registry_reservation=experiment_registry_reservation,
        execution_plan=execution_plan,
        execution_observability=execution_observability,
        strategy_registry=strategy_registry,
        artifact_context=artifact_context,
        governance_authority_manager=governance_authority_manager,
    )
    report.setdefault("artifact_observability", {})["candidate_results"] = dict(
        evaluation.candidate_artifact_observability
    )
    _emit_progress(
        progress_callback,
        stage="report_write",
        experiment_id=manifest.experiment_id,
        candidate_count=len(candidates),
    )
    stage_started = time.perf_counter()
    write_result = write_research_report(
        manager=manager,
        experiment_id=manifest.experiment_id,
        report_name="walk_forward",
        payload=report,
        artifact_context=artifact_context,
    )
    paths = write_result.paths
    stage_timings.extend(
        _prefixed_stage_timings("report_write", write_result.substage_timings or [])
    )
    stage_timings.append(
        _stage_timing(
            "report_write",
            stage_started,
            candidate_count=len(candidates),
            artifact_total_bytes=write_result.artifact_write_summary[
                "artifact_total_bytes"
            ],
            artifact_file_count=write_result.artifact_write_summary[
                "artifact_file_count"
            ],
            derived_candidates_bytes=write_result.artifact_write_summary[
                "derived_candidates_bytes"
            ],
            report_bytes=write_result.artifact_write_summary["report_bytes"],
        )
    )
    full_candidates = report.get("candidates")
    report.clear()
    report.update(write_result.report_payload or {})
    if manifest.research_run.report_detail in {"index", "summary", "standard"}:
        report["candidates"] = full_candidates
    _attach_authoritative_reproduction_receipt(
        report=report,
        full_candidates=full_candidates or [],
        manifest=manifest,
        report_path=paths.report_path,
    )
    _emit_progress(
        progress_callback,
        stage="complete",
        experiment_id=manifest.experiment_id,
        candidate_count=len(candidates),
        elapsed_s=round(time.perf_counter() - started, 3),
    )
    return report


def run_final_holdout_confirmation(
    *,
    manifest: ExperimentManifest,
    selection_report: dict[str, Any],
    db_path: str | Path | None,
    manager: ResearchPathManager,
    generated_at: str | None = None,
    progress_callback: ProgressCallback | None = None,
    strategy_registry: StrategyRegistry,
) -> dict[str, Any]:
    """Evaluate exactly the candidate frozen by a verified pre-holdout selection."""
    if manifest.dataset.split.final_holdout is None:
        raise ResearchValidationError("final_holdout_missing")
    artifact = selection_report.get("selection_artifact")
    if not isinstance(artifact, dict):
        raise ResearchValidationError("selection_artifact_missing")
    artifact_reasons = validate_selection_artifact(artifact)
    if artifact_reasons:
        raise ResearchValidationError(
            "selection_artifact_invalid:" + ",".join(artifact_reasons)
        )
    if artifact.get("manifest_hash") != manifest.manifest_hash():
        raise ResearchValidationError("selection_artifact_manifest_hash_mismatch")
    selected_id = str(artifact["selected_candidate_id"])
    candidates = [
        item
        for item in selection_report.get("candidates") or []
        if isinstance(item, dict)
    ]
    selected = next(
        (
            item
            for item in candidates
            if str(item.get("parameter_candidate_id") or "") == selected_id
        ),
        None,
    )
    if selected is None:
        raise ResearchValidationError("selection_artifact_candidate_missing")
    binding_reasons = validate_selection_artifact_binding(
        report=selection_report,
        selection_artifact=artifact,
        selected_candidate=selected,
    )
    if binding_reasons:
        raise ResearchValidationError(
            "selection_artifact_report_binding_invalid:" + ",".join(binding_reasons)
        )
    parameter_values = dict(
        selected.get("parameter_values_raw") or selected.get("parameter_values") or {}
    )
    if sha256_prefixed(parameter_values) != artifact.get("parameter_values_hash"):
        raise ResearchValidationError("selection_artifact_parameter_hash_mismatch")
    if selected.get("effective_strategy_parameters_hash") != artifact.get(
        "effective_strategy_parameters_hash"
    ):
        raise ResearchValidationError(
            "selection_artifact_effective_parameter_hash_mismatch"
        )
    compiled_payload = selected.get("compiled_strategy_contract")
    if not isinstance(compiled_payload, dict):
        raise ResearchValidationError("selection_artifact_compiled_contract_missing")
    if selected.get("compiled_strategy_contract_hash") != artifact.get(
        "compiled_strategy_contract_hash"
    ):
        raise ResearchValidationError(
            "selection_artifact_compiled_contract_hash_mismatch"
        )
    plugin = strategy_registry.resolve(manifest.strategy_name)
    compiled_contract = validate_compiled_strategy_contract(
        compiled_payload,
        expected_strategy_name=manifest.strategy_name,
        expected_registry_hash=strategy_registry.execution_scope_hash(
            manifest.strategy_name
        ),
        expected_plugin_hash=plugin.contract_hash(),
        expected_compiled_hash=str(artifact["compiled_strategy_contract_hash"]),
    )

    authorization = _reserve_final_holdout_authorization(
        manifest=manifest,
        selection_report=selection_report,
        selection_artifact=artifact,
        manager=manager,
        generated_at=generated_at,
    )
    run_context = DatasetRunContext()
    snapshot = load_dataset_split(
        db_path=db_path,
        manifest=manifest,
        split_name="final_holdout",
        run_context=run_context,
    )
    _require_enough_candles((snapshot,))
    quality = _quality_reports(db_path=db_path, snapshots={"final_holdout": snapshot})[
        "final_holdout"
    ]
    _validate_dataset_adapter_provenance(
        manifest=manifest, quality_reports={"final_holdout": quality}
    )
    scenario = _base_report_scenario(manifest)
    scenario_index = next(
        (
            index
            for index, item in enumerate(manifest.execution_model.scenarios)
            if item is scenario
        ),
        0,
    )
    scenario_id = _scenario_id(scenario, scenario_index)
    context = _backtest_context(
        manifest=manifest,
        manager=manager,
        candidate_id=selected_id,
        scenario_id=scenario_id,
        scenario_index=scenario_index,
        split_name="final_holdout",
        dataset_content_hash=snapshot.snapshot_fingerprint_hash(),
        parameter_values=parameter_values,
        progress_callback=progress_callback,
        artifact_context=ResearchArtifactContext(
            manager=manager,
            experiment_id=manifest.experiment_id,
            budget=_artifact_budget_from_limits(manifest.research_run.resource_limits),
        ),
    )
    execution_model = _execution_model_from_scenario(
        scenario,
        seed_context=_seed_context(
            simulation_seed_scope_hash=manifest.simulation_seed_scope_hash(),
            scenario=scenario,
            scenario_id=scenario_id,
            parameter_candidate_id=selected_id,
            split_name="final_holdout",
        ),
    )
    run = run_common_simulation_backtest(
        plugin=plugin,
        dataset=snapshot,
        parameter_values=parameter_values,
        fee_rate=scenario.fee_rate,
        slippage_bps=float(scenario.slippage_bps),
        parameter_stability_score=None,
        execution_model=execution_model,
        execution_timing_policy=manifest.execution_timing,
        portfolio_policy=manifest.portfolio_policy,
        risk_policy=manifest.risk_policy,
        context=context,
        compiled_contract=compiled_contract,
    )
    validate_execution_evidence(
        run=run,
        timing=manifest.execution_timing,
        model=execution_model,
        validation_bound=requires_candidate_validation(
            manifest.research_classification
        ),
    )
    metrics = run.metrics.as_dict()
    metrics_v2 = _metrics_v2_payload(run)
    gate_result, gate_reasons = _final_holdout_gate_result(
        manifest=manifest,
        metrics=metrics,
        metrics_v2=metrics_v2,
        quality=quality,
    )
    artifact_evidence = {
        "artifact_id": snapshot.artifact_id,
        "artifact_manifest_hash": snapshot.artifact_manifest_hash,
        "artifact_content_hash": snapshot.artifact_content_hash,
        "artifact_schema_hash": snapshot.artifact_schema_hash,
        "verification_status": snapshot.verification.overall_status.value
        if snapshot.verification
        else "UNAVAILABLE",
    }
    holdout_evidence = {
        "requested_range": snapshot.date_range.as_dict(),
        "snapshot_query_hash": snapshot.snapshot_query_hash(),
        "snapshot_data_hash": snapshot.snapshot_data_hash(),
        "snapshot_fingerprint_hash": snapshot.snapshot_fingerprint_hash(),
        "quality_hash": quality.content_hash,
    }
    holdout_hashes = final_holdout_hashes_from_manifest(
        manifest=manifest,
        final_holdout_split_hash=snapshot.snapshot_fingerprint_hash(),
        dataset_quality_hash=quality.content_hash,
        dataset_artifact=artifact_evidence,
        final_holdout_evidence=holdout_evidence,
    )
    candidate_results = [
        {
            "candidate_id": selected_id,
            "compiled_strategy_contract_hash": compiled_contract.compiled_contract_hash,
            "metrics": metrics,
            "metrics_v2": metrics_v2,
            "execution_event_summary": run.execution_event_summary
            or execution_event_summary(run.trades),
            "reproduction_hashes": _reproduction_result_hashes(run),
        }
    ]
    result_binding = {
        "selection_artifact_hash": artifact["content_hash"],
        "selected_candidate_id": selected_id,
        "candidate_results": candidate_results,
        "confirmation_gate_result": gate_result,
        "confirmation_gate_fail_reasons": gate_reasons,
    }
    final_holdout_result_hash = compute_final_holdout_result_hash(result_binding)
    completion = append_attempt_completion(
        manager=manager,
        reservation=authorization,
        updates={
            **holdout_hashes,
            "selection_artifact_hash": artifact["content_hash"],
            "selected_candidate_id": selected_id,
            "candidate_count": 1,
            "confirmation_gate_result": gate_result,
            "final_holdout_result_hash_schema_version": (
                FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION
            ),
            "final_holdout_result_hash": final_holdout_result_hash,
        },
        result_status="COMPLETED",
        created_at=generated_at,
    )
    material = {
        "schema_version": FINAL_HOLDOUT_CONFIRMATION_SCHEMA_VERSION,
        "artifact_type": "final_holdout_confirmation",
        "manifest_hash": manifest.manifest_hash(),
        "selection_artifact_hash": artifact["content_hash"],
        "selected_candidate_id": selected_id,
        "candidate_results": candidate_results,
        "final_holdout_result_hash_schema_version": (
            FINAL_HOLDOUT_RESULT_HASH_SCHEMA_VERSION
        ),
        "final_holdout_result_hash": final_holdout_result_hash,
        "dataset_evidence": {**artifact_evidence, **holdout_evidence},
        **holdout_hashes,
        "experiment_registry_path": authorization["path"],
        "experiment_registry_prior_hash": authorization["prior_hash"],
        "experiment_registry_row_hash": authorization["row_hash"],
        "experiment_registry_completion_row_hash": completion["row_hash"],
        "declared_attempt_index": authorization["row"].get("declared_attempt_index"),
        "declared_holdout_reuse_count": authorization["row"].get(
            "declared_holdout_reuse_count"
        ),
        "selection_attempt_index": authorization["row"].get("selection_attempt_index"),
        "selection_holdout_reuse_count": authorization["row"].get(
            "selection_holdout_reuse_count"
        ),
        "computed_attempt_index": authorization.get("computed_attempt_index"),
        "computed_holdout_reuse_count": completion["row"].get(
            "computed_holdout_reuse_count"
        ),
        "authorization_row_hash": authorization["row_hash"],
        "completion_row_hash": completion["row_hash"],
        "confirmation_gate_result": gate_result,
        "confirmation_gate_fail_reasons": gate_reasons,
    }
    report = {
        **material,
        "content_hash": sha256_prefixed(material, label="final_holdout_confirmation"),
    }
    target = manager.report_path(
        "research", manifest.experiment_id, "final_holdout_confirmation.json"
    )
    write_json_atomic(target, report)
    report["confirmation_artifact_path"] = str(target.resolve())
    return report


def _reserve_final_holdout_authorization(
    *,
    manifest: ExperimentManifest,
    selection_report: dict[str, Any],
    selection_artifact: dict[str, Any],
    manager: ResearchPathManager,
    generated_at: str | None,
) -> dict[str, Any]:
    split_rows = selection_report.get("dataset_splits")
    split_rows = split_rows if isinstance(split_rows, dict) else {}
    source = next((item for item in split_rows.values() if isinstance(item, dict)), {})
    artifact_evidence = {
        "artifact_id": source.get("artifact_id"),
        "artifact_manifest_hash": source.get("artifact_manifest_hash"),
        "artifact_content_hash": source.get("artifact_content_hash"),
        "artifact_schema_hash": source.get("artifact_schema_hash"),
        "verification_status": source.get("verification_status"),
    }
    dataset_artifact_evidence_hash = sha256_prefixed(artifact_evidence)
    holdout_range = (
        manifest.dataset.split.final_holdout.as_dict()
        if manifest.dataset.split.final_holdout
        else None
    )
    pre_exposure_key = pre_exposure_reservation_key_hash_from_parts(
        strategy_name=manifest.strategy_name,
        market=manifest.market,
        interval=manifest.interval,
        final_holdout=holdout_range,
        objective_metric=objective_metric_from_manifest(manifest),
        dataset_artifact_evidence_hash=dataset_artifact_evidence_hash,
    )
    if pre_exposure_key is None:
        raise ResearchValidationError("final_holdout_pre_exposure_identity_incomplete")
    identity = research_identity_from_manifest(manifest)
    declared_attempt_index = _optional_int(manifest.raw.get("attempt_index"))
    declared_holdout_reuse_count = _optional_int(
        manifest.raw.get("holdout_reuse_count")
    )
    selection_attempt_index = _optional_int(selection_report.get("attempt_index"))
    selection_holdout_reuse_count = _optional_int(
        selection_report.get("holdout_reuse_count")
    )
    base_payload = {
        "run_id": manifest.experiment_id,
        "experiment_id": manifest.experiment_id,
        "experiment_family_id": identity["experiment_family_id"],
        "hypothesis_id": identity["hypothesis_id"],
        "hypothesis_version": identity["hypothesis_version"],
        "hypothesis_contract_hash": identity["hypothesis_contract_hash"],
        "hypothesis_semantic_fingerprint": identity["hypothesis_semantic_fingerprint"],
        "hypothesis_status": identity["hypothesis_status"],
        "manifest_hash": manifest.manifest_hash(),
        "research_classification": manifest.research_classification,
        "dataset_snapshot_id": manifest.dataset.snapshot_id,
        "dataset_artifact": artifact_evidence,
        "dataset_artifact_evidence_hash": dataset_artifact_evidence_hash,
        "final_holdout_identity_hash": final_holdout_identity_hash_from_parts(
            dataset_source=manifest.dataset.source,
            market=manifest.market,
            interval=manifest.interval,
            final_holdout=holdout_range,
        ),
        "pre_exposure_reservation_key_hash": pre_exposure_key,
        "pre_exposure_reservation_key_schema_version": 1,
        "final_holdout_content_pending_until_completion": True,
        "objective_metric": objective_metric_from_manifest(manifest),
        "selection_artifact_hash": selection_artifact["content_hash"],
        "selected_candidate_id": selection_artifact["selected_candidate_id"],
        "declared_attempt_index": declared_attempt_index,
        "declared_holdout_reuse_count": declared_holdout_reuse_count,
        "selection_attempt_index": selection_attempt_index,
        "selection_holdout_reuse_count": selection_holdout_reuse_count,
    }
    contract = (
        manifest.statistical_validation.as_dict()
        if manifest.statistical_validation is not None
        else {"gates": {"max_holdout_reuse_count": 0}}
    )
    reservation = reserve_research_attempt_checked(
        manager=manager,
        base_payload=base_payload,
        statistical_validation_contract=contract,
        created_at=generated_at,
    )
    if not reservation.get("accepted"):
        raise ResearchValidationError(
            "final_holdout_pre_exposure_authorization_failed:"
            + ",".join(str(item) for item in reservation.get("reasons") or [])
        )
    return reservation


def _final_holdout_gate_result(
    *,
    manifest: ExperimentManifest,
    metrics: dict[str, Any],
    metrics_v2: dict[str, Any],
    quality: DatasetQualityReport,
) -> tuple[str, list[str]]:
    gate = manifest.acceptance_gate
    reasons = (
        list(quality.quality_gate_reasons)
        if quality.quality_gate_status != "PASS"
        else []
    )
    if int(metrics.get("trade_count") or 0) < gate.min_trade_count:
        reasons.append("final_holdout_min_trade_count_failed")
    if float(metrics.get("max_drawdown_pct") or 0.0) > gate.max_mdd_pct:
        reasons.append("final_holdout_max_drawdown_failed")
    if not _profit_factor_passes(
        metrics.get("profit_factor"),
        metrics.get("profit_factor_unbounded"),
        gate.min_profit_factor,
    ):
        reasons.append("final_holdout_profit_factor_failed")
    if (
        gate.oos_return_must_be_positive
        and float(metrics.get("return_pct") or 0.0) <= 0.0
    ):
        reasons.append("final_holdout_return_not_positive")
    reasons.extend(
        _metrics_v2_gate_reasons(
            gate=gate, metrics_v2=metrics_v2, prefix="final_holdout_"
        )
    )
    return ("PASS" if not reasons else "FAIL", sorted(set(reasons)))


def _attach_authoritative_reproduction_receipt(
    *,
    report: dict[str, Any],
    full_candidates: list[dict[str, Any]],
    manifest: ExperimentManifest,
    report_path: Path,
) -> None:
    """Attach immutable-artifact receipt evidence, or an explicit policy-A result.

    Mutable ``sqlite_candles`` is intentionally research-only DECLARED_ONLY
    evidence.  It can produce a diagnostic report but cannot honestly produce
    an authoritative immutable-artifact receipt.
    """
    split_rows = report.get("dataset_splits")
    non_verified = isinstance(split_rows, dict) and any(
        not isinstance(row, dict)
        or row.get("verification_status") != VerificationStatus.VERIFIED.value
        for row in split_rows.values()
    )
    if non_verified:
        if manifest.research_classification != "research_only":
            raise ResearchValidationError(
                "authoritative_receipt_requires_verified_dataset_artifact"
            )
        report["reproduction_receipt_status"] = "UNAVAILABLE_MUTABLE_SOURCE_POLICY_A"
        report["reproduction_receipt_reason"] = (
            "mutable_dataset_source_not_verified_immutable_artifact"
        )
        report["warnings"] = sorted(
            set(report.get("warnings") or ())
            | {
                "dataset_verification_not_immutable_artifact",
                "authoritative_reproduction_receipt_unavailable_mutable_source",
            }
        )
        return
    receipt_path = report_path.with_name("reproduction_receipt.json")
    receipt_report = dict(report)
    receipt_report["candidates"] = full_candidates
    receipt = create_reproduction_receipt(
        report=receipt_report, manifest=manifest, receipt_path=receipt_path
    )
    report["reproduction_receipt_path"] = str(receipt_path.resolve())
    report["reproduction_receipt_hash"] = receipt["receipt_content_hash"]
    report["reproduction_receipt_status"] = "AVAILABLE"


def _evaluate_candidates(
    *,
    manifest: ExperimentManifest,
    manager: ResearchPathManager,
    snapshots: dict[str, DatasetSnapshot],
    db_path: str | Path | None = None,
    quality_reports: dict[str, DatasetQualityReport],
    include_walk_forward: bool,
    execution_calibration: dict[str, Any] | None,
    execution_plan: ResearchExecutionPlan | None = None,
    work_unit_observability: list[dict[str, Any]] | None = None,
    progress_callback: ProgressCallback | None = None,
    candidate_evaluator: CandidateScenarioEvaluator | None = None,
    artifact_context: ResearchArtifactContext | None = None,
    strategy_registry: StrategyRegistry,
) -> CandidateEvaluationResult:
    substage_timings: list[dict[str, Any]] = []
    candidate_artifact_observability = {
        "candidate_result_file_count": 0,
        "candidate_result_total_bytes": 0,
        "candidate_result_write_wall_seconds": 0.0,
    }
    raw_candidates = iter_parameter_candidates(manifest.parameter_space)
    execution_scenarios = required_execution_scenarios(
        manifest.execution_model.scenarios
    )
    candidate_count = len(raw_candidates)
    scenario_count = len(execution_scenarios)
    split_count = len(snapshots)
    expected_work_task_count = parallel_work_task_count(
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        split_count=split_count,
        work_unit=manifest.research_run.execution.work_unit,
    )
    if execution_plan is not None:
        selection = execution_plan.payload.get("work_unit_selection")
        if isinstance(selection, dict):
            effective_work_unit = str(selection.get("effective_work_unit_type") or "")
            if effective_work_unit and effective_work_unit != str(
                manifest.research_run.execution.work_unit
            ):
                raise ResearchValidationError(
                    "work_unit_selection_disagrees_with_manifest:"
                    f"{effective_work_unit}!={manifest.research_run.execution.work_unit}"
                )
    aggregates: dict[str, dict[str, Any]] = {}
    manifest_hash = manifest.manifest_hash()
    dataset_quality_hash = combined_dataset_quality_hash(
        tuple(quality_reports.values())
    )
    portfolio_policy = manifest.portfolio_policy.as_dict()
    portfolio_policy_hash = manifest.portfolio_policy_hash()
    simulation_policy_hash = manifest.simulation_policy_hash()
    dataset_quality_status, dataset_quality_reasons = _combined_dataset_quality_gate(
        quality_reports
    )
    dataset_warning_codes = _dataset_quality_warning_codes(quality_reports)
    top_of_book_quality_summary = _top_of_book_quality_summary(quality_reports)
    strategy_plugin = strategy_registry.resolve(manifest.strategy_name)
    strategy_spec = strategy_plugin.spec
    metrics_gate_policy = metrics_gate_policy_from_acceptance_gate(
        manifest.acceptance_gate
    )
    metrics_gate_policy_digest = metrics_gate_policy_hash(metrics_gate_policy)
    probe_warnings = _probe_grade_gate_warnings(manifest)
    l2_depth_complete_snapshots_available = bool(
        top_of_book_quality_summary.get("l2_depth_complete_snapshots_available")
    )
    _emit_progress(
        progress_callback,
        stage="workload",
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        split_candle_counts=",".join(
            f"{split_name}:{len(snapshot.candles)}"
            for split_name, snapshot in sorted(snapshots.items())
        ),
        estimated_strategy_runs=_estimated_strategy_runs(
            candidate_count=len(raw_candidates),
            scenario_count=len(execution_scenarios),
            split_count=len(snapshots),
            include_walk_forward=include_walk_forward,
            walk_forward_split_count=sum(
                1 for key in snapshots if key.startswith("window_")
            ),
        ),
        research_classification=manifest.research_classification,
        top_of_book_requested=manifest.dataset.top_of_book is not None,
        top_of_book_required=bool(manifest.dataset.top_of_book.required)
        if manifest.dataset.top_of_book
        else False,
        calibration_required=manifest.execution_model.calibration_required,
    )

    dataset_hash, timing = compute_run_dataset_fingerprint_stage(
        snapshots=snapshots,
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        progress_callback=progress_callback,
    )
    substage_timings.append(timing)
    dataset_hashes, timing = precompute_dataset_hashes_stage(
        snapshots=snapshots,
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        work_task_count=expected_work_task_count,
        progress_callback=progress_callback,
    )
    substage_timings.append(timing)
    simulation_seed_scope_hash = manifest.simulation_seed_scope_hash()
    work_tasks, timing, split_work_unit_names = build_research_work_tasks_stage(
        manifest=manifest,
        snapshots=snapshots,
        raw_candidates=raw_candidates,
        execution_scenarios=execution_scenarios,
        dataset_hashes=dataset_hashes,
        manifest_hash=manifest_hash,
        simulation_seed_scope_hash=simulation_seed_scope_hash,
        include_walk_forward=include_walk_forward,
        progress_callback=progress_callback,
    )
    substage_timings.append(timing)
    registry_descriptor = strategy_registry.descriptor()
    compiled_by_candidate_scenario: dict[tuple[int, int], dict[str, Any]] = {}
    for task in work_tasks:
        key = (int(task["candidate_index"]), int(task["scenario_index"]))
        if key not in compiled_by_candidate_scenario:
            task_scenario = task["scenario"]
            compiled_by_candidate_scenario[key] = (
                StrategyCompiler(strategy_registry)
                .compile(
                    strategy_name=manifest.strategy_name,
                    raw_parameters=dict(task["params"]),
                    fee_rate=task_scenario.fee_rate,
                    slippage_bps=float(task_scenario.slippage_bps),
                    context=BacktestRunContext(
                        policy_materialization_mode="research_validation"
                    ),
                )
                .as_dict()
            )
        task["compiled_contract"] = compiled_by_candidate_scenario[key]
        task["strategy_registry_descriptor"] = registry_descriptor
    if execution_plan is not None:
        selection = execution_plan.payload.get("work_unit_selection")
        if isinstance(selection, dict):
            effective_work_unit = str(
                selection.get("effective_work_unit_type")
                or manifest.research_run.execution.work_unit
            )
            expected_plan_tasks = int(
                selection.get("candidate_scenario_split_task_count")
                if effective_work_unit == "candidate_scenario_split"
                else selection.get("candidate_scenario_task_count")
                or parallel_work_task_count(
                    candidate_count=candidate_count,
                    scenario_count=scenario_count,
                    split_count=len(split_work_unit_names),
                    work_unit=effective_work_unit,
                )
            )
            if (
                effective_work_unit == "candidate_scenario_split"
                and expected_plan_tasks != len(work_tasks)
            ):
                raise ResearchValidationError(
                    "candidate_scenario_split_task_count_mismatch:"
                    f"plan={expected_plan_tasks}:actual={len(work_tasks)}"
                )

    evaluator = candidate_evaluator or ResearchCandidateScenarioEvaluator()
    parallel_executor_used = (
        manifest.research_run.execution.mode == "parallel"
        and candidate_evaluator is None
    )
    process_runtime_observability: list[dict[str, Any]] = []
    execution_boundary = _execution_boundary_observability(
        manifest=manifest,
        candidate_evaluator=candidate_evaluator,
        parallel_executor_used=parallel_executor_used,
    )
    if execution_plan is not None:
        execution_boundary["resource_plan"] = dict(
            execution_plan.payload.get("resource_plan") or {}
        )
        execution_boundary["work_unit_selection"] = dict(
            execution_plan.payload.get("work_unit_selection") or {}
        )
        execution_boundary["data_plane_policy"] = dict(
            execution_plan.payload.get("data_plane_policy") or {}
        )
    execution_boundary["available_parallel_work_tasks"] = len(work_tasks)
    execution_boundary["requested_parallel_task_count"] = (
        len(work_tasks) if parallel_executor_used else 0
    )
    execution_boundary["actual_parallel_task_count"] = (
        len(work_tasks) if parallel_executor_used else 0
    )
    efficiency = parallel_efficiency_payload(
        available_work_tasks=len(work_tasks),
        requested_max_workers=manifest.research_run.execution.max_workers,
        effective_max_workers=manifest.research_run.execution.max_workers,
        work_unit=manifest.research_run.execution.work_unit,
        effective_worker_source="requested_pending_runtime_resolution",
    )
    _emit_progress(
        progress_callback,
        stage="parallel_efficiency",
        **efficiency,
    )
    substage_timings.append(
        {"stage": "parallel_efficiency", "wall_seconds": 0.0, **efficiency}
    )
    if parallel_executor_used:
        substage_timings.append(
            append_candidate_start_events_stage(
                manager=manager,
                manifest=manifest,
                work_tasks=work_tasks,
                candidate_count=candidate_count,
                scenario_count=scenario_count,
                split_count=split_count,
                artifact_context=artifact_context,
                progress_callback=progress_callback,
            )
        )
        worker_context = {
            "manifest": manifest,
            "db_path": str(db_path) if db_path is not None else None,
            "split_names": tuple(snapshots.keys()),
            "dataset_hashes": dict(dataset_hashes),
            "dataset_quality_hash": dataset_quality_hash,
            "manifest_hash": manifest_hash,
            "simulation_seed_scope_hash": simulation_seed_scope_hash,
            "include_walk_forward": include_walk_forward,
            "raw_candidate_count": len(raw_candidates),
            "data_plane_policy": dict(
                (
                    execution_plan.payload.get("data_plane_policy")
                    if execution_plan
                    else {}
                )
                or {}
            ),
            "strategy_registry_descriptor": registry_descriptor,
        }
        worker_started = time.perf_counter()
        _emit_progress(
            progress_callback,
            stage="parallel_worker_pool_start",
            candidate_count=candidate_count,
            scenario_count=scenario_count,
            work_task_count=len(work_tasks),
            split_count=split_count,
            max_workers=manifest.research_run.execution.max_workers,
            process_start_method=manifest.research_run.execution.process_start_method,
            elapsed_s=0.0,
        )
        substage_timings.append(
            {
                "stage": "parallel_worker_pool_start",
                "wall_seconds": 0.0,
                "candidate_count": candidate_count,
                "scenario_count": scenario_count,
                "work_task_count": len(work_tasks),
                "split_count": split_count,
                "max_workers": manifest.research_run.execution.max_workers,
                "process_start_method": manifest.research_run.execution.process_start_method,
            }
        )
        raw_results = []

        def collect_parallel_result(result: ResearchWorkResult) -> None:
            raw_results.append(
                _compact_work_result_with_detail_artifact(
                    manager=manager,
                    manifest=manifest,
                    result=result,
                    artifact_context=artifact_context,
                )
            )

        _execute_parallel_candidate_work_units(
            tasks=work_tasks,
            max_workers=manifest.research_run.execution.max_workers,
            process_start_method=manifest.research_run.execution.process_start_method,
            worker_context=worker_context,
            process_runtime_observability=process_runtime_observability,
            result_callback=collect_parallel_result,
        )
        substage_timings.append(
            _stage_timing(
                "parallel_worker_execution",
                worker_started,
                task_count=len(work_tasks),
                max_workers=manifest.research_run.execution.max_workers,
            )
        )
        result_collection_started = time.perf_counter()
        execution_boundary = _execution_boundary_observability(
            manifest=manifest,
            candidate_evaluator=candidate_evaluator,
            parallel_executor_used=parallel_executor_used,
            process_runtime_observability=(
                process_runtime_observability[-1]
                if process_runtime_observability
                else None
            ),
        )
        if execution_plan is not None:
            execution_boundary["resource_plan"] = dict(
                execution_plan.payload.get("resource_plan") or {}
            )
            execution_boundary["work_unit_selection"] = dict(
                execution_plan.payload.get("work_unit_selection") or {}
            )
            execution_boundary["data_plane_policy"] = dict(
                execution_plan.payload.get("data_plane_policy") or {}
            )
        execution_boundary["available_parallel_work_tasks"] = len(work_tasks)
        execution_boundary["requested_parallel_task_count"] = len(work_tasks)
        execution_boundary["actual_parallel_task_count"] = len(work_tasks)
        substage_timings.append(
            _stage_timing(
                "result_collection",
                result_collection_started,
                result_count=len(raw_results),
            )
        )
    else:
        raw_results = []
        append_start_wall_seconds = 0.0
        append_start_bytes_before = int(
            getattr(artifact_context, "total_bytes", 0) or 0
        )
        worker_wall_seconds = 0.0
        _emit_progress(
            progress_callback,
            stage="candidate_start_journal_append_start",
            candidate_count=candidate_count,
            scenario_count=scenario_count,
            work_task_count=len(work_tasks),
            split_count=split_count,
        )
        for task in work_tasks:
            work_unit = task["work_unit"]
            if not isinstance(work_unit, ResearchWorkUnit):
                raise ResearchValidationError("research_work_unit_missing")
            params = dict(task["params"])
            full_task = _task_from_evaluation_context(
                work_unit=work_unit,
                context=EvaluationContext(
                    manifest=manifest,
                    manager=manager,
                    snapshots=snapshots,
                    manifest_hash=manifest_hash,
                    simulation_seed_scope_hash=simulation_seed_scope_hash,
                    include_walk_forward=include_walk_forward,
                    raw_candidate_count=len(raw_candidates),
                    params=params,
                    candidate_index=int(task["candidate_index"]),
                    scenario=task["scenario"],
                    scenario_index=int(task["scenario_index"]),
                    scenario_id=str(task["scenario_id"]),
                    progress_callback=progress_callback,
                    artifact_context=artifact_context,
                    worker_pid=None,
                    compiled_contract=task.get("compiled_contract"),
                    strategy_registry=strategy_registry,
                ),
            )
            append_started = time.perf_counter()
            _append_candidate_event(
                manager=manager,
                manifest=manifest,
                artifact_context=artifact_context,
                event={
                    "stage": "candidate_start",
                    "candidate_id": candidate_id(
                        params, int(full_task["candidate_index"])
                    ),
                    "scenario_id": full_task["scenario_id"],
                    "scenario_index": full_task["scenario_index"],
                    "parameter_values": params,
                    "work_unit_hash": work_unit.work_unit_hash,
                },
            )
            append_start_wall_seconds += time.perf_counter() - append_started
            worker_started = time.perf_counter()
            serial_result = execute_research_work_units_serial(
                tasks=(
                    EvaluationContext(
                        manifest=manifest,
                        manager=manager,
                        snapshots=snapshots,
                        manifest_hash=manifest_hash,
                        simulation_seed_scope_hash=simulation_seed_scope_hash,
                        include_walk_forward=include_walk_forward,
                        raw_candidate_count=len(raw_candidates),
                        params=params,
                        candidate_index=int(task["candidate_index"]),
                        scenario=task["scenario"],
                        scenario_index=int(task["scenario_index"]),
                        scenario_id=str(task["scenario_id"]),
                        progress_callback=progress_callback,
                        artifact_context=artifact_context,
                        worker_pid=None,
                        compiled_contract=task.get("compiled_contract"),
                        strategy_registry=strategy_registry,
                    ),
                ),
                worker=lambda context: evaluator.evaluate(work_unit, context),
            )[0]
            raw_results.append(
                _compact_work_result_with_detail_artifact(
                    manager=manager,
                    manifest=manifest,
                    result=serial_result,
                    artifact_context=artifact_context,
                )
            )
            worker_wall_seconds += time.perf_counter() - worker_started
        substage_timings.append(
            {
                "stage": "append_candidate_start_events",
                "wall_seconds": round(append_start_wall_seconds, 6),
                "candidate_count": candidate_count,
                "scenario_count": scenario_count,
                "work_task_count": len(work_tasks),
                "split_count": split_count,
                "event_count": len(work_tasks),
                "bytes_written": max(
                    0,
                    int(
                        getattr(artifact_context, "total_bytes", 0)
                        or append_start_bytes_before
                    )
                    - append_start_bytes_before,
                ),
            }
        )
        _emit_progress(
            progress_callback,
            stage="candidate_start_journal_append_complete",
            candidate_count=candidate_count,
            scenario_count=scenario_count,
            work_task_count=len(work_tasks),
            split_count=split_count,
            event_count=len(work_tasks),
            bytes_written=max(
                0,
                int(
                    getattr(artifact_context, "total_bytes", 0)
                    or append_start_bytes_before
                )
                - append_start_bytes_before,
            ),
            elapsed_s=round(append_start_wall_seconds, 3),
        )
        substage_timings.append(
            {
                "stage": "parallel_worker_execution",
                "wall_seconds": round(worker_wall_seconds, 6),
                "task_count": len(work_tasks),
                "max_workers": 1,
                "execution_mode": "serial",
            }
        )
        substage_timings.append(
            {
                "stage": "result_collection",
                "wall_seconds": 0.0,
                "result_count": len(raw_results),
            }
        )
    sort_started = time.perf_counter()
    work_results = sort_work_results_deterministically(raw_results)
    substage_timings.append(
        _stage_timing("sort_work_results", sort_started, result_count=len(work_results))
    )
    normalize_started = time.perf_counter()
    work_results = [
        _normalize_failed_work_result_without_base(manifest=manifest, result=result)
        for result in work_results
    ]
    work_results = _merge_candidate_scenario_split_results(
        manifest=manifest, results=work_results
    )
    substage_timings.append(
        _stage_timing(
            "normalize_work_results", normalize_started, result_count=len(work_results)
        )
    )
    if work_unit_observability is not None:
        extend_started = time.perf_counter()
        work_unit_observability.extend(
            result.observability_payload() for result in work_results
        )
        substage_timings.append(
            _stage_timing(
                "extend_work_unit_observability",
                extend_started,
                result_count=len(work_results),
            )
        )
    for result in work_results:
        if result.status == "failed":
            if result.base_result is not None:
                _write_failed_candidate_evidence(
                    manager=manager,
                    manifest=manifest,
                    candidate=result.base_result,
                    artifact_context=artifact_context,
                )
            event = {
                "stage": "candidate_failure",
                "candidate_id": result.candidate_id,
                "scenario_id": result.scenario_id,
                "reason": result.failure_reason,
                "work_unit_hash": result.work_unit_hash,
            }
            if result.failure_evidence:
                event["resource_guard"] = result.failure_evidence
                if result.failure_reason == "candidate_exception":
                    event["exception_type"] = result.failure_evidence.get(
                        "exception_type"
                    )
                    event["message"] = result.failure_evidence.get("message")
            _append_candidate_event(
                manager=manager,
                manifest=manifest,
                event=event,
                artifact_context=artifact_context,
            )
        elif manifest.research_run.execution.mode == "parallel":
            _emit_progress(
                progress_callback,
                stage="work_unit_complete",
                candidate_id=result.candidate_id,
                scenario_id=result.scenario_id,
                scenario_index=result.scenario_index,
                work_unit_hash=result.work_unit_hash,
                wall_seconds=round(
                    float((result.observability or {}).get("wall_seconds") or 0.0), 3
                ),
                candles_processed=int(
                    (result.observability or {}).get("candles_processed") or 0
                ),
            )
    gate_aggregation_started = time.perf_counter()
    compact_results_by_scenario: dict[int, list[dict[str, Any]]] = {}
    for result in work_results:
        if result.base_result is None:
            raise ResearchValidationError(
                f"work_result_missing_base_result: {result.work_unit_hash}"
            )
        compact_results_by_scenario.setdefault(result.scenario_index, []).append(
            result.base_result
        )

    for scenario_index, scenario in execution_scenarios:
        scenario_id = _scenario_id(scenario, scenario_index)
        expected_calibration_hash = (
            execution_calibration.get("content_hash")
            if isinstance(execution_calibration, dict)
            else None
        )
        expected_execution_contract = _execution_reality_contract(
            manifest=manifest,
            scenario=scenario,
            calibration_hash=expected_calibration_hash,
            top_of_book_available=int(
                top_of_book_quality_summary.get("joined_quote_count") or 0
            )
            > 0,
            depth_available=l2_depth_complete_snapshots_available,
        )
        calibration_gate = compare_calibration_to_scenario(
            calibration=execution_calibration,
            assumed_slippage_bps=scenario.slippage_bps
            + scenario.market_order_extra_cost_bps,
            assumed_latency_ms=scenario.latency_ms,
            assumed_partial_fill_rate=scenario.partial_fill_rate,
            assumed_order_failure_rate=scenario.order_failure_rate,
            expected_market=manifest.market,
            expected_interval=manifest.interval,
            expected_execution_timing_policy=manifest.execution_timing.as_dict(),
            expected_execution_reality_contract=expected_execution_contract,
            expected_calibration_artifact_hash=expected_calibration_hash,
            require_content_hash=manifest.execution_model.calibration_required,
            min_sample_count=ExecutionCalibrationThresholds().min_sample,
            require_quality_gate_pass=(
                manifest.execution_model.calibration_required
                or manifest.execution_model.calibration_strictness == "fail"
            ),
        )
        base_results = sorted(
            compact_results_by_scenario.get(scenario_index, []),
            key=lambda item: int(item["index"]),
        )
        stability = _parameter_stability_scores(
            manifest=manifest,
            candidates=raw_candidates,
            evaluated_candidates=base_results,
        )
        pre_stress_gate_by_index = _pre_stress_gate_summaries(
            manifest=manifest,
            base_results=base_results,
            stability=stability,
            include_walk_forward=include_walk_forward,
            calibration_gate=calibration_gate,
            dataset_quality_status=dataset_quality_status,
            dataset_quality_reasons=dataset_quality_reasons,
        )
        perturbation_candidates = _parameter_perturbation_candidates(
            base_results=base_results,
            pre_stress_gate_by_index=pre_stress_gate_by_index,
        )
        selected_walk_forward = (
            build_walk_forward_selection_evidence(
                candidates=base_results,
                acceptance_gate=manifest.acceptance_gate,
                min_windows=manifest.walk_forward.min_windows,
            )
            if include_walk_forward and manifest.walk_forward is not None
            else None
        )
        for base in base_results:
            index = int(base["index"])
            params = dict(base["parameter_values"])
            executed_usage = (
                base.get("validation_resource_usage")
                or base.get("train_resource_usage")
                or {}
            )
            compiled_payload = dict(
                base.get("compiled_strategy_contract")
                or executed_usage.get("compiled_strategy_contract")
                or {}
            )
            compiled_hash = base.get(
                "compiled_strategy_contract_hash"
            ) or executed_usage.get("compiled_strategy_contract_hash")
            registry_hash = base.get("strategy_registry_hash") or executed_usage.get(
                "strategy_registry_hash"
            )
            if not compiled_payload or not compiled_hash:
                raise ResearchValidationError(
                    "executed_compiled_strategy_contract_missing"
                )
            effective_params = dict(compiled_payload["materialized_parameters"])
            effective_params_hash = str(
                compiled_payload["materialized_parameters_hash"]
            )
            parameter_source_map = dict(compiled_payload["parameter_source_map"])
            active_exit_policy = dict(compiled_payload.get("exit_policy") or {})
            active_exit_policy_hash = exit_policy_hash(active_exit_policy)
            executed_compiled_contract = validate_compiled_strategy_contract(
                compiled_payload,
                expected_strategy_name=manifest.strategy_name,
                expected_registry_hash=strategy_registry.execution_scope_hash(
                    manifest.strategy_name
                ),
                expected_plugin_hash=strategy_plugin.contract_hash(),
                expected_compiled_hash=str(compiled_hash),
            )
            stability_payload = stability[index]
            stability_score = stability_payload["score"]
            train_metrics = dict(base["train_metrics"])
            validation_metrics = dict(base["validation_metrics"])
            train_metrics_v2 = dict(base["train_metrics_v2"])
            validation_metrics_v2 = dict(base["validation_metrics_v2"])
            train_metrics["parameter_stability_score"] = stability_score
            validation_metrics["parameter_stability_score"] = stability_score
            fixed_parameter_walk_forward = base["walk_forward_metrics"]
            walk_forward = (
                selected_walk_forward
                if requires_candidate_validation(manifest.research_classification)
                else fixed_parameter_walk_forward
            )
            regime_gate = evaluate_regime_acceptance_gate(
                gate=manifest.acceptance_gate.regime_acceptance_gate,
                performance_rows=tuple(base.get("validation_regime_performance") or ()),
            )
            gate_result, fail_reasons = _gate_result(
                manifest=manifest,
                validation_metrics=validation_metrics,
                validation_metrics_v2=validation_metrics_v2,
                walk_forward_metrics=walk_forward,
                stability_score=stability_score,
                include_walk_forward=include_walk_forward,
                regime_gate_result=regime_gate.as_dict(),
                execution_calibration_gate=calibration_gate,
                dataset_quality_status=dataset_quality_status,
                dataset_quality_reasons=dataset_quality_reasons,
            )
            validation_stress_suite = None
            stress_gate_result = None
            stress_fail_reasons: list[str] = []
            stress_contract = (
                manifest.stress_suite.as_dict()
                if manifest.stress_suite is not None
                else None
            )
            stress_contract_hash = (
                sha256_prefixed(stress_contract)
                if stress_contract is not None
                else None
            )
            if manifest.stress_suite is not None:
                signal_omission_runs = _signal_omission_stress_runs(
                    manifest=manifest,
                    snapshot=snapshots["validation"],
                    scenario=scenario,
                    scenario_id=scenario_id,
                    scenario_index=scenario_index,
                    candidate_id=str(base["candidate_id"]),
                    parameter_values=params,
                    plugin=strategy_plugin,
                    registry=strategy_registry,
                    compiled_contract=executed_compiled_contract,
                )
                validation_stress_suite = analyze_stress_suite(
                    contract=manifest.stress_suite,
                    context=StressSuiteContext(
                        manifest_hash=manifest_hash,
                        experiment_id=manifest.experiment_id,
                        candidate_id=base["candidate_id"],
                        scenario_id=scenario_id,
                        split_name="validation",
                        parameter_values=params,
                        portfolio_policy_hash=portfolio_policy_hash,
                        simulation_policy_hash=simulation_policy_hash,
                    ),
                    original_metrics=validation_metrics,
                    metrics_v2=validation_metrics_v2,
                    closed_trades=_closed_trades_for_stress_suite(
                        manager=manager,
                        base=base,
                        split_name="validation",
                    ),
                    starting_cash=manifest.portfolio_policy.starting_cash_krw,
                    parameter_perturbation_candidates=perturbation_candidates,
                    signal_omission_runs=signal_omission_runs,
                )
                stress_fail_reasons.extend(
                    str(reason)
                    for reason in validation_stress_suite.get("fail_reasons") or []
                )
                stress_gate_result = "PASS" if not stress_fail_reasons else "FAIL"
                if (
                    manifest.stress_suite.required_for_validation
                    and stress_gate_result != "PASS"
                ):
                    gate_result = "FAIL"
                    fail_reasons = sorted(
                        set(fail_reasons)
                        | set(stress_fail_reasons)
                        | {"stress_suite_gate_not_passed"}
                    )
            execution_metadata = list(base.get("validation_execution_metadata") or [])
            execution_reality_summary = _execution_reality_summary(
                policy=manifest.execution_timing,
                execution_metadata=execution_metadata,
                execution_event_summary=dict(
                    base.get("validation_execution_event_summary") or {}
                ),
            )
            execution_event_gate_reasons = _execution_event_gate_reasons(
                dict(base.get("validation_execution_event_summary") or {})
            )
            if execution_event_gate_reasons:
                gate_result = "FAIL"
                fail_reasons = sorted(
                    set(fail_reasons) | set(execution_event_gate_reasons)
                )
            if execution_reality_summary["execution_reality_gate_status"] == "FAIL":
                gate_result = "FAIL"
                fail_reasons = sorted(
                    set(fail_reasons)
                    | set(
                        str(item)
                        for item in execution_reality_summary[
                            "execution_reality_gate_reasons"
                        ]
                    )
                )
            if base.get("candidate_failed"):
                gate_result = "FAIL"
                fail_reasons = sorted(
                    set(fail_reasons)
                    | {
                        "candidate_resource_limit_exceeded"
                        if base.get("failure_reason")
                        == "candidate_resource_limit_exceeded"
                        else str(base.get("failure_reason") or "candidate_failed")
                    }
                    | set(
                        str(item)
                        for item in (base.get("resource_guard") or {}).get(
                            "reasons", []
                        )
                    )
                )
            policy_mismatch_reasons = _portfolio_policy_execution_gate_reasons(base)
            if policy_mismatch_reasons:
                gate_result = "FAIL"
                fail_reasons = sorted(set(fail_reasons) | set(policy_mismatch_reasons))
            cost_model = {
                "fee_rate": scenario.fee_rate,
                "slippage_bps": float(scenario.slippage_bps),
            }
            cost_assumption = (
                scenario.cost_assumption.as_dict()
                if scenario.cost_assumption is not None
                else {
                    "label": "",
                    "role": scenario.scenario_role,
                    "fee_rate": scenario.fee_rate,
                    "fee_source": "",
                    "fee_authority_policy": "runtime_fee_authority_or_config_fallback",
                    "slippage_bps": float(scenario.slippage_bps),
                    "slippage_source": "",
                    "validation_eligible_as_base": False,
                    "source": scenario.source,
                }
            )
            execution_model_payload = _scenario_payload(scenario)
            execution_contract = _execution_reality_contract(
                manifest=manifest,
                scenario=scenario,
                calibration_hash=calibration_gate.get("artifact_hash")
                if isinstance(calibration_gate, dict)
                else None,
                top_of_book_available=int(
                    top_of_book_quality_summary.get("joined_quote_count") or 0
                )
                > 0,
                depth_available=l2_depth_complete_snapshots_available,
            )
            capability_contract = _execution_capability_contract_from_reality(
                execution_contract
            )
            capability_fail_reasons = unsupported_capability_reasons(execution_contract)
            if capability_fail_reasons:
                gate_result = "FAIL"
                fail_reasons = sorted(set(fail_reasons) | set(capability_fail_reasons))
            scenario_result = {
                "scenario_id": scenario_id,
                "scenario_index": scenario_index,
                "scenario_type": scenario.type,
                "scenario_role": scenario.scenario_role,
                "scenario_role_source": scenario.scenario_role_source,
                "run_purpose": manifest.research_run.run_purpose,
                "execution_model": execution_model_payload,
                "execution_model_hash": execution_model_payload["model_params_hash"],
                "model_params_hash": execution_model_payload["model_params_hash"],
                "cost_model": cost_model,
                "cost_assumption": cost_assumption,
                "execution_calibration_gate": calibration_gate,
                "execution_timing_policy": manifest.execution_timing.as_dict(),
                "portfolio_policy": portfolio_policy,
                "portfolio_policy_hash": portfolio_policy_hash,
                "work_unit_portfolio_policy_hash": base.get(
                    "work_unit_portfolio_policy_hash"
                ),
                "executed_portfolio_policy": base.get("executed_portfolio_policy"),
                "executed_portfolio_policy_hash": base.get(
                    "executed_portfolio_policy_hash"
                ),
                "ledger_starting_cash_krw": base.get("ledger_starting_cash_krw"),
                "ledger_initial_position_qty": base.get("ledger_initial_position_qty"),
                "position_sizing_policy": base.get("position_sizing_policy"),
                "train_executed_portfolio_policy": base.get(
                    "train_executed_portfolio_policy"
                ),
                "train_executed_portfolio_policy_hash": base.get(
                    "train_executed_portfolio_policy_hash"
                ),
                "validation_executed_portfolio_policy": base.get(
                    "validation_executed_portfolio_policy"
                ),
                "validation_executed_portfolio_policy_hash": base.get(
                    "validation_executed_portfolio_policy_hash"
                ),
                "simulation_policy_hash": simulation_policy_hash,
                "execution_reality_contract": execution_contract,
                "execution_contract_hash": execution_contract[
                    "execution_contract_hash"
                ],
                "execution_capability_contract": capability_contract,
                "execution_capability_contract_hash": capability_contract[
                    "execution_capability_contract_hash"
                ],
                "evidence_tier": capability_contract["evidence_tier"],
                "unavailable_required_capabilities": capability_contract[
                    "unavailable_required_capabilities"
                ],
                "execution_reality_summary": execution_reality_summary,
                "train_execution_event_summary": base.get(
                    "train_execution_event_summary"
                )
                or {},
                "validation_execution_event_summary": base.get(
                    "validation_execution_event_summary"
                )
                or {},
                "train_strategy_diagnostics": base.get("train_strategy_diagnostics")
                or {},
                "validation_strategy_diagnostics": base.get(
                    "validation_strategy_diagnostics"
                )
                or {},
                "strategy_diagnostics": base.get("validation_strategy_diagnostics")
                or {},
                "execution_event_summary": base.get(
                    "validation_execution_event_summary"
                )
                or {},
                "behavior_hash": (base.get("validation_reproduction_hashes") or {}).get(
                    "behavior_hash"
                ),
                "decision_behavior_hash": (
                    base.get("validation_reproduction_hashes") or {}
                ).get("behavior_hash"),
                "trade_ledger_hash": (
                    base.get("validation_reproduction_hashes") or {}
                ).get("trade_ledger_hash"),
                "equity_curve_hash": (
                    base.get("validation_reproduction_hashes") or {}
                ).get("equity_curve_hash"),
                "composite_behavior_hash": (
                    base.get("validation_reproduction_hashes") or {}
                ).get("composite_behavior_hash"),
                "common_decision_behavior_hash": (
                    (base.get("validation_reproduction_hashes") or {}).get(
                        "strategy_behavior_hash"
                    )
                ),
                "strategy_behavior_hash": (
                    base.get("validation_reproduction_hashes") or {}
                ).get("strategy_behavior_hash"),
                "composite_behavior_hash_v2": (
                    (base.get("validation_resource_usage") or {}).get(
                        "composite_behavior_hash_v2"
                    )
                ),
                "train_behavior_hash": (base.get("train_resource_usage") or {}).get(
                    "behavior_hash"
                ),
                "train_composite_behavior_hash": (
                    base.get("train_resource_usage") or {}
                ).get("composite_behavior_hash"),
                "train_composite_behavior_hash_v2": (
                    (base.get("train_resource_usage") or {}).get(
                        "composite_behavior_hash_v2"
                    )
                ),
                "validation_behavior_hash": (
                    base.get("validation_resource_usage") or {}
                ).get("behavior_hash"),
                "validation_composite_behavior_hash": (
                    base.get("validation_resource_usage") or {}
                ).get("composite_behavior_hash"),
                "validation_composite_behavior_hash_v2": (
                    (base.get("validation_resource_usage") or {}).get(
                        "composite_behavior_hash_v2"
                    )
                ),
                "strategy_spec": strategy_spec.as_dict(),
                "strategy_spec_hash": strategy_spec.spec_hash(),
                "strategy_plugin_contract": strategy_plugin.contract_payload(),
                "strategy_plugin_contract_hash": strategy_plugin.contract_hash(),
                "exit_policy": active_exit_policy,
                "exit_policy_hash": active_exit_policy_hash,
                "parameter_values_raw": params,
                "effective_strategy_parameters": effective_params,
                "effective_strategy_parameters_hash": effective_params_hash,
                "strategy_parameter_source_map": parameter_source_map,
                "compiled_strategy_contract": compiled_payload,
                "compiled_strategy_contract_hash": compiled_hash,
                "strategy_registry_hash": registry_hash,
                "capability_contract": compiled_payload["capability_contract"],
                "capability_contract_hash": compiled_payload[
                    "capability_contract_hash"
                ],
                "candidate_regime_policy_applied_in_research": False,
                "candidate_regime_policy_required_for_live": True,
                "candidate_regime_policy_equivalence_required": True,
                "candidate_regime_policy_equivalence_evidence_hash": None,
                "candidate_regime_policy_limitation_reasons": [
                    "research_backtest_candidate_regime_policy_not_applied"
                ],
                "train_metrics": train_metrics,
                "validation_metrics": validation_metrics,
                # Produced while the complete validation result is available;
                # receipt construction consumes this field directly.
                "metrics_hash": (base.get("validation_reproduction_hashes") or {}).get(
                    "metrics_hash"
                ),
                "metrics_schema_version": METRICS_SCHEMA_VERSION,
                "metrics_gate_policy": metrics_gate_policy,
                "metrics_gate_policy_hash": metrics_gate_policy_digest,
                "stress_suite_contract": stress_contract,
                "stress_suite_contract_hash": stress_contract_hash,
                "validation_stress_suite": validation_stress_suite,
                "stress_suite_gate_result": stress_gate_result,
                "stress_suite_fail_reasons": sorted(set(stress_fail_reasons)),
                "train_metrics_v2": train_metrics_v2,
                "validation_metrics_v2": validation_metrics_v2,
                "walk_forward_metrics": walk_forward,
                "fixed_parameter_walk_forward_diagnostics": fixed_parameter_walk_forward,
                "regime_gate_result": regime_gate.as_dict(),
                "market_regime_bucket_performance": base[
                    "validation_regime_performance"
                ],
                "market_regime_coverage": base["validation_regime_coverage"],
                "train_market_regime_bucket_performance": base[
                    "train_regime_performance"
                ],
                "train_market_regime_coverage": base["train_regime_coverage"],
                "allowed_live_regimes": list(regime_gate.allowed_live_regimes),
                "blocked_live_regimes": list(regime_gate.blocked_live_regimes),
                "regime_evidence": regime_gate.evidence,
                "parameter_stability": stability_payload,
                "walk_forward_gate_result": "PASS"
                if walk_forward and walk_forward["return_consistency_pass"]
                else None,
                "scenario_acceptance_gate_result": gate_result,
                "scenario_fail_reasons": fail_reasons,
                "candidate_failed": bool(base.get("candidate_failed")),
                "candidate_failed_before_complete_metrics": bool(
                    base.get("candidate_failed_before_complete_metrics")
                ),
                "evaluation_status": base.get("evaluation_status"),
                "metrics_status": base.get("metrics_status"),
                "metrics_v2_source": base.get("metrics_v2_source"),
                "failure_reason": base.get("failure_reason"),
                "resource_guard": base.get("resource_guard"),
                "suggested_rerun_scope": base.get("suggested_rerun_scope"),
                "failure_artifact_ref": base.get("failure_artifact_ref"),
                "failure_artifact_path": base.get("failure_artifact_path"),
                "detail_artifact_ref": base.get("detail_artifact_ref"),
                "detail_artifact_path": base.get("detail_artifact_path"),
                "detail_artifact_hash": base.get("detail_artifact_hash"),
                "retained_detail_summary": base.get("retained_detail_summary"),
                "train_closed_trade_count": base.get("train_closed_trade_count"),
                "validation_closed_trade_count": base.get(
                    "validation_closed_trade_count"
                ),
                "train_closed_trades_hash": base.get("train_closed_trades_hash"),
                "validation_closed_trades_hash": base.get(
                    "validation_closed_trades_hash"
                ),
                "train_equity_curve_count": base.get("train_equity_curve_count"),
                "validation_equity_curve_count": base.get(
                    "validation_equity_curve_count"
                ),
                "train_resource_usage": base.get("train_resource_usage"),
                "validation_resource_usage": base.get("validation_resource_usage"),
                "train_audit_trace_index": base.get("train_audit_trace_index"),
                "validation_audit_trace_index": base.get(
                    "validation_audit_trace_index"
                ),
                "train_equity_curve": [],
                "validation_equity_curve": [],
                "train_execution_metadata": base.get("train_execution_metadata") or [],
                "validation_execution_metadata": base.get(
                    "validation_execution_metadata"
                )
                or [],
            }
            _apply_fail_reason_classification(
                scenario_result, reason_key="scenario_fail_reasons"
            )
            candidate_payload = aggregates.setdefault(
                base["candidate_id"],
                {
                    "experiment_id": manifest.experiment_id,
                    "manifest_hash": manifest_hash,
                    "dataset_snapshot_id": manifest.dataset.snapshot_id,
                    "dataset_content_hash": dataset_hash,
                    "dataset_quality_hash": dataset_quality_hash,
                    "dataset_quality_gate_status": dataset_quality_status,
                    "dataset_quality_gate_reasons": dataset_quality_reasons,
                    "dataset_quality_report_hashes": {
                        split_name: report.content_hash
                        for split_name, report in sorted(quality_reports.items())
                    },
                    "top_of_book_quality_summary": top_of_book_quality_summary,
                    "execution_timing_policy": manifest.execution_timing.as_dict(),
                    "portfolio_policy": portfolio_policy,
                    "portfolio_policy_hash": portfolio_policy_hash,
                    "simulation_policy_hash": simulation_policy_hash,
                    "execution_reality_contract": _execution_reality_contract(
                        manifest=manifest,
                        scenario=scenario,
                        calibration_hash=calibration_gate.get("artifact_hash")
                        if isinstance(calibration_gate, dict)
                        else None,
                        top_of_book_available=int(
                            top_of_book_quality_summary.get("joined_quote_count") or 0
                        )
                        > 0,
                        depth_available=l2_depth_complete_snapshots_available,
                    ),
                    "strategy_name": manifest.strategy_name,
                    "run_purpose": manifest.research_run.run_purpose,
                    "strategy_spec": strategy_spec.as_dict(),
                    "strategy_spec_hash": strategy_spec.spec_hash(),
                    "strategy_plugin_contract": strategy_plugin.contract_payload(),
                    "strategy_plugin_contract_hash": strategy_plugin.contract_hash(),
                    "exit_policy": active_exit_policy,
                    "exit_policy_hash": active_exit_policy_hash,
                    "parameter_candidate_id": base["candidate_id"],
                    "parameter_values": params,
                    "parameter_values_raw": params,
                    "effective_strategy_parameters": effective_params,
                    "effective_strategy_parameters_hash": effective_params_hash,
                    "strategy_parameter_source_map": parameter_source_map,
                    "compiled_strategy_contract": compiled_payload,
                    "compiled_strategy_contract_hash": compiled_hash,
                    "strategy_registry_hash": registry_hash,
                    "capability_contract": compiled_payload["capability_contract"],
                    "capability_contract_hash": compiled_payload[
                        "capability_contract_hash"
                    ],
                    "candidate_regime_policy_applied_in_research": False,
                    "candidate_regime_policy_required_for_live": True,
                    "candidate_regime_policy_equivalence_required": True,
                    "candidate_regime_policy_equivalence_evidence_hash": None,
                    "candidate_regime_policy_limitation_reasons": [
                        "research_backtest_candidate_regime_policy_not_applied"
                    ],
                    "scenario_policy": manifest.execution_model.scenario_policy,
                    "scenario_results": [],
                    "execution_model_source": manifest.execution_model.source,
                    "cost_assumption_contract": manifest.execution_model.as_dict(),
                    "research_classification": manifest.research_classification,
                    "execution_calibration_required": manifest.execution_model.calibration_required,
                    "execution_calibration_strictness": manifest.execution_model.calibration_strictness,
                    "final_holdout_required_for_validation": manifest.acceptance_gate.final_holdout_required_for_validation,
                    "final_holdout_present": "final_holdout" in snapshots,
                    "walk_forward_required": manifest.acceptance_gate.walk_forward_required,
                    "metrics_gate_policy": metrics_gate_policy,
                    "metrics_gate_policy_hash": metrics_gate_policy_digest,
                    "metrics_contract_required": bool(
                        manifest.acceptance_gate.metrics_contract_required
                    ),
                    "stress_suite_required": stress_suite_required(manifest),
                    "stress_suite_contract": stress_contract,
                    "stress_suite_contract_hash": stress_contract_hash,
                    "regime_classifier_version": MARKET_REGIME_VERSION,
                    "warnings": [],
                    "repository_version": _repository_version(),
                },
            )
            candidate_payload["scenario_results"].append(scenario_result)
            candidate_payload["warnings"] = sorted(
                set(candidate_payload.get("warnings") or ())
                | set(base.get("warnings") or ())
                | set(dataset_warning_codes)
                | set(probe_warnings)
                | set(manifest.portfolio_policy.warning_codes())
            )
            if candidate_payload.get("_primary_scenario_result") is None:
                candidate_payload["_primary_scenario_result"] = scenario_result
    substage_timings.append(
        _stage_timing(
            "scenario_gate_aggregation",
            gate_aggregation_started,
            scenario_count=len(execution_scenarios),
            candidate_count=len(aggregates),
        )
    )

    candidate_payload_started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    for candidate_payload in aggregates.values():
        _apply_scenario_policy(manifest=manifest, candidate=candidate_payload)
        primary = candidate_payload.pop("_primary_scenario_result", None) or (
            candidate_payload["scenario_results"][0]
            if candidate_payload.get("scenario_results")
            else {}
        )
        _declare_candidate_scenario_semantics(
            candidate=candidate_payload,
            primary=primary if isinstance(primary, dict) else {},
            policy=manifest.execution_model.scenario_policy,
        )
        cost_authority = _cost_authority_resolution(manifest)
        candidate_payload.update(
            {
                "cost_model": candidate_payload.get("primary_cost_model"),
                "base_cost_assumption": _primary_base_cost_assumption(
                    candidate_payload
                ),
                "cost_authority_source": cost_authority["cost_authority_source"],
                "cost_authority_resolution": cost_authority[
                    "cost_authority_resolution"
                ],
                "runtime_base_cost_assumption": cost_authority[
                    "runtime_base_cost_assumption"
                ],
                "legacy_cost_model_present": cost_authority[
                    "legacy_cost_model_present"
                ],
                "legacy_cost_model_authority": cost_authority[
                    "legacy_cost_model_authority"
                ],
                "scenario_cost_assumption_contract_hash": cost_authority[
                    "scenario_cost_assumption_contract_hash"
                ],
                "cost_assumption_contract": manifest.execution_model.as_dict(),
                "portfolio_policy": portfolio_policy,
                "portfolio_policy_hash": portfolio_policy_hash,
                "work_unit_portfolio_policy_hash": primary.get(
                    "work_unit_portfolio_policy_hash"
                ),
                "executed_portfolio_policy": primary.get("executed_portfolio_policy"),
                "executed_portfolio_policy_hash": primary.get(
                    "executed_portfolio_policy_hash"
                ),
                "ledger_starting_cash_krw": primary.get("ledger_starting_cash_krw"),
                "ledger_initial_position_qty": primary.get(
                    "ledger_initial_position_qty"
                ),
                "position_sizing_policy": primary.get("position_sizing_policy"),
                "simulation_policy_hash": simulation_policy_hash,
                "execution_model": primary.get("execution_model"),
                "execution_calibration_gate": _combined_calibration_gate(
                    candidate_payload.get("scenario_results") or []
                ),
                "train_metrics": primary.get("train_metrics"),
                "validation_metrics": candidate_payload.get(
                    "primary_validation_metrics"
                ),
                "metrics_schema_version": primary.get("metrics_schema_version"),
                "metrics_gate_policy": primary.get("metrics_gate_policy")
                or candidate_payload.get("metrics_gate_policy"),
                "metrics_gate_policy_hash": primary.get("metrics_gate_policy_hash")
                or candidate_payload.get("metrics_gate_policy_hash"),
                "metrics_contract_required": bool(
                    manifest.acceptance_gate.metrics_contract_required
                ),
                "stress_suite_required": stress_suite_required(manifest),
                "stress_suite_contract": primary.get("stress_suite_contract"),
                "stress_suite_contract_hash": primary.get("stress_suite_contract_hash"),
                "validation_stress_suite": primary.get("validation_stress_suite"),
                "stress_suite_gate_result": primary.get("stress_suite_gate_result"),
                "stress_suite_fail_reasons": primary.get("stress_suite_fail_reasons")
                or [],
                "train_metrics_v2": primary.get("train_metrics_v2"),
                "validation_metrics_v2": primary.get("validation_metrics_v2"),
                "walk_forward_metrics": primary.get("walk_forward_metrics"),
                "market_regime_bucket_performance": primary.get(
                    "market_regime_bucket_performance"
                ),
                "market_regime_coverage": primary.get("market_regime_coverage"),
                "train_market_regime_bucket_performance": primary.get(
                    "train_market_regime_bucket_performance"
                ),
                "train_market_regime_coverage": primary.get(
                    "train_market_regime_coverage"
                ),
                "regime_gate_result": primary.get("regime_gate_result"),
                "allowed_live_regimes": list(primary.get("allowed_live_regimes") or []),
                "blocked_live_regimes": list(primary.get("blocked_live_regimes") or []),
                "regime_evidence": dict(primary.get("regime_evidence") or {}),
                "walk_forward_gate_result": primary.get("walk_forward_gate_result"),
                "parameter_stability": primary.get("parameter_stability"),
                "execution_timing_policy": manifest.execution_timing.as_dict(),
                "execution_reality_contract": primary.get("execution_reality_contract"),
                "execution_contract_hash": primary.get("execution_contract_hash"),
                "execution_capability_contract": primary.get(
                    "execution_capability_contract"
                ),
                "execution_capability_contract_hash": primary.get(
                    "execution_capability_contract_hash"
                ),
                "evidence_tier": primary.get("evidence_tier"),
                "unavailable_required_capabilities": primary.get(
                    "unavailable_required_capabilities"
                ),
                "execution_reality_summary": primary.get("execution_reality_summary"),
                "execution_event_summary": primary.get("execution_event_summary"),
                "behavior_hash": primary.get("behavior_hash"),
                "decision_behavior_hash": primary.get("decision_behavior_hash"),
                "trade_ledger_hash": primary.get("trade_ledger_hash"),
                "equity_curve_hash": primary.get("equity_curve_hash"),
                "composite_behavior_hash": primary.get("composite_behavior_hash"),
                "common_decision_behavior_hash": primary.get(
                    "common_decision_behavior_hash"
                ),
                "strategy_behavior_hash": primary.get("strategy_behavior_hash"),
                "composite_behavior_hash_v2": primary.get("composite_behavior_hash_v2"),
                "train_behavior_hash": primary.get("train_behavior_hash"),
                "train_composite_behavior_hash": primary.get(
                    "train_composite_behavior_hash"
                ),
                "train_composite_behavior_hash_v2": primary.get(
                    "train_composite_behavior_hash_v2"
                ),
                "validation_behavior_hash": primary.get("validation_behavior_hash"),
                "validation_composite_behavior_hash": primary.get(
                    "validation_composite_behavior_hash"
                ),
                "validation_composite_behavior_hash_v2": primary.get(
                    "validation_composite_behavior_hash_v2"
                ),
                "final_holdout_behavior_hash": primary.get(
                    "final_holdout_behavior_hash"
                ),
                "final_holdout_composite_behavior_hash": primary.get(
                    "final_holdout_composite_behavior_hash"
                ),
                "final_holdout_composite_behavior_hash_v2": primary.get(
                    "final_holdout_composite_behavior_hash_v2"
                ),
                "strategy_spec": primary.get("strategy_spec")
                or strategy_spec.as_dict(),
                "strategy_spec_hash": primary.get("strategy_spec_hash")
                or strategy_spec.spec_hash(),
                "exit_policy": primary.get("exit_policy"),
                "exit_policy_hash": primary.get("exit_policy_hash"),
                "parameter_values_raw": primary.get("parameter_values_raw")
                or candidate_payload.get("parameter_values_raw"),
                "effective_strategy_parameters": (
                    primary.get("effective_strategy_parameters")
                    or candidate_payload.get("effective_strategy_parameters")
                ),
                "effective_strategy_parameters_hash": (
                    primary.get("effective_strategy_parameters_hash")
                    or candidate_payload.get("effective_strategy_parameters_hash")
                ),
                "strategy_parameter_source_map": (
                    primary.get("strategy_parameter_source_map")
                    or candidate_payload.get("strategy_parameter_source_map")
                ),
                "candidate_regime_policy_applied_in_research": bool(
                    primary.get("candidate_regime_policy_applied_in_research")
                ),
                "candidate_regime_policy_required_for_live": bool(
                    primary.get("candidate_regime_policy_required_for_live")
                ),
                "candidate_regime_policy_equivalence_required": bool(
                    primary.get("candidate_regime_policy_equivalence_required")
                ),
                "candidate_regime_policy_equivalence_evidence_hash": primary.get(
                    "candidate_regime_policy_equivalence_evidence_hash"
                ),
                "candidate_regime_policy_limitation_reasons": (
                    primary.get("candidate_regime_policy_limitation_reasons") or []
                ),
                "train_execution_event_summary": primary.get(
                    "train_execution_event_summary"
                ),
                "validation_execution_event_summary": primary.get(
                    "validation_execution_event_summary"
                ),
                "final_holdout_execution_event_summary": primary.get(
                    "final_holdout_execution_event_summary"
                ),
                "train_strategy_diagnostics": primary.get("train_strategy_diagnostics"),
                "validation_strategy_diagnostics": primary.get(
                    "validation_strategy_diagnostics"
                ),
                "final_holdout_strategy_diagnostics": primary.get(
                    "final_holdout_strategy_diagnostics"
                ),
                "strategy_diagnostics": primary.get("strategy_diagnostics"),
                "run_purpose": manifest.research_run.run_purpose,
                "candidate_failed": bool(primary.get("candidate_failed")),
                "candidate_failed_before_complete_metrics": bool(
                    primary.get("candidate_failed_before_complete_metrics")
                ),
                "evaluation_status": primary.get("evaluation_status"),
                "metrics_status": primary.get("metrics_status"),
                "metrics_v2_source": primary.get("metrics_v2_source"),
                "failure_reason": primary.get("failure_reason"),
                "resource_guard": primary.get("resource_guard"),
                "suggested_rerun_scope": primary.get("suggested_rerun_scope"),
                "failure_artifact_ref": primary.get("failure_artifact_ref"),
                "failure_artifact_path": primary.get("failure_artifact_path"),
                "detail_artifact_ref": primary.get("detail_artifact_ref"),
                "detail_artifact_path": primary.get("detail_artifact_path"),
                "detail_artifact_hash": primary.get("detail_artifact_hash"),
                "retained_detail_summary": primary.get("retained_detail_summary"),
                "train_resource_usage": primary.get("train_resource_usage"),
                "validation_resource_usage": primary.get("validation_resource_usage"),
                "final_holdout_resource_usage": primary.get(
                    "final_holdout_resource_usage"
                ),
                "train_audit_trace_index": primary.get("train_audit_trace_index"),
                "validation_audit_trace_index": primary.get(
                    "validation_audit_trace_index"
                ),
                "final_holdout_audit_trace_index": primary.get(
                    "final_holdout_audit_trace_index"
                ),
                "train_equity_curve": [],
                "validation_equity_curve": [],
                "final_holdout_equity_curve": [],
                "validation_equity_curve_count": primary.get(
                    "validation_equity_curve_count"
                ),
                "final_holdout_equity_curve_count": primary.get(
                    "final_holdout_equity_curve_count"
                ),
                "validation_equity_curve_hash": primary.get(
                    "validation_equity_curve_hash"
                ),
                "final_holdout_equity_curve_hash": primary.get(
                    "final_holdout_equity_curve_hash"
                ),
            }
        )
        warning_reasons = _execution_calibration_warning_reasons(candidate_payload)
        candidate_payload["has_execution_calibration_warning"] = bool(warning_reasons)
        candidate_payload["execution_calibration_warning_reasons"] = warning_reasons
        if warning_reasons:
            candidate_payload["warnings"] = sorted(
                set(candidate_payload.get("warnings") or ()) | set(warning_reasons)
            )
        policy_result = validate_execution_calibration_policy(
            candidate_payload,
            target=manifest.research_classification,
        )
        candidate_payload["execution_calibration_policy_result"] = (
            policy_result.as_dict()
        )
        candidate_payload["execution_calibration_policy_reasons"] = list(
            policy_result.reasons
        )
        candidate_payload["execution_calibration_policy_source"] = (
            policy_result.policy_source
        )
        if policy_result.artifact_hash is not None:
            candidate_payload["execution_calibration_artifact_hash"] = (
                policy_result.artifact_hash
            )
        if policy_result.artifact_hashes:
            candidate_payload["execution_calibration_artifact_hashes"] = list(
                policy_result.artifact_hashes
            )
        if policy_result.status == "FAIL":
            candidate_payload["acceptance_gate_result"] = "FAIL"
            candidate_payload["aggregate_acceptance_gate_result"] = "FAIL"
            candidate_payload["gate_fail_reasons"] = sorted(
                set(candidate_payload.get("gate_fail_reasons") or ())
                | set(policy_result.reasons)
            )
        _attach_candidate_diagnostic_blocks(
            candidate=candidate_payload,
            manifest=manifest,
            strategy_plugin=strategy_plugin,
        )
        rows.append(candidate_payload)
    substage_timings.append(
        _stage_timing(
            "candidate_payload_aggregation",
            candidate_payload_started,
            candidate_count=len(rows),
        )
    )
    _mark_noop_behavior_hash_groups(
        rows=rows,
        behavior_parameter_names=set(strategy_spec.behavior_affecting_parameter_names),
        validation_required=manifest.research_classification != "research_only",
    )
    for candidate_payload in rows:
        _apply_fail_reason_classification(candidate_payload)
    profile_hash_wall_seconds = 0.0
    profile_build_wall_seconds = 0.0
    behavior_profile_build_wall_seconds = 0.0
    behavior_profile_hash_wall_seconds = 0.0
    profile_hash_observability = _empty_hash_observability()
    behavior_profile_hash_observability = _empty_hash_observability()
    candidate_profile_hash_observability = _empty_hash_observability()
    artifact_write_wall_seconds = 0.0
    append_complete_wall_seconds = 0.0
    for candidate_payload in rows:
        total_started = time.perf_counter()
        profile_build_started = time.perf_counter()
        candidate_profile = build_candidate_profile(candidate_payload)
        profile_build_wall_seconds += time.perf_counter() - profile_build_started

        behavior_profile_build_started = time.perf_counter()
        behavior_profile = build_candidate_behavior_profile(
            candidate_payload,
            base_profile=candidate_profile,
        )
        behavior_profile_build_wall_seconds += (
            time.perf_counter() - behavior_profile_build_started
        )

        profile_hash_started = time.perf_counter()
        with observe_hashing() as profile_hash_observer:
            candidate_payload["candidate_profile_hash"] = sha256_prefixed(
                candidate_profile,
                label="candidate_profile_hash",
            )
        profile_hash_wall_seconds += time.perf_counter() - profile_hash_started
        _merge_hash_observability(
            profile_hash_observability, profile_hash_observer.as_dict()
        )
        _merge_hash_observability(
            candidate_profile_hash_observability, profile_hash_observer.as_dict()
        )

        behavior_profile_hash_started = time.perf_counter()
        with observe_hashing() as behavior_hash_observer:
            candidate_payload["candidate_behavior_profile_hash"] = sha256_prefixed(
                behavior_profile,
                label="candidate_behavior_profile_hash",
            )
        behavior_profile_hash_wall_seconds += (
            time.perf_counter() - behavior_profile_hash_started
        )
        _merge_hash_observability(
            behavior_profile_hash_observability, behavior_hash_observer.as_dict()
        )
        _merge_hash_observability(
            candidate_profile_hash_observability, behavior_hash_observer.as_dict()
        )
        profile_total_wall_seconds = time.perf_counter() - total_started
        store = artifact_context or ResearchArtifactContext(
            manager=manager,
            experiment_id=manifest.experiment_id,
            budget=_artifact_budget_from_limits(manifest.research_run.resource_limits),
        )
        write_started = time.perf_counter()
        write_event = store.write_json_atomic(
            _candidate_result_path(
                manager,
                manifest.experiment_id,
                str(candidate_payload["parameter_candidate_id"]),
            ),
            summarize_candidate_result(
                candidate_payload, manifest.research_run.report_detail
            ),
        )
        artifact_write_wall_seconds += time.perf_counter() - write_started
        candidate_artifact_observability["candidate_result_file_count"] += 1
        candidate_artifact_observability["candidate_result_total_bytes"] += int(
            write_event.bytes
        )
        append_started = time.perf_counter()
        _append_candidate_event(
            manager=manager,
            manifest=manifest,
            artifact_context=artifact_context,
            event={
                "stage": "candidate_complete",
                "candidate_id": candidate_payload["parameter_candidate_id"],
                "acceptance_gate_result": candidate_payload.get(
                    "acceptance_gate_result"
                ),
                "gate_fail_reasons": candidate_payload.get("gate_fail_reasons") or [],
            },
        )
        append_complete_wall_seconds += time.perf_counter() - append_started
        candidate_payload.setdefault("runtime_observability", {})
        if isinstance(candidate_payload["runtime_observability"], dict):
            candidate_payload["runtime_observability"][
                "candidate_profile_hash_total_wall_seconds"
            ] = round(
                profile_total_wall_seconds,
                6,
            )
    candidate_artifact_observability["candidate_result_write_wall_seconds"] = round(
        artifact_write_wall_seconds,
        6,
    )
    substage_timings.append(
        {
            "stage": "candidate_profile_hash",
            "wall_seconds": round(
                profile_build_wall_seconds
                + profile_hash_wall_seconds
                + behavior_profile_build_wall_seconds
                + behavior_profile_hash_wall_seconds,
                6,
            ),
            "candidate_count": len(rows),
        }
    )
    substage_timings.append(
        {
            "stage": "candidate_profile_hash.profile_build",
            "wall_seconds": round(profile_build_wall_seconds, 6),
            "candidate_count": len(rows),
        }
    )
    substage_timings.append(
        {
            "stage": "candidate_profile_hash.profile_hash",
            "wall_seconds": round(profile_hash_wall_seconds, 6),
            "candidate_count": len(rows),
            **profile_hash_observability,
        }
    )
    substage_timings.append(
        {
            "stage": "candidate_profile_hash.behavior_profile_build",
            "wall_seconds": round(behavior_profile_build_wall_seconds, 6),
            "candidate_count": len(rows),
        }
    )
    substage_timings.append(
        {
            "stage": "candidate_profile_hash.behavior_profile_hash",
            "wall_seconds": round(behavior_profile_hash_wall_seconds, 6),
            "candidate_count": len(rows),
            **behavior_profile_hash_observability,
        }
    )
    substage_timings.append(
        {
            "stage": "candidate_profile_hash.total",
            "wall_seconds": round(
                profile_build_wall_seconds
                + profile_hash_wall_seconds
                + behavior_profile_build_wall_seconds
                + behavior_profile_hash_wall_seconds,
                6,
            ),
            "candidate_count": len(rows),
            **candidate_profile_hash_observability,
        }
    )
    substage_timings.append(
        {
            "stage": "candidate_result_artifact_write",
            "wall_seconds": round(artifact_write_wall_seconds, 6),
            **candidate_artifact_observability,
        }
    )
    substage_timings.append(
        {
            "stage": "append_candidate_complete_events",
            "wall_seconds": round(append_complete_wall_seconds, 6),
            "event_count": len(rows),
        }
    )
    return CandidateEvaluationResult(
        candidates=sorted(rows, key=_candidate_rank_key),
        execution_boundary=execution_boundary,
        substage_timings=substage_timings,
        candidate_artifact_observability=candidate_artifact_observability,
        candidate_profile_hash_observability={
            **candidate_profile_hash_observability,
            "candidate_count": len(rows),
            "profile_hash": dict(profile_hash_observability),
            "behavior_profile_hash": dict(behavior_profile_hash_observability),
        },
    )


def _work_unit_split_names(
    *,
    manifest: ExperimentManifest,
    snapshots: dict[str, DatasetSnapshot],
    include_walk_forward: bool,
) -> list[str]:
    work_unit = (
        str(manifest.research_run.execution.work_unit or "candidate_scenario")
        .strip()
        .lower()
    )
    if work_unit == "candidate_scenario":
        return ["candidate_scenario"]
    if work_unit != "candidate_scenario_split":
        raise ResearchValidationError(f"unsupported_research_work_unit:{work_unit}")
    if include_walk_forward or any(name.startswith("window_") for name in snapshots):
        raise ResearchValidationError(
            "candidate_scenario_split_walk_forward_not_supported"
        )
    if "final_holdout" in snapshots:
        raise ResearchValidationError(
            "candidate_scenario_split_final_holdout_not_supported"
        )
    missing = [name for name in ("train", "validation") if name not in snapshots]
    if missing:
        raise ResearchValidationError(
            f"candidate_scenario_split_missing_required_splits:{','.join(missing)}"
        )
    return ["train", "validation"]


def _merge_candidate_scenario_split_results(
    *,
    manifest: ExperimentManifest,
    results: list[ResearchWorkResult],
) -> list[ResearchWorkResult]:
    if (
        str(manifest.research_run.execution.work_unit or "candidate_scenario")
        != "candidate_scenario_split"
    ):
        return results
    grouped: dict[tuple[int, int], list[ResearchWorkResult]] = {}
    passthrough: list[ResearchWorkResult] = []
    for result in results:
        if result.status != "completed":
            passthrough.append(result)
            continue
        grouped.setdefault((result.scenario_index, result.candidate_index), []).append(
            result
        )
    merged: list[ResearchWorkResult] = []
    for key in sorted(grouped):
        group = sorted(grouped[key], key=lambda item: str(item.work_unit.split_name))
        split_names = [str(item.work_unit.split_name) for item in group]
        if split_names != ["train", "validation"]:
            raise ResearchValidationError(
                "candidate_scenario_split_merge_requires_train_validation:"
                + ",".join(split_names)
            )
        train_base = dict(group[0].base_result or {})
        validation_base = dict(group[1].base_result or {})
        merged_base = dict(train_base)
        for name, value in validation_base.items():
            if name.startswith("validation_") or name in {
                "validation_metrics",
                "validation_metrics_v2",
            }:
                merged_base[name] = value
        merged_base.update(
            {
                "work_unit_mode": "candidate_scenario_split",
                "split_work_unit_hashes": [item.work_unit_hash for item in group],
                "warnings": sorted(
                    set(train_base.get("warnings") or ())
                    | set(validation_base.get("warnings") or ())
                ),
                "validation_executed_portfolio_policy": validation_base.get(
                    "validation_executed_portfolio_policy"
                ),
                "validation_executed_portfolio_policy_hash": validation_base.get(
                    "validation_executed_portfolio_policy_hash"
                ),
                "executed_portfolio_policy": validation_base.get(
                    "validation_executed_portfolio_policy"
                ),
                "executed_portfolio_policy_hash": validation_base.get(
                    "validation_executed_portfolio_policy_hash"
                ),
            }
        )
        first = group[0]
        merged_observability = {
            "work_unit": first.work_unit.as_dict(),
            "status": "completed",
            "work_unit_mode": "candidate_scenario_split",
            "merged_split_names": split_names,
            "split_work_unit_hashes": [item.work_unit_hash for item in group],
            "wall_seconds": round(
                sum(
                    float((item.observability or {}).get("wall_seconds") or 0.0)
                    for item in group
                ),
                6,
            ),
            "candles_processed": sum(
                int((item.observability or {}).get("candles_processed") or 0)
                for item in group
            ),
        }
        merged.append(
            ResearchWorkResult(
                work_unit=first.work_unit,
                work_unit_hash=sha256_prefixed(
                    {
                        "work_unit_mode": "candidate_scenario_split",
                        "split_work_unit_hashes": [
                            item.work_unit_hash for item in group
                        ],
                    }
                ),
                candidate_index=first.candidate_index,
                candidate_id=first.candidate_id,
                scenario_index=first.scenario_index,
                scenario_id=first.scenario_id,
                status="completed",
                base_result=merged_base,
                observability=merged_observability,
            )
        )
    return sort_work_results_deterministically(passthrough + merged)


def _normalize_failed_work_result_without_base(
    *,
    manifest: ExperimentManifest,
    result: ResearchWorkResult,
) -> ResearchWorkResult:
    if result.base_result is not None or result.status != "failed":
        return result
    scenario = manifest.execution_model.scenarios[int(result.scenario_index)]
    reason = result.failure_reason or "parallel_executor_exception"
    evidence = result.failure_evidence or {
        "status": "ERROR",
        "reason": reason,
        "phase": "future_result",
    }
    base = _failed_candidate_base_result(
        manifest=manifest,
        work_unit=result.work_unit,
        candidate_index=int(result.candidate_index),
        candidate_id=str(result.candidate_id),
        params=dict(result.work_unit.parameter_values),
        scenario=scenario,
        scenario_index=int(result.scenario_index),
        scenario_id=str(result.scenario_id),
        reason=reason,
        resource_guard=evidence,
    )
    return ResearchWorkResult(
        work_unit=result.work_unit,
        work_unit_hash=result.work_unit_hash,
        candidate_index=result.candidate_index,
        candidate_id=result.candidate_id,
        scenario_index=result.scenario_index,
        scenario_id=result.scenario_id,
        status=result.status,
        base_result=base,
        failure_reason=reason,
        failure_evidence=evidence,
        observability=result.observability,
    )


_DETAIL_ONLY_RESULT_KEYS = frozenset(
    {
        "train_closed_trades",
        "validation_closed_trades",
        "final_holdout_closed_trades",
        "train_equity_curve",
        "validation_equity_curve",
        "final_holdout_equity_curve",
    }
)


def _compact_work_result_with_detail_artifact(
    *,
    manager: ResearchPathManager,
    manifest: ExperimentManifest,
    result: ResearchWorkResult,
    artifact_context: ResearchArtifactContext | None,
) -> ResearchWorkResult:
    if result.base_result is None:
        return result
    base_result = _base_result_with_work_unit_policy_evidence(
        result.base_result, work_unit=result.work_unit
    )
    detail_payload = _json_safe_payload(base_result)
    detail_hash = sha256_prefixed(
        detail_payload, label="candidate_detail_artifact_hash"
    )
    path = _candidate_detail_result_path(
        manager,
        manifest.experiment_id,
        candidate_id=str(result.candidate_id),
        scenario_id=str(result.scenario_id),
        work_unit_hash=str(result.work_unit_hash),
    )
    store = artifact_context or ResearchArtifactContext(
        manager=manager,
        experiment_id=manifest.experiment_id,
        budget=_artifact_budget_from_limits(manifest.research_run.resource_limits),
    )
    store.write_json_atomic(
        path,
        {
            "artifact_type": "candidate_detail_result",
            "schema_version": 2,
            "candidate_id": result.candidate_id,
            "scenario_id": result.scenario_id,
            "work_unit_hash": result.work_unit_hash,
            "detail_artifact_hash": detail_hash,
            "base_result": detail_payload,
        },
    )
    compact = _compact_base_result_for_parent(base_result)
    compact["detail_artifact_ref"] = _data_dir_relative_ref(manager, path)
    compact["detail_artifact_path"] = str(path.resolve())
    compact["detail_artifact_hash"] = detail_hash
    return ResearchWorkResult(
        work_unit=result.work_unit,
        work_unit_hash=result.work_unit_hash,
        candidate_index=result.candidate_index,
        candidate_id=result.candidate_id,
        scenario_index=result.scenario_index,
        scenario_id=result.scenario_id,
        status=result.status,
        base_result=compact,
        failure_reason=result.failure_reason,
        failure_evidence=result.failure_evidence,
        observability=result.observability,
        content_hash=result.content_hash,
    )


def _base_result_with_work_unit_policy_evidence(
    base: dict[str, Any],
    *,
    work_unit: ResearchWorkUnit,
) -> dict[str, Any]:
    enriched = dict(base)
    enriched.setdefault(
        "work_unit_portfolio_policy_hash", work_unit.portfolio_policy_hash
    )
    if enriched.get("executed_portfolio_policy_hash"):
        return enriched
    split_evidence: dict[str, dict[str, Any]] = {}
    for split in ("train", "validation", "final_holdout"):
        resource_usage = enriched.get(f"{split}_resource_usage")
        if not isinstance(resource_usage, dict):
            continue
        evidence = {
            "executed_portfolio_policy": resource_usage.get(
                "executed_portfolio_policy"
            ),
            "executed_portfolio_policy_hash": resource_usage.get(
                "executed_portfolio_policy_hash"
            ),
            "ledger_starting_cash_krw": resource_usage.get("ledger_starting_cash_krw"),
            "ledger_initial_position_qty": resource_usage.get(
                "ledger_initial_position_qty"
            ),
            "position_sizing_policy": resource_usage.get("position_sizing_policy"),
        }
        if evidence["executed_portfolio_policy_hash"]:
            split_evidence[split] = evidence
            enriched.setdefault(
                f"{split}_executed_portfolio_policy",
                evidence["executed_portfolio_policy"],
            )
            enriched.setdefault(
                f"{split}_executed_portfolio_policy_hash",
                evidence["executed_portfolio_policy_hash"],
            )
    primary = (
        split_evidence.get("final_holdout")
        or split_evidence.get("validation")
        or split_evidence.get("train")
    )
    if primary:
        enriched.setdefault(
            "executed_portfolio_policy", primary.get("executed_portfolio_policy")
        )
        enriched.setdefault(
            "executed_portfolio_policy_hash",
            primary.get("executed_portfolio_policy_hash"),
        )
        enriched.setdefault(
            "ledger_starting_cash_krw", primary.get("ledger_starting_cash_krw")
        )
        enriched.setdefault(
            "ledger_initial_position_qty", primary.get("ledger_initial_position_qty")
        )
        enriched.setdefault(
            "position_sizing_policy", primary.get("position_sizing_policy")
        )
    return enriched


def _compact_base_result_for_parent(base: dict[str, Any]) -> dict[str, Any]:
    compact = {
        key: value for key, value in base.items() if key not in _DETAIL_ONLY_RESULT_KEYS
    }
    for split in ("train", "validation", "final_holdout"):
        trades = base.get(f"{split}_closed_trades") or ()
        equity_curve = base.get(f"{split}_equity_curve") or ()
        compact[f"{split}_closed_trade_count"] = len(trades)
        compact[f"{split}_closed_trades_hash"] = sha256_prefixed(
            _json_safe_payload(trades),
            label=f"{split}_closed_trades_hash",
        )
        compact[f"{split}_equity_curve_count"] = len(equity_curve)
        compact[f"{split}_equity_curve_hash"] = sha256_prefixed(
            _json_safe_payload(equity_curve),
            label=f"{split}_equity_curve_hash",
        )
    return compact


def _json_safe_payload(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return _json_safe_payload(value.as_dict())
    if isinstance(value, dict):
        return {str(key): _json_safe_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_payload(item) for item in value]
    return value


def _mark_noop_behavior_hash_groups(
    *,
    rows: list[dict[str, Any]],
    behavior_parameter_names: set[str],
    validation_required: bool,
) -> None:
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        behavior_hash = str(row.get("behavior_hash") or "")
        if behavior_hash:
            by_hash.setdefault(behavior_hash, []).append(row)
    for behavior_hash, group in by_hash.items():
        if len(group) < 2:
            continue
        behavior_param_sets = {
            tuple(
                sorted(
                    (key, repr(value))
                    for key, value in (row.get("parameter_values") or {}).items()
                    if key in behavior_parameter_names
                )
            )
            for row in group
        }
        if len(behavior_param_sets) < 2:
            continue
        ids = sorted(str(row.get("parameter_candidate_id") or "") for row in group)
        for row in group:
            row["no_op_behavior_hash_detected"] = True
            row["no_op_behavior_hash"] = behavior_hash
            row["no_op_behavior_candidate_ids"] = ids
            if validation_required:
                row["acceptance_gate_result"] = "FAIL"
                row["gate_fail_reasons"] = sorted(
                    set(row.get("gate_fail_reasons") or ())
                    | {"no_op_behavior_parameter_detected"}
                )


def _evaluate_candidate_base_result(
    *,
    manifest: ExperimentManifest,
    manager: ResearchPathManager | None,
    plugin: Any,
    snapshots: dict[str, DatasetSnapshot],
    params: dict[str, Any],
    index: int,
    raw_candidate_count: int,
    scenario: ExecutionScenario,
    scenario_index: int,
    scenario_id: str,
    manifest_hash: str,
    simulation_seed_scope_hash: str,
    include_walk_forward: bool,
    work_unit: ResearchWorkUnit,
    work_unit_observability: list[dict[str, Any]] | None,
    progress_callback: ProgressCallback | None,
    artifact_context: ResearchArtifactContext | None = None,
    compiled_contract: Any = None,
) -> dict[str, Any]:
    param_candidate_id = candidate_id(params, index)
    work_started = time.perf_counter()
    work_cpu_started = time.process_time()
    split_observability: list[dict[str, Any]] = []
    _emit_progress(
        progress_callback,
        stage="work_unit_start",
        candidate_id=param_candidate_id,
        scenario_id=scenario_id,
        scenario_index=scenario_index,
        work_unit_hash=work_unit.work_unit_hash,
        work_unit_type="candidate_scenario",
    )

    def _run(split_name: str) -> BacktestRun:
        executable_scenarios = required_execution_scenarios(
            manifest.execution_model.scenarios
        )
        executable_scenario_count = len(executable_scenarios)
        scenario_ordinal = next(
            (
                ordinal
                for ordinal, (required_index, _) in enumerate(
                    executable_scenarios, start=1
                )
                if required_index == scenario_index
            ),
            min(scenario_index + 1, executable_scenario_count),
        )
        _emit_progress(
            progress_callback,
            stage="evaluate",
            scenario=f"{scenario_ordinal}/{executable_scenario_count}",
            candidate=f"{index + 1}/{raw_candidate_count}",
            split=split_name,
            candles=len(snapshots[split_name].candles),
            candidate_id=param_candidate_id,
            report_detail=manifest.research_run.report_detail,
        )
        context = _backtest_context(
            manifest=manifest,
            manager=manager,
            candidate_id=param_candidate_id,
            scenario_id=scenario_id,
            scenario_index=scenario_index,
            split_name=split_name,
            dataset_content_hash=snapshots[split_name].snapshot_fingerprint_hash(),
            parameter_values=params,
            progress_callback=progress_callback,
            artifact_context=artifact_context,
        )
        try:
            split_started = time.perf_counter()
            split_cpu_started = time.process_time()
            execution_model = _execution_model_from_scenario(
                scenario,
                seed_context=_seed_context(
                    simulation_seed_scope_hash=simulation_seed_scope_hash,
                    scenario=scenario,
                    scenario_id=scenario_id,
                    parameter_candidate_id=param_candidate_id,
                    split_name=split_name,
                ),
            )

            def runner_call() -> BacktestRun:
                return run_common_simulation_backtest(
                    plugin=plugin,
                    dataset=snapshots[split_name],
                    parameter_values=params,
                    fee_rate=scenario.fee_rate,
                    slippage_bps=float(scenario.slippage_bps),
                    parameter_stability_score=None,
                    execution_model=execution_model,
                    execution_timing_policy=manifest.execution_timing,
                    portfolio_policy=manifest.portfolio_policy,
                    risk_policy=manifest.risk_policy,
                    context=context,
                    compiled_contract=compiled_contract,
                )

            profile_observability: dict[str, Any] = {}
            if manifest.research_run.diagnostic_mode == "profiling":
                if manager is None:
                    raise ResearchValidationError(
                        "profiling_requires_main_process_artifact_manager"
                    )
                result, profile_observability = run_with_cprofile(
                    func=runner_call,
                    manager=manager,
                    experiment_id=manifest.experiment_id,
                    candidate_id=param_candidate_id,
                    scenario_id=scenario_id,
                    split_name=split_name,
                    candles_processed=len(snapshots[split_name].candles),
                )
            else:
                result = runner_call()
            validate_execution_evidence(
                run=result,
                timing=manifest.execution_timing,
                model=execution_model,
                validation_bound=requires_candidate_validation(
                    manifest.research_classification
                ),
            )
            wall_seconds = time.perf_counter() - split_started
            cpu_seconds = time.process_time() - split_cpu_started
            candles = len(snapshots[split_name].candles)
            split_payload = {
                "split_name": split_name,
                "status": "completed",
                "wall_seconds": round(wall_seconds, 6),
                "cpu_seconds": round(cpu_seconds, 6),
                "candles_processed": candles,
                "candles_per_second": round(candles / wall_seconds, 6)
                if wall_seconds > 0
                else None,
            }
            split_payload.update(profile_observability)
            split_observability.append(split_payload)
            if profile_observability:
                resource_usage = dict(result.resource_usage or {})
                resource_usage.update(profile_observability)
                result = replace(result, resource_usage=resource_usage)
            return result
        except Exception as exc:
            split_observability.append(
                {
                    "split_name": split_name,
                    "status": "failed",
                    "failure_reason": type(exc).__name__,
                    "candles_processed": len(snapshots[split_name].candles),
                }
            )
            if context.audit_trace is not None and not isinstance(
                getattr(exc, "audit_trace_index", None), dict
            ):
                audit_index = context.audit_trace.complete(status="failed")
                if isinstance(exc, BacktestResourceLimitExceeded):
                    exc.evidence.setdefault("audit_trace_index", audit_index)
                    exc.evidence.setdefault("split", split_name)
                else:
                    setattr(exc, "audit_trace_index", audit_index)
                    setattr(exc, "failed_split", split_name)
            raise

    if work_unit.work_unit_mode == "candidate_scenario_split":
        split_name = str(work_unit.split_name)
        if split_name not in {"train", "validation"}:
            raise ResearchValidationError(
                f"candidate_scenario_split_unsupported_split:{split_name}"
            )
        split_run = _run(split_name)
        executed_policy_evidence = _candidate_split_executed_portfolio_policy_evidence(
            split_name=split_name,
            run=split_run,
            work_unit=work_unit,
        )
        work_wall_seconds = time.perf_counter() - work_started
        work_cpu_seconds = time.process_time() - work_cpu_started
        candles_processed = sum(
            int(item.get("candles_processed") or 0) for item in split_observability
        )
        work_observability = {
            "work_unit": work_unit.as_dict(),
            "status": "completed",
            "wall_seconds": round(work_wall_seconds, 6),
            "cpu_seconds": round(work_cpu_seconds, 6),
            "candles_processed": candles_processed,
            "candles_per_second": round(candles_processed / work_wall_seconds, 6)
            if work_wall_seconds > 0
            else None,
            "split_results": split_observability,
            "content_hash": sha256_prefixed(
                {
                    "work_unit_hash": work_unit.work_unit_hash,
                    "status": "completed",
                    "split_name": split_name,
                    "candles_processed": candles_processed,
                }
            ),
        }
        if work_unit_observability is not None:
            work_unit_observability.append(work_observability)
        _emit_progress(
            progress_callback,
            stage="work_unit_complete",
            candidate_id=param_candidate_id,
            scenario_id=scenario_id,
            scenario_index=scenario_index,
            work_unit_hash=work_unit.work_unit_hash,
            wall_seconds=round(work_wall_seconds, 3),
            candles_processed=candles_processed,
        )
        base = _partial_split_base_result(
            manifest=manifest,
            params=params,
            index=index,
            candidate_id_value=param_candidate_id,
            split_name=split_name,
            split_run=split_run,
            work_unit=work_unit,
            executed_policy_evidence=executed_policy_evidence,
        )
        base.update(
            {
                "compiled_strategy_contract": compiled_contract.as_dict(),
                "compiled_strategy_contract_hash": compiled_contract.compiled_contract_hash,
                "strategy_registry_hash": compiled_contract.strategy_registry_hash,
                "strategy_plugin_contract_hash": compiled_contract.strategy_plugin_contract_hash,
            }
        )
        return base

    train = _run("train")
    validation = _run("validation")
    walk_forward = (
        _walk_forward_metrics(
            manifest=manifest,
            snapshots=snapshots,
            parameter_values=params,
            fee_rate=scenario.fee_rate,
            scenario=scenario,
            scenario_id=scenario_id,
            scenario_index=scenario_index,
            manager=manager,
            parameter_candidate_id=param_candidate_id,
            parameter_stability_score=None,
            progress_callback=progress_callback,
            artifact_context=artifact_context,
            plugin=plugin,
            compiled_contract=compiled_contract,
        )
        if include_walk_forward
        else None
    )
    executed_policy_evidence = _candidate_executed_portfolio_policy_evidence(
        train=train,
        validation=validation,
        work_unit=work_unit,
    )
    work_wall_seconds = time.perf_counter() - work_started
    work_cpu_seconds = time.process_time() - work_cpu_started
    candles_processed = sum(
        int(item.get("candles_processed") or 0) for item in split_observability
    )
    work_observability = {
        "work_unit": work_unit.as_dict(),
        "status": "completed",
        "wall_seconds": round(work_wall_seconds, 6),
        "cpu_seconds": round(work_cpu_seconds, 6),
        "candles_processed": candles_processed,
        "candles_per_second": round(candles_processed / work_wall_seconds, 6)
        if work_wall_seconds > 0
        else None,
        "split_results": split_observability,
        "content_hash": sha256_prefixed(
            {
                "work_unit_hash": work_unit.work_unit_hash,
                "status": "completed",
                "split_names": [item.get("split_name") for item in split_observability],
                "candles_processed": candles_processed,
            }
        ),
    }
    if work_unit_observability is not None:
        work_unit_observability.append(work_observability)
    _emit_progress(
        progress_callback,
        stage="work_unit_complete",
        candidate_id=param_candidate_id,
        scenario_id=scenario_id,
        scenario_index=scenario_index,
        work_unit_hash=work_unit.work_unit_hash,
        wall_seconds=round(work_wall_seconds, 3),
        candles_processed=candles_processed,
    )
    return {
        "index": index,
        "candidate_id": param_candidate_id,
        "run_purpose": manifest.research_run.run_purpose,
        "candidate_failed": False,
        "candidate_failed_before_complete_metrics": False,
        "evaluation_status": "completed",
        "metrics_status": "complete",
        "work_unit_portfolio_policy_hash": work_unit.portfolio_policy_hash,
        **executed_policy_evidence,
        "metrics_v2_source": "computed",
        "parameter_values": params,
        "compiled_strategy_contract": compiled_contract.as_dict(),
        "compiled_strategy_contract_hash": compiled_contract.compiled_contract_hash,
        "strategy_registry_hash": compiled_contract.strategy_registry_hash,
        "strategy_plugin_contract_hash": compiled_contract.strategy_plugin_contract_hash,
        "train_metrics": train.metrics.as_dict(),
        "validation_metrics": validation.metrics.as_dict(),
        "train_metrics_v2": _metrics_v2_payload(train),
        "validation_metrics_v2": _metrics_v2_payload(validation),
        "train_closed_trades": train.closed_trades,
        "validation_closed_trades": validation.closed_trades,
        "train_equity_curve": [point.as_dict() for point in train.equity_curve],
        "validation_equity_curve": [
            point.as_dict() for point in validation.equity_curve
        ],
        "train_execution_metadata": _execution_metadata(train.trades),
        "validation_execution_metadata": _execution_metadata(validation.trades),
        "train_execution_event_summary": train.execution_event_summary
        or execution_event_summary(train.trades),
        "validation_execution_event_summary": validation.execution_event_summary
        or execution_event_summary(validation.trades),
        "train_strategy_diagnostics": train.strategy_diagnostics or {},
        "validation_strategy_diagnostics": validation.strategy_diagnostics or {},
        "train_regime_performance": [row.as_dict() for row in train.regime_performance],
        "train_regime_coverage": [row.as_dict() for row in train.regime_coverage],
        "validation_regime_performance": [
            row.as_dict() for row in validation.regime_performance
        ],
        "validation_regime_coverage": [
            row.as_dict() for row in validation.regime_coverage
        ],
        "walk_forward_metrics": walk_forward,
        "warnings": sorted(set(train.warnings + validation.warnings)),
        "train_resource_usage": train.resource_usage,
        "validation_resource_usage": validation.resource_usage,
        "train_reproduction_hashes": _reproduction_result_hashes(train),
        "validation_reproduction_hashes": _reproduction_result_hashes(validation),
        "train_audit_trace_index": train.audit_trace_index,
        "validation_audit_trace_index": validation.audit_trace_index,
        "retained_detail_summary": validation.retained_detail_summary,
    }


def _record_failed_work_unit(
    *,
    work_unit_observability: list[dict[str, Any]] | None,
    work_unit: ResearchWorkUnit,
    reason: str,
    resource_guard: dict[str, Any],
    limits: Any,
) -> None:
    if work_unit_observability is None:
        return
    rerun_scope = _suggested_rerun_scope(
        candidate_id=work_unit.candidate_id,
        scenario_id=work_unit.scenario_id,
        reason=reason,
        resource_guard=resource_guard,
        limits=limits,
    )
    work_unit_observability.append(
        {
            "work_unit": work_unit.as_dict(),
            "status": "failed",
            "failure_reason": reason,
            "resource_guard": resource_guard,
            "suggested_rerun_scope": rerun_scope,
            "content_hash": sha256_prefixed(
                {
                    "work_unit_hash": work_unit.work_unit_hash,
                    "status": "failed",
                    "failure_reason": reason,
                }
            ),
        }
    )


def _candidate_executed_portfolio_policy_evidence(
    *,
    train: BacktestRun,
    validation: BacktestRun,
    work_unit: ResearchWorkUnit,
) -> dict[str, Any]:
    split_evidence = {
        "train": _run_portfolio_policy_evidence(train),
        "validation": _run_portfolio_policy_evidence(validation),
    }
    primary = split_evidence.get("validation") or split_evidence.get("train") or {}
    payload: dict[str, Any] = {
        "work_unit_portfolio_policy_hash": work_unit.portfolio_policy_hash,
        "train_executed_portfolio_policy": split_evidence["train"].get(
            "executed_portfolio_policy"
        ),
        "train_executed_portfolio_policy_hash": split_evidence["train"].get(
            "executed_portfolio_policy_hash"
        ),
        "validation_executed_portfolio_policy": split_evidence["validation"].get(
            "executed_portfolio_policy"
        ),
        "validation_executed_portfolio_policy_hash": split_evidence["validation"].get(
            "executed_portfolio_policy_hash"
        ),
        "executed_portfolio_policy": primary.get("executed_portfolio_policy"),
        "executed_portfolio_policy_hash": primary.get("executed_portfolio_policy_hash"),
        "ledger_starting_cash_krw": primary.get("ledger_starting_cash_krw"),
        "ledger_initial_position_qty": primary.get("ledger_initial_position_qty"),
        "position_sizing_policy": primary.get("position_sizing_policy"),
    }
    return payload


def _reproduction_result_hashes(run: BacktestRun) -> dict[str, str]:
    """Record deterministic result evidence while the complete result is present.

    Report detail reduction may discard ledgers and curves later, so these are
    produced here once from the completed run and carried as explicit report
    fields.  Receipt construction only validates these recorded values.
    """

    strategy_behavior_hash = sha256_prefixed(
        [
            item.as_dict() if hasattr(item, "as_dict") else item
            for item in run.decisions
        ],
        label="reproduction_strategy_behavior",
    )
    trade_ledger_hash = sha256_prefixed(
        _execution_metadata(run.trades), label="reproduction_trade_ledger"
    )
    equity_curve_hash = sha256_prefixed(
        [point.as_dict() for point in run.equity_curve],
        label="reproduction_equity_curve",
    )
    metrics_hash = sha256_prefixed(
        {"metrics": run.metrics.as_dict(), "metrics_v2": _metrics_v2_payload(run)},
        label="reproduction_metrics",
    )
    behavior_hash = sha256_prefixed(
        {
            "strategy_behavior_hash": strategy_behavior_hash,
            "trade_ledger_hash": trade_ledger_hash,
            "equity_curve_hash": equity_curve_hash,
        },
        label="reproduction_behavior",
    )
    composite_behavior_hash = sha256_prefixed(
        {"behavior_hash": behavior_hash, "metrics_hash": metrics_hash},
        label="reproduction_composite_behavior",
    )
    return {
        "behavior_hash": behavior_hash,
        "strategy_behavior_hash": strategy_behavior_hash,
        "trade_ledger_hash": trade_ledger_hash,
        "equity_curve_hash": equity_curve_hash,
        "metrics_hash": metrics_hash,
        "composite_behavior_hash": composite_behavior_hash,
    }


def _candidate_split_executed_portfolio_policy_evidence(
    *,
    split_name: str,
    run: BacktestRun,
    work_unit: ResearchWorkUnit,
) -> dict[str, Any]:
    evidence = _run_portfolio_policy_evidence(run)
    return {
        "work_unit_portfolio_policy_hash": work_unit.portfolio_policy_hash,
        f"{split_name}_executed_portfolio_policy": evidence.get(
            "executed_portfolio_policy"
        ),
        f"{split_name}_executed_portfolio_policy_hash": evidence.get(
            "executed_portfolio_policy_hash"
        ),
        "executed_portfolio_policy": evidence.get("executed_portfolio_policy"),
        "executed_portfolio_policy_hash": evidence.get(
            "executed_portfolio_policy_hash"
        ),
        "ledger_starting_cash_krw": evidence.get("ledger_starting_cash_krw"),
        "ledger_initial_position_qty": evidence.get("ledger_initial_position_qty"),
        "position_sizing_policy": evidence.get("position_sizing_policy"),
    }


def _partial_split_base_result(
    *,
    manifest: ExperimentManifest,
    params: dict[str, Any],
    index: int,
    candidate_id_value: str,
    split_name: str,
    split_run: BacktestRun,
    work_unit: ResearchWorkUnit,
    executed_policy_evidence: dict[str, Any],
) -> dict[str, Any]:
    metrics = split_run.metrics.as_dict()
    metrics_v2 = _metrics_v2_payload(split_run)
    payload: dict[str, Any] = {
        "index": index,
        "candidate_id": candidate_id_value,
        "run_purpose": manifest.research_run.run_purpose,
        "candidate_failed": False,
        "candidate_failed_before_complete_metrics": False,
        "evaluation_status": "completed",
        "metrics_status": "partial_split",
        "work_unit_mode": "candidate_scenario_split",
        "work_unit_portfolio_policy_hash": work_unit.portfolio_policy_hash,
        **executed_policy_evidence,
        "metrics_v2_source": "computed",
        "parameter_values": params,
        "warnings": sorted(set(split_run.warnings)),
        "retained_detail_summary": split_run.retained_detail_summary,
        "walk_forward_metrics": None,
    }
    payload[f"{split_name}_metrics"] = metrics
    payload[f"{split_name}_metrics_v2"] = metrics_v2
    payload[f"{split_name}_closed_trades"] = split_run.closed_trades
    payload[f"{split_name}_equity_curve"] = [
        point.as_dict() for point in split_run.equity_curve
    ]
    payload[f"{split_name}_execution_metadata"] = _execution_metadata(split_run.trades)
    payload[f"{split_name}_execution_event_summary"] = (
        split_run.execution_event_summary or execution_event_summary(split_run.trades)
    )
    payload[f"{split_name}_strategy_diagnostics"] = split_run.strategy_diagnostics or {}
    payload[f"{split_name}_regime_performance"] = [
        row.as_dict() for row in split_run.regime_performance
    ]
    payload[f"{split_name}_regime_coverage"] = [
        row.as_dict() for row in split_run.regime_coverage
    ]
    payload[f"{split_name}_resource_usage"] = split_run.resource_usage
    payload[f"{split_name}_reproduction_hashes"] = _reproduction_result_hashes(
        split_run
    )
    payload[f"{split_name}_audit_trace_index"] = split_run.audit_trace_index
    return payload


def _run_portfolio_policy_evidence(run: BacktestRun | None) -> dict[str, Any]:
    if run is None or not isinstance(run.resource_usage, dict):
        return {}
    resource_usage = run.resource_usage
    return {
        "executed_portfolio_policy": resource_usage.get("executed_portfolio_policy"),
        "executed_portfolio_policy_hash": resource_usage.get(
            "executed_portfolio_policy_hash"
        ),
        "ledger_starting_cash_krw": resource_usage.get("ledger_starting_cash_krw"),
        "ledger_initial_position_qty": resource_usage.get(
            "ledger_initial_position_qty"
        ),
        "position_sizing_policy": resource_usage.get("position_sizing_policy"),
    }


def _resource_guard_portfolio_policy_evidence(
    resource_guard: dict[str, Any],
) -> dict[str, Any]:
    return {
        "executed_portfolio_policy": resource_guard.get("executed_portfolio_policy"),
        "executed_portfolio_policy_hash": resource_guard.get(
            "executed_portfolio_policy_hash"
        ),
        "ledger_starting_cash_krw": resource_guard.get("ledger_starting_cash_krw"),
        "ledger_initial_position_qty": resource_guard.get(
            "ledger_initial_position_qty"
        ),
        "position_sizing_policy": resource_guard.get("position_sizing_policy"),
    }


def _failed_candidate_base_result(
    *,
    manifest: ExperimentManifest,
    work_unit: ResearchWorkUnit | None = None,
    candidate_index: int,
    candidate_id: str,
    params: dict[str, Any],
    scenario: ExecutionScenario,
    scenario_index: int,
    scenario_id: str,
    reason: str,
    resource_guard: dict[str, Any],
) -> dict[str, Any]:
    metrics = _failed_metrics_payload()
    evaluation_status = (
        "resource_limited"
        if reason == "candidate_resource_limit_exceeded"
        or (
            isinstance(resource_guard, dict)
            and any(
                str(item).startswith("max_")
                for item in resource_guard.get("reasons") or []
            )
        )
        else "evaluation_failed"
    )
    metrics_v2 = _failed_metrics_v2_payload(evaluation_status=evaluation_status)
    split = (
        str(resource_guard.get("split") or "unknown")
        if isinstance(resource_guard, dict)
        else "unknown"
    )
    audit_index = (
        resource_guard.get("audit_trace_index")
        if isinstance(resource_guard.get("audit_trace_index"), dict)
        else None
    )
    work_unit_policy_hash = (
        work_unit.portfolio_policy_hash if work_unit is not None else None
    )
    rerun_scope = _suggested_rerun_scope(
        candidate_id=candidate_id,
        scenario_id=scenario_id,
        reason=reason,
        resource_guard=resource_guard,
        limits=manifest.research_run.resource_limits,
    )
    policy_evidence = (
        _resource_guard_portfolio_policy_evidence(resource_guard)
        if isinstance(resource_guard, dict)
        else {}
    )
    split_policy_evidence: dict[str, Any] = {}
    if policy_evidence.get("executed_portfolio_policy_hash") and split in {
        "train",
        "validation",
    }:
        split_policy_evidence = {
            f"{split}_executed_portfolio_policy": policy_evidence.get(
                "executed_portfolio_policy"
            ),
            f"{split}_executed_portfolio_policy_hash": policy_evidence.get(
                "executed_portfolio_policy_hash"
            ),
        }
    return {
        "index": candidate_index,
        "candidate_id": candidate_id,
        "run_purpose": manifest.research_run.run_purpose,
        "parameter_values": params,
        "work_unit_portfolio_policy_hash": work_unit_policy_hash,
        **policy_evidence,
        **split_policy_evidence,
        "train_metrics": metrics,
        "validation_metrics": metrics,
        "train_metrics_v2": metrics_v2,
        "validation_metrics_v2": metrics_v2,
        "train_execution_metadata": [],
        "validation_execution_metadata": [],
        "train_execution_event_summary": {},
        "validation_execution_event_summary": {},
        "train_regime_performance": [],
        "train_regime_coverage": [],
        "validation_regime_performance": [],
        "validation_regime_coverage": [],
        "walk_forward_metrics": None,
        "warnings": [reason],
        "candidate_failed": True,
        "candidate_failed_before_complete_metrics": True,
        "evaluation_status": evaluation_status,
        "metrics_status": "unavailable",
        "metrics_v2_source": "failure_fallback",
        "failure_reason": reason,
        "resource_guard": resource_guard,
        "suggested_rerun_scope": rerun_scope,
        "failed_split": split,
        "scenario_id": scenario_id,
        "scenario_index": scenario_index,
        "scenario_type": scenario.type,
        "research_run_policy": manifest.research_run.as_dict(),
        "train_audit_trace_index": audit_index if split == "train" else None,
        "validation_audit_trace_index": audit_index if split == "validation" else None,
        "final_holdout_audit_trace_index": audit_index
        if split == "final_holdout"
        else None,
    }


def _suggested_rerun_scope(
    *,
    candidate_id: str,
    scenario_id: str,
    reason: str,
    resource_guard: dict[str, Any],
    limits: Any,
) -> dict[str, Any] | None:
    if not isinstance(resource_guard, dict):
        return None
    guard_reasons = [str(item) for item in resource_guard.get("reasons") or []]
    if reason != "candidate_resource_limit_exceeded" and not any(
        item in RESOURCE_INTEGRITY_REASON_CODES for item in guard_reasons
    ):
        return None
    return {
        "candidate_id": candidate_id,
        "scenario_id": scenario_id,
        "failed_split": str(resource_guard.get("split") or "unknown"),
        "reason": reason,
        "resource_guard_reasons": sorted(set(guard_reasons)),
        "candles_processed": int(resource_guard.get("candles_processed") or 0),
        "total_candles": int(resource_guard.get("total_candles") or 0),
        "original_max_runtime_s_per_candidate_split": getattr(
            limits,
            "max_runtime_s_per_candidate_split",
            None,
        ),
        "recommended_rerun_mode": "narrow_candidate_single_split",
    }


def _collect_audit_trace_indexes(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    indexes: list[dict[str, Any]] = []
    for candidate in candidates:
        scenario_results = candidate.get("scenario_results")
        if not isinstance(scenario_results, list):
            continue
        for scenario in scenario_results:
            if not isinstance(scenario, dict):
                continue
            for key in (
                "train_audit_trace_index",
                "validation_audit_trace_index",
                "final_holdout_audit_trace_index",
            ):
                value = scenario.get(key)
                if isinstance(value, dict):
                    indexes.append(value)
            walk_forward = scenario.get("walk_forward_metrics")
            windows = (
                walk_forward.get("windows") if isinstance(walk_forward, dict) else None
            )
            if isinstance(windows, list):
                for window in windows:
                    if not isinstance(window, dict):
                        continue
                    for key in ("train_audit_trace_index", "test_audit_trace_index"):
                        value = window.get(key)
                        if isinstance(value, dict):
                            indexes.append(value)
    return indexes


def _pre_stress_gate_summaries(
    *,
    manifest: ExperimentManifest,
    base_results: list[dict[str, Any]],
    stability: dict[int, dict[str, Any]],
    include_walk_forward: bool,
    calibration_gate: dict[str, Any],
    dataset_quality_status: str,
    dataset_quality_reasons: list[str],
) -> dict[int, dict[str, Any]]:
    summaries: dict[int, dict[str, Any]] = {}
    for base in base_results:
        index = int(base["index"])
        validation_metrics = dict(base["validation_metrics"])
        regime_gate = evaluate_regime_acceptance_gate(
            gate=manifest.acceptance_gate.regime_acceptance_gate,
            performance_rows=tuple(base.get("validation_regime_performance") or ()),
        )
        gate_result, fail_reasons = _gate_result(
            manifest=manifest,
            validation_metrics=validation_metrics,
            validation_metrics_v2=dict(base["validation_metrics_v2"]),
            walk_forward_metrics=base["walk_forward_metrics"],
            stability_score=stability[index]["score"],
            include_walk_forward=include_walk_forward,
            regime_gate_result=regime_gate.as_dict(),
            execution_calibration_gate=calibration_gate,
            dataset_quality_status=dataset_quality_status,
            dataset_quality_reasons=dataset_quality_reasons,
        )
        if base.get("candidate_failed"):
            gate_result = "FAIL"
            fail_reasons = sorted(
                set(fail_reasons)
                | {
                    "candidate_resource_limit_exceeded"
                    if base.get("failure_reason") == "candidate_resource_limit_exceeded"
                    else str(base.get("failure_reason") or "candidate_failed")
                }
                | set(
                    str(item)
                    for item in (base.get("resource_guard") or {}).get("reasons", [])
                )
            )
        policy_mismatch_reasons = _portfolio_policy_execution_gate_reasons(base)
        if policy_mismatch_reasons:
            gate_result = "FAIL"
            fail_reasons = sorted(set(fail_reasons) | set(policy_mismatch_reasons))
        summaries[index] = {
            "gate_result": gate_result,
            "fail_reasons": sorted(set(fail_reasons)),
        }
    return summaries


def _portfolio_policy_execution_gate_reasons(base: dict[str, Any]) -> list[str]:
    declared_hash = str(base.get("work_unit_portfolio_policy_hash") or "").strip()
    executed_hash = str(base.get("executed_portfolio_policy_hash") or "").strip()
    if not declared_hash:
        return []
    if not executed_hash:
        return [MISSING_EXECUTED_PORTFOLIO_POLICY_EVIDENCE_REASON]
    if executed_hash != declared_hash:
        return [PORTFOLIO_POLICY_EXECUTION_MISMATCH_REASON]
    expected_splits = {"train", "validation"}
    for split in ("train", "validation"):
        split_hash = base.get(f"{split}_executed_portfolio_policy_hash")
        if split_hash is None or str(split_hash).strip() == "":
            if split in expected_splits:
                return [MISSING_EXECUTED_PORTFOLIO_POLICY_EVIDENCE_REASON]
            continue
        if str(split_hash).strip() != declared_hash:
            return [PORTFOLIO_POLICY_EXECUTION_MISMATCH_REASON]
    return []


SIMULATION_INTEGRITY_REASON_CODES = {
    MISSING_EXECUTED_PORTFOLIO_POLICY_EVIDENCE_REASON,
    PORTFOLIO_POLICY_EXECUTION_MISMATCH_REASON,
}
RESOURCE_INTEGRITY_REASON_CODES = {
    "candidate_resource_limit_exceeded",
    "max_runtime_exceeded",
    "max_trades_exceeded",
    "max_rss_exceeded",
}
CANDIDATE_ELIGIBILITY_REASON_CODES = {
    "research_only_not_live_eligible",
    "probe_grade_pass_not_validation_eligible",
    "exploratory_mode_not_validation_eligible",
}


def _reason_matches_any(reason: str, codes: set[str]) -> bool:
    return reason in codes or any(reason.startswith(f"{code}:") for code in codes)


def _classified_fail_reasons(
    reasons: list[Any] | tuple[Any, ...] | set[Any],
) -> dict[str, Any]:
    normalized = sorted({str(reason) for reason in reasons if str(reason)})
    simulation = [
        reason
        for reason in normalized
        if _reason_matches_any(reason, SIMULATION_INTEGRITY_REASON_CODES)
    ]
    resource = [
        reason
        for reason in normalized
        if _reason_matches_any(reason, RESOURCE_INTEGRITY_REASON_CODES)
    ]
    declaration = [
        reason
        for reason in normalized
        if _reason_matches_any(reason, CANDIDATE_ELIGIBILITY_REASON_CODES)
    ]
    classified = set(simulation) | set(resource) | set(declaration)
    performance = [reason for reason in normalized if reason not in classified]
    return {
        "simulation_integrity_status": "FAIL" if simulation else "PASS",
        "simulation_integrity_fail_reasons": simulation,
        "resource_integrity_status": "FAIL" if resource else "PASS",
        "resource_integrity_fail_reasons": resource,
        "strategy_performance_gate_status": "FAIL" if performance else "PASS",
        "strategy_performance_fail_reasons": performance,
        "candidate_eligibility_status": "FAIL" if declaration else "PASS",
        "candidate_eligibility_reasons": declaration,
    }


def _apply_fail_reason_classification(
    payload: dict[str, Any], *, reason_key: str = "gate_fail_reasons"
) -> None:
    reasons = payload.get(reason_key)
    if not isinstance(reasons, (list, tuple, set)):
        reasons = []
    classification = _classified_fail_reasons(reasons)
    if _candidate_metrics_not_evaluated(payload):
        classification["strategy_performance_gate_status"] = "NOT_EVALUATED"
        classification["strategy_performance_fail_reasons"] = []
        classification["strategy_performance_not_evaluated_reasons"] = [
            "candidate_failed_before_complete_metrics"
        ]
    payload.update(classification)


def _candidate_metrics_not_evaluated(payload: dict[str, Any]) -> bool:
    return (
        payload.get("candidate_failed_before_complete_metrics") is True
        or payload.get("metrics_v2_source") == "failure_fallback"
        or payload.get("evaluation_status") == "resource_limited"
        or payload.get("metrics_status") == "unavailable"
    )


def _parameter_perturbation_candidates(
    *,
    base_results: list[dict[str, Any]],
    pre_stress_gate_by_index: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for base in base_results:
        summary = pre_stress_gate_by_index.get(int(base["index"]), {})
        rows.append(
            {
                "candidate_id": base.get("candidate_id"),
                "parameter_values": dict(base.get("parameter_values") or {}),
                "validation_metrics": dict(base.get("validation_metrics") or {}),
                "final_holdout_metrics": dict(base.get("final_holdout_metrics") or {}),
                "scenario_acceptance_gate_result": summary.get("gate_result"),
                "scenario_fail_reasons": list(summary.get("fail_reasons") or []),
            }
        )
    return tuple(rows)


def _signal_omission_stress_runs(
    *,
    manifest: ExperimentManifest,
    snapshot: DatasetSnapshot,
    scenario: ExecutionScenario,
    scenario_id: str,
    scenario_index: int,
    candidate_id: str,
    parameter_values: dict[str, Any],
    plugin: Any,
    registry: StrategyRegistry,
    compiled_contract: Any,
) -> tuple[dict[str, Any], ...]:
    contract = (
        manifest.stress_suite.signal_omission
        if manifest.stress_suite is not None
        else None
    )
    if contract is None:
        return ()
    rows: list[dict[str, Any]] = []
    for rate in contract.omission_rates_pct:
        seed_material = {
            "manifest_hash": manifest.manifest_hash(),
            "candidate_id": candidate_id,
            "scenario_id": scenario_id,
            "split_name": snapshot.split_name,
            "signal_omission_contract_hash": sha256_prefixed(contract.as_dict()),
            "omission_rate_pct": rate,
        }
        transformer = EntrySignalOmissionTransformer(
            omission_rate_pct=rate,
            seed_material=seed_material,
        )
        execution_model = _execution_model_from_scenario(
            scenario,
            seed_context=_seed_context(
                simulation_seed_scope_hash=manifest.simulation_seed_scope_hash(),
                scenario=scenario,
                scenario_id=scenario_id,
                parameter_candidate_id=candidate_id,
                split_name=snapshot.split_name,
            ),
        )
        run = run_common_simulation_backtest(
            plugin=plugin,
            dataset=snapshot,
            parameter_values=parameter_values,
            fee_rate=scenario.fee_rate,
            slippage_bps=scenario.slippage_bps,
            execution_model=execution_model,
            execution_timing_policy=manifest.execution_timing,
            portfolio_policy=manifest.portfolio_policy,
            risk_policy=manifest.risk_policy,
            context=BacktestRunContext(
                experiment_id=manifest.experiment_id,
                candidate_id=candidate_id,
                scenario_id=f"{scenario_id}:signal_omission:{rate:g}",
                scenario_index=scenario_index,
                split_name=snapshot.split_name,
                report_detail="summary",
            ),
            compiled_contract=compiled_contract,
            registry=registry,
            decision_stream_transformer=transformer,
        )
        rows.append(
            {
                "omission_rate_pct": rate,
                "return_pct": run.metrics.return_pct,
                "metrics_hash": run.metrics_hash,
                "decision_stream_hash": run.decision_stream_hash,
                "decision_stream_perturbation_evidence": run.execution_event_summary.get(
                    "decision_stream_perturbation_evidence"
                ),
            }
        )
    return tuple(rows)


def _probe_grade_gate_warnings(manifest: ExperimentManifest) -> list[str]:
    gate = manifest.acceptance_gate
    warnings: set[str] = set()
    if (
        manifest.research_classification == "research_only"
        and gate.min_trade_count <= 5
    ):
        warnings.add("probe_grade_gate_detected")
    if gate.min_profit_factor <= 1.0:
        warnings.add("probe_grade_gate_detected")
    if not gate.metrics_contract_required:
        warnings.add("probe_grade_gate_detected")
    if not gate.walk_forward_required:
        warnings.add("probe_grade_gate_detected")
    if not gate.final_holdout_required_for_validation:
        warnings.add("probe_grade_gate_detected")
    if gate.min_cagr_pct is None or (
        gate.min_expectancy_per_trade_krw is None
        and gate.min_expectancy_per_trade_pct is None
    ):
        warnings.add("probe_grade_gate_detected")
    if warnings:
        warnings.add("probe_grade_pass_not_validation_eligible")
    return sorted(warnings)


def _failed_metrics_payload() -> dict[str, Any]:
    return {
        "return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "profit_factor": None,
        "profit_factor_unbounded": False,
        "trade_count": 0,
        "win_rate": 0.0,
        "avg_win": None,
        "avg_loss": None,
        "fee_total": 0.0,
        "slippage_total": 0.0,
        "max_consecutive_losses": 0,
        "single_trade_dependency_score": None,
        "parameter_stability_score": None,
    }


def _failed_metrics_v2_payload(
    *, evaluation_status: str = "evaluation_failed"
) -> dict[str, Any]:
    return {
        "metrics_schema_version": METRICS_SCHEMA_VERSION,
        "evaluation_status": evaluation_status,
        "metrics_status": "unavailable",
        "metrics_v2_source": "failure_fallback",
        "candidate_failed_before_complete_metrics": True,
        "return_risk": {
            "total_return_pct": 0.0,
            "cagr_pct": None,
            "max_drawdown_pct": 0.0,
            "realized_return_pct": 0.0,
            "unrealized_pnl_end": 0.0,
            "open_position_at_end": False,
        },
        "trade_quality": {
            "closed_trade_count": 0,
            "execution_count": 0,
            "win_rate": 0.0,
            "avg_win": None,
            "avg_loss": None,
            "payoff_ratio": None,
            "profit_factor": None,
            "profit_factor_unbounded": False,
            "expectancy_per_trade_krw": None,
            "expectancy_per_trade_pct": None,
            "max_consecutive_losses": 0,
            "single_trade_dependency_score": None,
        },
        "time_exposure": {
            "period_start_ts": None,
            "period_end_ts": None,
            "elapsed_ms": None,
            "calendar_days": None,
            "active_bar_count": 0,
            "exposure_time_pct": None,
            "avg_holding_time_ms": None,
            "median_holding_time_ms": None,
            "max_holding_time_ms": None,
        },
        "cost_execution": {
            "fee_total": 0.0,
            "slippage_total": 0.0,
            "fee_drag_ratio": None,
            "slippage_drag_ratio": None,
            "filled_execution_count": 0,
            "partial_fill_count": 0,
            "failed_execution_count": 0,
            "skipped_execution_count": 0,
            "quote_coverage_pct": None,
            "median_quote_age_ms": None,
            "p95_quote_age_ms": None,
            "fee_drag_ratio_basis": "traded_notional",
            "slippage_drag_ratio_basis": "traded_notional",
        },
        "limitation_reasons": [
            "candidate_failed_before_complete_metrics",
            evaluation_status,
        ],
    }


def _write_failed_candidate_evidence(
    *,
    manager: ResearchPathManager,
    manifest: ExperimentManifest,
    candidate: dict[str, Any],
    artifact_context: ResearchArtifactContext | None = None,
) -> None:
    if not manifest.research_run.artifact_policy.failed_candidate_evidence:
        return
    path = _candidate_failure_path(
        manager, manifest.experiment_id, str(candidate["candidate_id"])
    )
    candidate["failure_artifact_ref"] = _data_dir_relative_ref(manager, path)
    candidate["failure_artifact_path"] = str(path)
    candidate["failure_artifact_content_hash"] = sha256_prefixed(
        content_hash_payload(candidate), label="candidate_failure_artifact"
    )
    store = artifact_context or ResearchArtifactContext(
        manager=manager,
        experiment_id=manifest.experiment_id,
        budget=_artifact_budget_from_limits(manifest.research_run.resource_limits),
    )
    store.write_json_atomic(path, candidate)


def _apply_scenario_policy(
    *, manifest: ExperimentManifest, candidate: dict[str, Any]
) -> None:
    policy = manifest.execution_model.scenario_policy
    scenario_results = list(candidate.get("scenario_results") or [])
    diagnostic_results = [
        item for item in scenario_results if _scenario_is_diagnostic_only(item)
    ]
    required_results = [
        item for item in scenario_results if not _scenario_is_diagnostic_only(item)
    ]
    pass_results = [
        item
        for item in required_results
        if item.get("scenario_acceptance_gate_result") == "PASS"
    ]
    fail_results = [
        item
        for item in required_results
        if item.get("scenario_acceptance_gate_result") != "PASS"
    ]
    candidate["scenario_pass_count"] = len(pass_results)
    candidate["scenario_fail_count"] = len(fail_results)
    candidate["required_scenario_count"] = len(required_results)
    candidate["diagnostic_scenario_count"] = len(diagnostic_results)
    reasons: list[str] = []
    base_results = [
        item for item in required_results if item.get("scenario_role") == "base"
    ]
    primary = (
        next(
            (
                item
                for item in base_results
                if item.get("scenario_acceptance_gate_result") == "PASS"
            ),
            None,
        )
        or (pass_results[0] if pass_results else None)
        or (base_results[0] if base_results else None)
        or (required_results[0] if required_results else None)
    )
    candidate["required_scenario_ids"] = [
        str(item.get("scenario_id")) for item in required_results
    ]
    candidate["diagnostic_scenario_ids"] = [
        str(item.get("scenario_id")) for item in diagnostic_results
    ]

    if not required_results:
        reasons.append("scenario_result_missing")
    elif policy == "legacy_cost_model_single_pass":
        if not pass_results:
            for item in fail_results:
                for reason in item.get("scenario_fail_reasons") or []:
                    reasons.append(str(reason))
            reasons.append("scenario_policy_no_passing_base_scenario")
    elif policy == "single_scenario":
        if len(required_results) != 1:
            reasons.append("scenario_policy_unsupported")
        elif not pass_results:
            for reason in required_results[0].get("scenario_fail_reasons") or []:
                reasons.append(str(reason))
            reasons.append("scenario_policy_required_scenario_failed")
    elif policy == "must_pass_base_and_survive_stress":
        base_results = [
            item for item in required_results if item.get("scenario_role") == "base"
        ]
        stress_results = [
            item for item in required_results if item.get("scenario_role") == "stress"
        ]
        if not any(
            item.get("scenario_acceptance_gate_result") == "PASS"
            for item in base_results
        ):
            reasons.append("scenario_policy_no_passing_base_scenario")
        if not any(
            item.get("scenario_acceptance_gate_result") == "PASS"
            for item in stress_results
        ):
            reasons.append("scenario_policy_no_passing_stress_scenario")
        for item in fail_results:
            for reason in item.get("scenario_fail_reasons") or []:
                reasons.append(str(reason))
            reasons.append(
                "scenario_policy_required_scenario_failed:"
                f"{item.get('scenario_id')}:{','.join(str(reason) for reason in item.get('scenario_fail_reasons') or [])}"
            )
        primary = base_results[0] if base_results else primary
    else:
        reasons.append("scenario_policy_unsupported")

    candidate["_primary_scenario_result"] = primary
    candidate["acceptance_gate_result"] = "PASS" if not reasons else "FAIL"
    candidate["gate_fail_reasons"] = reasons
    _declare_candidate_scenario_semantics(
        candidate=candidate,
        primary=primary if isinstance(primary, dict) else {},
        policy=policy,
    )


def _declare_candidate_scenario_semantics(
    *,
    candidate: dict[str, Any],
    primary: dict[str, Any],
    policy: str,
) -> None:
    scenario_results = [
        item
        for item in candidate.get("scenario_results") or []
        if isinstance(item, dict)
    ]
    base = next(
        (item for item in scenario_results if item.get("scenario_role") == "base"), None
    )
    stress_results = [
        item for item in scenario_results if item.get("scenario_role") == "stress"
    ]
    aggregate_result = candidate.get("acceptance_gate_result")
    if primary:
        primary_contract = primary.get("compiled_strategy_contract")
        primary_contract_hash = primary.get("compiled_strategy_contract_hash")
        if not isinstance(primary_contract, dict) or not primary_contract_hash:
            raise ResearchValidationError("primary_scenario_compiled_contract_missing")
        validate_compiled_strategy_contract(
            dict(primary_contract), expected_compiled_hash=str(primary_contract_hash)
        )
        candidate["compiled_strategy_contract"] = primary_contract
        candidate["compiled_strategy_contract_hash"] = primary_contract_hash
        candidate["strategy_registry_hash"] = primary_contract["strategy_registry_hash"]
        candidate["strategy_plugin_contract_hash"] = primary_contract[
            "strategy_plugin_contract_hash"
        ]
        candidate["capability_contract"] = primary_contract["capability_contract"]
        candidate["capability_contract_hash"] = primary_contract[
            "capability_contract_hash"
        ]
    candidate.update(
        {
            "primary_scenario_id": primary.get("scenario_id"),
            "primary_scenario_role": primary.get("scenario_role"),
            "primary_metric_source": "primary_base_scenario_alias"
            if primary.get("scenario_role") == "base"
            else "primary_scenario_alias",
            "primary_cost_model": primary.get("cost_model"),
            "primary_validation_metrics": primary.get("validation_metrics"),
            "aggregate_gate_policy": policy,
            "aggregate_acceptance_gate_result": aggregate_result,
            "base_scenario_id": base.get("scenario_id")
            if isinstance(base, dict)
            else None,
            "base_validation_metrics": base.get("validation_metrics")
            if isinstance(base, dict)
            else None,
            "stress_scenario_ids": [item.get("scenario_id") for item in stress_results],
            "stress_gate_results": [
                {
                    "scenario_id": item.get("scenario_id"),
                    "scenario_acceptance_gate_result": item.get(
                        "scenario_acceptance_gate_result"
                    ),
                    "scenario_fail_reasons": item.get("scenario_fail_reasons") or [],
                }
                for item in stress_results
            ],
            "aggregate_gate_source": "required_scenario_policy",
            "primary_metric_source_semantics": "primary_base_scenario_alias"
            if primary.get("scenario_role") == "base"
            else "primary_scenario_alias",
            "primary_metric_scenario_role": primary.get("scenario_role"),
            "primary_metric_scenario_id": primary.get("scenario_id"),
        }
    )


def _scenario_is_diagnostic_only(scenario: dict[str, Any]) -> bool:
    if str(scenario.get("scenario_role") or "") == "diagnostic_zero_cost":
        return True
    assumption = scenario.get("cost_assumption")
    return (
        isinstance(assumption, dict)
        and assumption.get("role") == "diagnostic_zero_cost"
    )


def _cost_authority_resolution(manifest: ExperimentManifest) -> dict[str, Any]:
    execution_model = manifest.execution_model.as_dict()
    scenarios = (
        execution_model.get("scenarios")
        if isinstance(execution_model.get("scenarios"), list)
        else []
    )
    explicit_scenarios = manifest.execution_model.source != "legacy_cost_model"
    base_assumptions = [
        scenario.get("cost_assumption")
        for scenario in scenarios
        if isinstance(scenario, dict)
        and scenario.get("scenario_role") == "base"
        and isinstance(scenario.get("cost_assumption"), dict)
        and scenario["cost_assumption"].get("validation_eligible_as_base") is True
    ]
    runtime_base = dict(base_assumptions[0]) if base_assumptions else None
    source = "execution_model.scenarios" if explicit_scenarios else "legacy_cost_model"
    legacy_authority = (
        "fallback_only_not_runtime_authority"
        if explicit_scenarios
        else "runtime_base_when_no_execution_model_scenarios"
    )
    return {
        "cost_authority_source": source,
        "cost_authority_resolution": {
            "source": source,
            "runtime_authority": source,
            "legacy_cost_model_authority": legacy_authority,
            "runtime_base_cost_assumption_source": (
                "execution_model.scenarios.base.validation_eligible_as_base"
                if runtime_base is not None
                else None
            ),
        },
        "runtime_base_cost_assumption": runtime_base,
        "legacy_cost_model_present": manifest.raw.get("cost_model") is not None,
        "legacy_cost_model_authority": legacy_authority,
        "scenario_cost_assumption_contract_hash": sha256_prefixed(
            manifest.execution_model.as_dict()
        ),
    }


def _attach_candidate_diagnostic_blocks(
    *,
    candidate: dict[str, Any],
    manifest: ExperimentManifest,
    strategy_plugin: Any,
) -> None:
    candidate["research_strategy_data_requirements"] = (
        strategy_plugin.data_requirements(
            candidate.get("effective_strategy_parameters")
            or candidate.get("parameter_values")
            or {}
        ).capability_contract_payload()
    )
    candidate["cost_sensitivity"] = _cost_sensitivity_summary(
        candidate.get("scenario_results") or []
    )
    candidate["position_sizing_sensitivity"] = _position_sizing_sensitivity_summary(
        base_policy=manifest.portfolio_policy,
        candidate=candidate,
    )
    if manifest.research_run.diagnostic_mode == "exploratory":
        candidate["exploratory_result"] = {
            "diagnostic_mode": "exploratory",
            "raw_edge_summary": _raw_edge_summary(candidate),
            "cost_sensitivity": candidate["cost_sensitivity"],
            "feature_bucket_performance": candidate.get(
                "market_regime_bucket_performance"
            ),
            "regime_bucket_performance": candidate.get(
                "market_regime_bucket_performance"
            ),
            "failure_diagnostics": {
                "gate_fail_reasons": list(candidate.get("gate_fail_reasons") or []),
                "validation_strategy_diagnostics": candidate.get(
                    "validation_strategy_diagnostics"
                )
                or {},
            },
            "validation_gate_evaluated": False,
            "validation_gate_non_authoritative": True,
        }
        candidate["acceptance_gate_result"] = "FAIL"
        candidate["aggregate_acceptance_gate_result"] = "FAIL"
        candidate["acceptance_gate_status"] = "diagnostic_only"
        candidate["gate_fail_reasons"] = sorted(
            set(candidate.get("gate_fail_reasons") or [])
            | {"exploratory_mode_not_validation_eligible"}
        )


def _cost_sensitivity_summary(scenario_results: list[dict[str, Any]]) -> dict[str, Any]:
    by_role: dict[str, dict[str, Any]] = {}
    for scenario in scenario_results:
        if not isinstance(scenario, dict):
            continue
        role = _cost_sensitivity_role(scenario)
        by_role.setdefault(role, _scenario_cost_metrics(scenario))
    if "zero_cost" not in by_role:
        by_role["zero_cost"] = {
            "status": "missing",
            "synthetic": True,
            "validation_return_pct": None,
            "validation_profit_factor": None,
            "validation_trade_count": None,
            "fee_total": None,
            "slippage_total": None,
            "scenario_role": "diagnostic_zero_cost",
            "fee_rate": 0,
            "slippage_bps": 0,
            "validation_eligible_as_base": False,
            "missing_reason": "real_zero_cost_scenario_result_absent",
        }
    if "base_cost" not in by_role and scenario_results:
        by_role["base_cost"] = _scenario_cost_metrics(scenario_results[0])
    if "stress_cost" not in by_role:
        stress = next(
            (
                item
                for item in scenario_results
                if isinstance(item, dict) and item.get("scenario_role") == "stress"
            ),
            scenario_results[-1] if scenario_results else {},
        )
        by_role["stress_cost"] = _scenario_cost_metrics(
            stress if isinstance(stress, dict) else {}
        )
    zero_return = _safe_metric_float(by_role["zero_cost"].get("validation_return_pct"))
    base_return = _safe_metric_float(by_role["base_cost"].get("validation_return_pct"))
    stress_return = _safe_metric_float(
        by_role["stress_cost"].get("validation_return_pct")
    )
    fee_total = _safe_metric_float(by_role["base_cost"].get("fee_total"))
    slippage_total = _safe_metric_float(by_role["base_cost"].get("slippage_total"))
    return {
        "zero_cost": by_role["zero_cost"],
        "base_cost": by_role["base_cost"],
        "stress_cost": by_role["stress_cost"],
        "fee_drag_ratio": _drag_ratio(zero_return, base_return, fee_total),
        "slippage_drag_ratio": _drag_ratio(base_return, stress_return, slippage_total),
        "cost_breakeven_trade_edge": _cost_breakeven_trade_edge(by_role["base_cost"]),
        "validation_authority": "diagnostic_only_zero_cost_excluded_from_validation",
    }


def _cost_sensitivity_role(scenario: dict[str, Any]) -> str:
    cost = scenario.get("cost_assumption")
    if isinstance(cost, dict) and cost.get("role") == "diagnostic_zero_cost":
        return "zero_cost"
    fee = (
        float(
            scenario.get("cost_model", {}).get(
                "fee_rate", scenario.get("fee_rate", 0.0)
            )
            or 0.0
        )
        if isinstance(scenario.get("cost_model"), dict)
        else 0.0
    )
    slippage = (
        float(
            scenario.get("cost_model", {}).get(
                "slippage_bps", scenario.get("slippage_bps", 0.0)
            )
            or 0.0
        )
        if isinstance(scenario.get("cost_model"), dict)
        else 0.0
    )
    if fee == 0.0 and slippage == 0.0:
        return "zero_cost"
    return "stress_cost" if scenario.get("scenario_role") == "stress" else "base_cost"


def _scenario_cost_metrics(scenario: dict[str, Any]) -> dict[str, Any]:
    metrics = (
        scenario.get("validation_metrics_v2")
        if isinstance(scenario.get("validation_metrics_v2"), dict)
        else {}
    )
    legacy = (
        scenario.get("validation_metrics")
        if isinstance(scenario.get("validation_metrics"), dict)
        else {}
    )
    return_risk = (
        metrics.get("return_risk")
        if isinstance(metrics.get("return_risk"), dict)
        else {}
    )
    trade_quality = (
        metrics.get("trade_quality")
        if isinstance(metrics.get("trade_quality"), dict)
        else {}
    )
    cost_execution = (
        metrics.get("cost_execution")
        if isinstance(metrics.get("cost_execution"), dict)
        else {}
    )
    cost_model = (
        scenario.get("cost_model")
        if isinstance(scenario.get("cost_model"), dict)
        else {}
    )
    cost_assumption = (
        scenario.get("cost_assumption")
        if isinstance(scenario.get("cost_assumption"), dict)
        else {}
    )
    diagnostic_zero_cost = cost_assumption.get("role") == "diagnostic_zero_cost"
    return {
        "validation_return_pct": return_risk.get(
            "total_return_pct",
            legacy.get("return_pct", legacy.get("total_return_pct")),
        ),
        "validation_profit_factor": trade_quality.get(
            "profit_factor", legacy.get("profit_factor")
        ),
        "validation_trade_count": trade_quality.get(
            "closed_trade_count", legacy.get("trade_count")
        ),
        "fee_total": cost_execution.get("fee_total", legacy.get("fee_total")),
        "slippage_total": cost_execution.get(
            "slippage_total", legacy.get("slippage_total")
        ),
        "scenario_role": scenario.get("scenario_role"),
        "fee_rate": cost_model.get("fee_rate"),
        "slippage_bps": cost_model.get("slippage_bps"),
        "validation_eligible_as_base": False
        if diagnostic_zero_cost
        or scenario.get("scenario_role") == "diagnostic_zero_cost"
        else None,
    }


def _safe_metric_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _drag_ratio(
    reference: float | None, observed: float | None, cost_total: float | None
) -> float | None:
    if reference is None or observed is None:
        return 0.0 if cost_total is not None else None
    denominator = abs(reference) if abs(reference) > 1e-12 else 1.0
    return (reference - observed) / denominator


def _cost_breakeven_trade_edge(base_cost: dict[str, Any]) -> float | None:
    trades = _safe_metric_float(base_cost.get("validation_trade_count"))
    if trades is None or trades <= 0:
        return None
    fee = _safe_metric_float(base_cost.get("fee_total")) or 0.0
    slippage = _safe_metric_float(base_cost.get("slippage_total")) or 0.0
    return (fee + slippage) / trades


def _raw_edge_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    diagnostics = candidate.get("validation_strategy_diagnostics")
    metrics = candidate.get("validation_metrics_v2")
    return_risk = (
        metrics.get("return_risk")
        if isinstance(metrics, dict) and isinstance(metrics.get("return_risk"), dict)
        else {}
    )
    return {
        "validation_return_pct": return_risk.get("total_return_pct"),
        "raw_signal_count": diagnostics.get("raw_signal_count")
        if isinstance(diagnostics, dict)
        else None,
        "final_signal_count": diagnostics.get("final_signal_count")
        if isinstance(diagnostics, dict)
        else None,
    }


def _position_sizing_sensitivity_summary(
    *,
    base_policy: Any,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    starting_cash = float(base_policy.starting_cash_krw)
    base_fraction = float(base_policy.position_sizing.buy_fraction)
    trades = _position_sizing_replay_trades(candidate)
    results: dict[str, dict[str, Any]] = {}
    for fraction in (0.99, 0.50, 0.25, 0.10):
        policy_payload = base_policy.as_dict()
        policy_payload["position_sizing"] = dict(policy_payload["position_sizing"])
        policy_payload["position_sizing"]["buy_fraction"] = fraction
        replay = _simulate_position_sizing_fraction(
            trades=trades,
            starting_cash=starting_cash,
            buy_fraction=fraction,
            base_buy_fraction=base_fraction,
        )
        replay.update(
            {
                "portfolio_policy_hash": sha256_prefixed(policy_payload),
                "starting_cash_krw": starting_cash,
                "buy_fraction": fraction,
                "diagnostic_only": True,
                "simulation_method": "independent_closed_trade_portfolio_replay",
            }
        )
        results[f"{fraction:.2f}"] = replay
    return {
        "by_buy_fraction": results,
        "validation_authority": "diagnostic_only_excluded_from_validation",
        "primary_metrics_overridden": False,
        "status": "available" if trades else "missing",
        "direct_linear_scaling_used": False,
        **(
            {}
            if trades
            else {"missing_reason": "validation_closed_trade_replay_inputs_missing"}
        ),
    }


def _position_sizing_replay_trades(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    trades = candidate.get("validation_closed_trades")
    if not isinstance(trades, list):
        for scenario in candidate.get("scenario_results") or []:
            if (
                not isinstance(scenario, dict)
                or scenario.get("scenario_role") != "base"
            ):
                continue
            scenario_trades = scenario.get("validation_closed_trades")
            if isinstance(scenario_trades, list):
                trades = scenario_trades
                break
    return (
        [dict(trade) for trade in trades if isinstance(trade, dict)]
        if isinstance(trades, list)
        else []
    )


def _simulate_position_sizing_fraction(
    *,
    trades: list[dict[str, Any]],
    starting_cash: float,
    buy_fraction: float,
    base_buy_fraction: float,
) -> dict[str, Any]:
    if not trades:
        return {
            "status": "missing",
            "validation_return_pct": None,
            "validation_max_drawdown_pct": None,
            "validation_profit_factor": None,
            "validation_trade_count": 0,
            "missing_reason": "validation_closed_trade_replay_inputs_missing",
        }
    cash = float(starting_cash)
    peak = cash
    max_drawdown_pct = 0.0
    wins = 0.0
    losses = 0.0
    applied = 0
    for trade in sorted(
        trades, key=lambda item: int(item.get("exit_ts") or item.get("entry_ts") or 0)
    ):
        trade_return_pct = _closed_trade_return_pct(
            trade,
            starting_cash=starting_cash,
            base_buy_fraction=base_buy_fraction,
        )
        if trade_return_pct is None:
            continue
        pnl = max(0.0, cash * float(buy_fraction)) * (trade_return_pct / 100.0)
        cash += pnl
        applied += 1
        if pnl > 0.0:
            wins += pnl
        elif pnl < 0.0:
            losses += abs(pnl)
        peak = max(peak, cash)
        if peak > 0.0:
            max_drawdown_pct = max(max_drawdown_pct, ((peak - cash) / peak) * 100.0)
    profit_factor = None
    if losses > 0.0:
        profit_factor = wins / losses
    elif wins > 0.0:
        profit_factor = 1_000_000_000_000.0
    return {
        "status": "available" if applied else "missing",
        "validation_return_pct": ((cash / starting_cash) - 1.0) * 100.0
        if starting_cash > 0.0 and applied
        else None,
        "validation_max_drawdown_pct": max_drawdown_pct if applied else None,
        "validation_profit_factor": profit_factor if applied else None,
        "validation_profit_factor_unbounded": bool(
            applied and losses == 0.0 and wins > 0.0
        ),
        "validation_trade_count": applied,
    }


def _closed_trade_return_pct(
    trade: dict[str, Any],
    *,
    starting_cash: float,
    base_buy_fraction: float,
) -> float | None:
    value = _safe_metric_float(trade.get("return_pct"))
    if value is not None:
        return value
    net_pnl = _safe_metric_float(trade.get("net_pnl"))
    if net_pnl is None:
        return None
    entry_notional = _safe_metric_float(trade.get("entry_notional"))
    if entry_notional is not None and entry_notional > 0.0:
        return (net_pnl / entry_notional) * 100.0
    baseline_notional = float(starting_cash) * max(float(base_buy_fraction), 1e-12)
    if baseline_notional <= 0.0:
        return None
    return (net_pnl / baseline_notional) * 100.0


def _combined_calibration_gate(
    scenario_results: list[dict[str, Any]],
) -> dict[str, Any]:
    gates = [
        item.get("execution_calibration_gate")
        for item in scenario_results
        if isinstance(item.get("execution_calibration_gate"), dict)
    ]
    reasons = sorted(
        {str(reason) for gate in gates for reason in gate.get("reasons") or []}
    )
    statuses = {str(gate.get("status")) for gate in gates}
    hashes = sorted(
        {
            str(gate.get("artifact_hash"))
            for gate in gates
            if isinstance(gate.get("artifact_hash"), str)
            and str(gate.get("artifact_hash")).startswith("sha256:")
        }
    )
    status = "PASS"
    if "FAIL" in statuses:
        status = "FAIL"
    elif "MISSING" in statuses:
        status = "MISSING"
    payload: dict[str, Any] = {
        "status": status,
        "reasons": reasons,
        "scenario_gates": gates,
    }
    if len(hashes) == 1:
        payload["artifact_hash"] = hashes[0]
    if hashes:
        payload["artifact_hashes"] = hashes
    return payload


def _gate_result(
    *,
    manifest: ExperimentManifest,
    validation_metrics: dict[str, Any],
    walk_forward_metrics: dict[str, Any] | None,
    stability_score: float | None,
    include_walk_forward: bool,
    validation_metrics_v2: dict[str, Any] | None = None,
    regime_gate_result: dict[str, Any] | None = None,
    execution_calibration_gate: dict[str, Any] | None = None,
    dataset_quality_status: str = "PASS",
    dataset_quality_reasons: list[str] | None = None,
) -> tuple[str, list[str]]:
    gate = manifest.acceptance_gate
    reasons: list[str] = []
    if dataset_quality_status != "PASS":
        reasons.extend(dataset_quality_reasons or ["dataset_quality_failed"])
    if int(validation_metrics.get("trade_count") or 0) < gate.min_trade_count:
        reasons.append("min_trade_count_failed")
    if float(validation_metrics.get("max_drawdown_pct") or 0.0) > gate.max_mdd_pct:
        reasons.append("max_drawdown_failed")
    profit_factor = validation_metrics.get("profit_factor")
    if not _profit_factor_passes(
        profit_factor,
        validation_metrics.get("profit_factor_unbounded"),
        gate.min_profit_factor,
    ):
        reasons.append("profit_factor_failed")
    if (
        gate.oos_return_must_be_positive
        and float(validation_metrics.get("return_pct") or 0.0) <= 0.0
    ):
        reasons.append("validation_return_not_positive")
    reasons.extend(
        _metrics_v2_gate_reasons(gate=gate, metrics_v2=validation_metrics_v2, prefix="")
    )
    if gate.parameter_stability_required and (
        stability_score is None or stability_score < 0.5
    ):
        reasons.append("parameter_stability_failed")
    if gate.walk_forward_required:
        if not include_walk_forward or not walk_forward_metrics:
            reasons.append("walk_forward_missing")
        elif not bool(walk_forward_metrics.get("return_consistency_pass")):
            reasons.append("walk_forward_failed")
    if gate.regime_acceptance_gate.required:
        if not isinstance(regime_gate_result, dict):
            reasons.append("regime_gate_missing")
        elif regime_gate_result.get("result") != "PASS":
            reasons.extend(
                str(reason)
                for reason in regime_gate_result.get("reasons")
                or ["regime_gate_failed"]
            )
    if manifest.execution_model.calibration_required:
        if not isinstance(execution_calibration_gate, dict):
            reasons.append("execution_calibration_missing")
        elif execution_calibration_gate.get("status") != "PASS":
            reasons.extend(
                str(reason)
                for reason in execution_calibration_gate.get("reasons")
                or ["execution_calibration_failed"]
            )
    elif (
        manifest.execution_model.calibration_strictness == "fail"
        and isinstance(execution_calibration_gate, dict)
        and execution_calibration_gate.get("status") == "FAIL"
    ):
        reasons.extend(
            str(reason)
            for reason in execution_calibration_gate.get("reasons")
            or ["execution_calibration_failed"]
        )
    return ("PASS" if not reasons else "FAIL", reasons)


def _metrics_v2_gate_reasons(
    *, gate, metrics_v2: dict[str, Any] | None, prefix: str
) -> list[str]:
    has_v2_gate = (
        any(
            value is not None
            for value in (
                gate.min_cagr_pct,
                gate.min_expectancy_per_trade_krw,
                gate.min_expectancy_per_trade_pct,
                gate.max_exposure_time_pct,
                gate.max_avg_holding_time_minutes,
                gate.max_fee_drag_ratio,
                gate.max_slippage_drag_ratio,
                gate.max_single_trade_dependency_score,
                gate.min_trade_days_pct,
                gate.max_zero_filled_days,
                gate.max_consecutive_zero_filled_days,
                gate.min_filled_execution_per_kst_day,
            )
        )
        or gate.reject_open_position_at_end
        or gate.metrics_contract_required
    )
    if not has_v2_gate:
        return []
    if not isinstance(metrics_v2, dict):
        return [f"{prefix}metrics_v2_missing" if prefix else "metrics_v2_missing"]
    if int(metrics_v2.get("metrics_schema_version") or 0) != METRICS_SCHEMA_VERSION:
        return [
            f"{prefix}metrics_contract_missing"
            if prefix
            else "metrics_contract_missing"
        ]
    if (
        metrics_v2.get("metrics_status") == "unavailable"
        or metrics_v2.get("metrics_v2_source") == "failure_fallback"
    ):
        return [
            f"{prefix}metrics_v2_unavailable" if prefix else "metrics_v2_unavailable"
        ]
    return_risk = (
        metrics_v2.get("return_risk")
        if isinstance(metrics_v2.get("return_risk"), dict)
        else {}
    )
    trade_quality = (
        metrics_v2.get("trade_quality")
        if isinstance(metrics_v2.get("trade_quality"), dict)
        else {}
    )
    time_exposure = (
        metrics_v2.get("time_exposure")
        if isinstance(metrics_v2.get("time_exposure"), dict)
        else {}
    )
    cost_execution = (
        metrics_v2.get("cost_execution")
        if isinstance(metrics_v2.get("cost_execution"), dict)
        else {}
    )
    reasons: list[str] = []
    _append_min_reason(
        reasons,
        value=return_risk.get("cagr_pct"),
        threshold=gate.min_cagr_pct,
        missing_code=f"{prefix}metrics_v2_required_field_missing",
        failed_code=f"{prefix}min_cagr_failed",
    )
    _append_min_reason(
        reasons,
        value=trade_quality.get("expectancy_per_trade_krw"),
        threshold=gate.min_expectancy_per_trade_krw,
        missing_code=f"{prefix}metrics_v2_required_field_missing",
        failed_code=f"{prefix}min_expectancy_per_trade_krw_failed",
    )
    _append_min_reason(
        reasons,
        value=trade_quality.get("expectancy_per_trade_pct"),
        threshold=gate.min_expectancy_per_trade_pct,
        missing_code=f"{prefix}metrics_v2_required_field_missing",
        failed_code=f"{prefix}min_expectancy_per_trade_pct_failed",
    )
    _append_max_reason(
        reasons,
        value=time_exposure.get("exposure_time_pct"),
        threshold=gate.max_exposure_time_pct,
        missing_code=f"{prefix}metrics_v2_required_field_missing",
        failed_code=f"{prefix}max_exposure_time_failed",
    )
    avg_holding_ms = time_exposure.get("avg_holding_time_ms")
    avg_holding_minutes = (
        (float(avg_holding_ms) / 60_000.0) if avg_holding_ms is not None else None
    )
    _append_max_reason(
        reasons,
        value=avg_holding_minutes,
        threshold=gate.max_avg_holding_time_minutes,
        missing_code=f"{prefix}metrics_v2_required_field_missing",
        failed_code=f"{prefix}max_avg_holding_time_failed",
    )
    _append_max_reason(
        reasons,
        value=cost_execution.get("fee_drag_ratio"),
        threshold=gate.max_fee_drag_ratio,
        missing_code=f"{prefix}metrics_v2_required_field_missing",
        failed_code=f"{prefix}max_fee_drag_ratio_failed",
    )
    _append_max_reason(
        reasons,
        value=cost_execution.get("slippage_drag_ratio"),
        threshold=gate.max_slippage_drag_ratio,
        missing_code=f"{prefix}metrics_v2_required_field_missing",
        failed_code=f"{prefix}max_slippage_drag_ratio_failed",
    )
    _append_max_reason(
        reasons,
        value=trade_quality.get("single_trade_dependency_score"),
        threshold=gate.max_single_trade_dependency_score,
        missing_code=f"{prefix}metrics_v2_required_field_missing",
        failed_code=f"{prefix}max_single_trade_dependency_score_failed",
    )
    if gate.reject_open_position_at_end and bool(
        return_risk.get("open_position_at_end")
    ):
        reasons.append(f"{prefix}open_position_at_end_failed")
    participation = (
        metrics_v2.get("participation")
        if isinstance(metrics_v2.get("participation"), dict)
        else {}
    )
    if any(
        value is not None
        for value in (
            gate.min_trade_days_pct,
            gate.max_zero_filled_days,
            gate.max_consecutive_zero_filled_days,
            gate.min_filled_execution_per_kst_day,
        )
    ):
        if not participation:
            reasons.append(f"{prefix}daily_participation_metrics_missing")
        else:
            configured_basis = getattr(gate, "participation_count_basis", None)
            if (
                configured_basis is not None
                and participation.get("count_basis") != configured_basis
            ):
                reasons.append(f"{prefix}daily_participation_count_basis_mismatch")
            calendar_day_count = int(participation.get("calendar_day_count") or 0)
            days_with_filled = int(participation.get("days_with_filled_execution") or 0)
            trade_days_pct = (
                days_with_filled / float(calendar_day_count) * 100.0
                if calendar_day_count > 0
                else None
            )
            _append_min_reason(
                reasons,
                value=trade_days_pct,
                threshold=gate.min_trade_days_pct,
                missing_code=f"{prefix}daily_participation_trade_days_pct_missing",
                failed_code=f"{prefix}daily_participation_min_trade_days_pct_failed",
            )
            _append_max_reason(
                reasons,
                value=participation.get("zero_filled_days"),
                threshold=gate.max_zero_filled_days,
                missing_code=f"{prefix}daily_participation_zero_filled_days_missing",
                failed_code=f"{prefix}daily_participation_max_zero_filled_days_failed",
            )
            _append_max_reason(
                reasons,
                value=participation.get("max_consecutive_zero_filled_days"),
                threshold=gate.max_consecutive_zero_filled_days,
                missing_code=f"{prefix}daily_participation_consecutive_zero_filled_days_missing",
                failed_code=f"{prefix}daily_participation_max_consecutive_zero_filled_days_failed",
            )
            _append_min_reason(
                reasons,
                value=participation.get("min_daily_filled_execution_count"),
                threshold=gate.min_filled_execution_per_kst_day,
                missing_code=f"{prefix}daily_participation_min_daily_filled_count_missing",
                failed_code=f"{prefix}daily_participation_min_filled_execution_per_kst_day_failed",
            )
    return reasons


def _append_min_reason(
    reasons: list[str],
    *,
    value: Any,
    threshold: float | None,
    missing_code: str,
    failed_code: str,
) -> None:
    if threshold is None:
        return
    if value is None:
        reasons.append(missing_code)
        return
    if float(value) < float(threshold):
        reasons.append(failed_code)


def _append_max_reason(
    reasons: list[str],
    *,
    value: Any,
    threshold: float | None,
    missing_code: str,
    failed_code: str,
) -> None:
    if threshold is None:
        return
    if value is None:
        reasons.append(missing_code)
        return
    if float(value) > float(threshold):
        reasons.append(failed_code)


def _parameter_stability_scores(
    *,
    manifest: ExperimentManifest,
    candidates: list[dict[str, Any]],
    evaluated_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, params in enumerate(candidates):
        neighbors = _neighbor_indices(manifest.parameter_space, candidates, params)
        acceptable = [
            neighbor_index
            for neighbor_index in neighbors
            if _validation_metrics_gate_compatible(
                manifest, evaluated_candidates[neighbor_index]["validation_metrics"]
            )
        ]
        score = (len(acceptable) / len(neighbors)) if neighbors else None
        out.append(
            {
                "score": score,
                "neighbor_count": len(neighbors),
                "acceptable_neighbor_count": len(acceptable),
                "neighbor_candidate_ids": [
                    evaluated_candidates[item]["candidate_id"] for item in neighbors
                ],
                "acceptable_neighbor_candidate_ids": [
                    evaluated_candidates[item]["candidate_id"] for item in acceptable
                ],
                "method": "one_parameter_grid_step_validation_gate_compatible_neighbors",
            }
        )
    return out


def _neighbor_indices(
    parameter_space: dict[str, tuple[object, ...]],
    candidates: list[dict[str, Any]],
    params: dict[str, Any],
) -> list[int]:
    value_positions = {
        key: {value: position for position, value in enumerate(values)}
        for key, values in parameter_space.items()
    }
    neighbors: list[int] = []
    for index, other in enumerate(candidates):
        differing_steps = 0
        comparable = True
        for key in sorted(parameter_space):
            if other.get(key) == params.get(key):
                continue
            left = value_positions[key].get(params.get(key))
            right = value_positions[key].get(other.get(key))
            if left is None or right is None or abs(left - right) != 1:
                comparable = False
                break
            differing_steps += 1
        if comparable and differing_steps == 1:
            neighbors.append(index)
    return neighbors


def _validation_metrics_gate_compatible(
    manifest: ExperimentManifest, metrics: dict[str, Any]
) -> bool:
    gate = manifest.acceptance_gate
    if int(metrics.get("trade_count") or 0) < gate.min_trade_count:
        return False
    if float(metrics.get("max_drawdown_pct") or 0.0) > gate.max_mdd_pct:
        return False
    profit_factor = metrics.get("profit_factor")
    if not _profit_factor_passes(
        profit_factor, metrics.get("profit_factor_unbounded"), gate.min_profit_factor
    ):
        return False
    if (
        gate.oos_return_must_be_positive
        and float(metrics.get("return_pct") or 0.0) <= 0.0
    ):
        return False
    return True


def _profit_factor_passes(value: Any, unbounded: Any, minimum: float) -> bool:
    if unbounded is True:
        return True
    if value is None:
        return False
    return float(value) >= float(minimum)


def _walk_forward_metrics(
    *,
    manifest: ExperimentManifest,
    snapshots: dict[str, DatasetSnapshot],
    parameter_values: dict[str, Any],
    fee_rate: float,
    scenario: ExecutionScenario | None = None,
    scenario_id: str | None = None,
    scenario_index: int = 0,
    manager: ResearchPathManager | None = None,
    slippage_bps: float | None = None,
    parameter_candidate_id: str | None = None,
    parameter_stability_score: float | None = None,
    progress_callback: ProgressCallback | None = None,
    artifact_context: ResearchArtifactContext | None = None,
    plugin: Any,
    compiled_contract: Any,
) -> dict[str, Any]:
    config = manifest.walk_forward
    if config is None:
        return {
            "window_count": 0,
            "pass_window_count": 0,
            "fail_window_count": 0,
            "return_consistency_pass": False,
            "failure_reason": "walk_forward_missing",
            "windows": [],
        }
    active_scenario = scenario or ExecutionScenario(
        type="fixed_bps",
        fee_rate=float(fee_rate),
        slippage_bps=float(slippage_bps or 0.0),
        source="legacy_test_call",
    )
    windows: list[dict[str, Any]] = []
    active_scenario_id = scenario_id or _scenario_id(active_scenario, scenario_index)

    def _run_window(
        snapshot: DatasetSnapshot, split_name: str, context: BacktestRunContext | None
    ) -> BacktestRun:
        execution_model = _execution_model_from_scenario(
            active_scenario,
            seed_context=_seed_context(
                simulation_seed_scope_hash=manifest.simulation_seed_scope_hash(),
                scenario=active_scenario,
                scenario_id=active_scenario_id,
                parameter_candidate_id=parameter_candidate_id or "unknown_candidate",
                split_name=split_name,
            ),
        )
        if context is None:
            return run_common_simulation_backtest(
                plugin=plugin,
                dataset=snapshot,
                parameter_values=parameter_values,
                fee_rate=active_scenario.fee_rate,
                slippage_bps=active_scenario.slippage_bps,
                parameter_stability_score=parameter_stability_score,
                execution_model=execution_model,
                execution_timing_policy=manifest.execution_timing,
                portfolio_policy=manifest.portfolio_policy,
                risk_policy=manifest.risk_policy,
                context=None,
                compiled_contract=compiled_contract,
            )
        return run_common_simulation_backtest(
            plugin=plugin,
            dataset=snapshot,
            parameter_values=parameter_values,
            fee_rate=active_scenario.fee_rate,
            slippage_bps=active_scenario.slippage_bps,
            parameter_stability_score=parameter_stability_score,
            execution_model=execution_model,
            execution_timing_policy=manifest.execution_timing,
            portfolio_policy=manifest.portfolio_policy,
            risk_policy=manifest.risk_policy,
            context=context,
            compiled_contract=compiled_contract,
        )

    for window_id in sorted(
        {key.rsplit("_", 1)[0] for key in snapshots if key.startswith("window_")}
    ):
        train_snapshot = snapshots[f"{window_id}_train"]
        test_snapshot = snapshots[f"{window_id}_test"]
        train_context = (
            _backtest_context(
                manifest=manifest,
                manager=manager,
                candidate_id=parameter_candidate_id or "unknown_candidate",
                scenario_id=active_scenario_id,
                scenario_index=scenario_index,
                split_name=f"{window_id}_train",
                dataset_content_hash=train_snapshot.snapshot_fingerprint_hash(),
                parameter_values=parameter_values,
                progress_callback=progress_callback,
                artifact_context=artifact_context,
            )
            if manager is not None
            else None
        )
        train = _run_window(train_snapshot, f"{window_id}_train", train_context)
        test_context = (
            _backtest_context(
                manifest=manifest,
                manager=manager,
                candidate_id=parameter_candidate_id or "unknown_candidate",
                scenario_id=active_scenario_id,
                scenario_index=scenario_index,
                split_name=f"{window_id}_test",
                dataset_content_hash=test_snapshot.snapshot_fingerprint_hash(),
                parameter_values=parameter_values,
                progress_callback=progress_callback,
                artifact_context=artifact_context,
            )
            if manager is not None
            else None
        )
        test = _run_window(test_snapshot, f"{window_id}_test", test_context)
        test_metrics = test.metrics.as_dict()
        pass_reasons: list[str] = []
        if not _validation_metrics_gate_compatible(manifest, test_metrics):
            pass_reasons.append("test_metrics_gate_incompatible")
        if (
            manifest.acceptance_gate.oos_return_must_be_positive
            and float(test_metrics.get("return_pct") or 0.0) <= 0.0
        ):
            pass_reasons.append("test_return_not_positive")
        windows.append(
            {
                "window_id": window_id,
                "train_date_range": train_snapshot.date_range.as_dict(),
                "test_date_range": test_snapshot.date_range.as_dict(),
                "train_candle_count": len(train_snapshot.candles),
                "test_candle_count": len(test_snapshot.candles),
                "train_metrics": train.metrics.as_dict(),
                "test_metrics": test_metrics,
                "train_metrics_v2": _metrics_v2_payload(train),
                "test_metrics_v2": _metrics_v2_payload(test),
                "train_audit_trace_index": train.audit_trace_index,
                "test_audit_trace_index": test.audit_trace_index,
                "train_market_regime_coverage": [
                    row.as_dict() for row in train.regime_coverage
                ],
                "test_market_regime_coverage": [
                    row.as_dict() for row in test.regime_coverage
                ],
                "test_market_regime_bucket_performance": [
                    row.as_dict() for row in test.regime_performance
                ],
                "trade_count_by_regime": {
                    str(row.regime): int(row.trade_count)
                    for row in test.regime_coverage
                    if row.dimension == "composite_regime"
                },
                "candle_count_by_regime": {
                    str(row.regime): int(row.candle_count)
                    for row in test.regime_coverage
                    if row.dimension == "composite_regime"
                },
                "worst_regime_profit_factor": _worst_regime_metric(
                    test.regime_performance, "profit_factor"
                ),
                "worst_regime_net_pnl": _worst_regime_metric(
                    test.regime_performance, "net_pnl"
                ),
                "gate_result": "PASS" if not pass_reasons else "FAIL",
                "fail_reasons": pass_reasons,
            }
        )
    test_returns = [
        float(window["test_metrics"].get("return_pct") or 0.0) for window in windows
    ]
    pass_count = sum(1 for window in windows if window["gate_result"] == "PASS")
    failure_reason = None
    if len(windows) < config.min_windows:
        failure_reason = "walk_forward_insufficient_windows"
    elif pass_count != len(windows):
        failure_reason = "walk_forward_failed"
    return {
        "window_count": len(windows),
        "pass_window_count": pass_count,
        "fail_window_count": len(windows) - pass_count,
        "mean_test_return_pct": (sum(test_returns) / len(test_returns))
        if test_returns
        else None,
        "median_test_return_pct": median(test_returns) if test_returns else None,
        "worst_test_return_pct": min(test_returns) if test_returns else None,
        "return_consistency_pass": failure_reason is None,
        "failure_reason": failure_reason,
        "windows": windows,
    }


def _worst_regime_metric(rows: Any, key: str) -> float | None:
    values = [
        getattr(row, key)
        for row in rows
        if getattr(row, "dimension", "") == "composite_regime"
        and getattr(row, key) is not None
    ]
    return min(float(value) for value in values) if values else None


def _metrics_v2_payload(run: BacktestRun | None) -> dict[str, Any] | None:
    if run is None or run.metrics_v2 is None:
        return None
    payload = run.metrics_v2.as_dict()
    payload.update(
        {
            "evaluation_status": "completed",
            "metrics_status": "complete",
            "metrics_v2_source": "computed",
            "candidate_failed_before_complete_metrics": False,
        }
    )
    return payload


def _report_payload(
    *,
    manifest: ExperimentManifest,
    snapshots: tuple[DatasetSnapshot, ...],
    quality_reports: tuple[DatasetQualityReport, ...],
    candidates: list[dict[str, Any]],
    report_kind: str,
    generated_at: str | None,
    manifest_path: str | None = None,
    command_name: str | None = None,
    command_args: dict[str, Any] | None = None,
    execution_calibration: dict[str, Any] | None = None,
    manager: ResearchPathManager | None = None,
    experiment_registry_reservation: dict[str, Any] | None = None,
    execution_plan: ResearchExecutionPlan | None = None,
    execution_observability: dict[str, Any] | None = None,
    artifact_context: ResearchArtifactContext | None = None,
    strategy_registry: StrategyRegistry,
    governance_authority_manager: ResearchPathManager | None = None,
) -> dict[str, Any]:
    dataset_hash = combined_dataset_fingerprint(snapshots)
    dataset_quality_hash = combined_dataset_quality_hash(quality_reports)
    portfolio_policy = manifest.portfolio_policy.as_dict()
    portfolio_policy_hash = manifest.portfolio_policy_hash()
    simulation_policy_hash = manifest.simulation_policy_hash()
    split_hashes = {
        snapshot.split_name: snapshot.snapshot_fingerprint_hash()
        for snapshot in snapshots
    }
    final_holdout_hashes = (
        final_holdout_hashes_from_manifest(
            manifest=manifest,
            final_holdout_split_hash=split_hashes.get("final_holdout"),
            dataset_quality_hash=dataset_quality_hash,
            dataset_artifact={
                "artifact_id": next(
                    (
                        snapshot.artifact_id
                        for snapshot in snapshots
                        if snapshot.artifact_id
                    ),
                    None,
                ),
                "artifact_manifest_hash": next(
                    (
                        snapshot.artifact_manifest_hash
                        for snapshot in snapshots
                        if snapshot.artifact_manifest_hash
                    ),
                    None,
                ),
                "artifact_content_hash": next(
                    (
                        snapshot.artifact_content_hash
                        for snapshot in snapshots
                        if snapshot.artifact_content_hash
                    ),
                    None,
                ),
                "artifact_schema_hash": next(
                    (
                        snapshot.artifact_schema_hash
                        for snapshot in snapshots
                        if snapshot.artifact_schema_hash
                    ),
                    None,
                ),
                "verification_status": next(
                    (
                        snapshot.verification.overall_status.value
                        for snapshot in snapshots
                        if snapshot.verification
                    ),
                    "UNAVAILABLE",
                ),
            },
            final_holdout_evidence=(
                {
                    "requested_range": next(
                        snapshot.date_range.as_dict()
                        for snapshot in snapshots
                        if snapshot.split_name == "final_holdout"
                    ),
                    "snapshot_query_hash": next(
                        snapshot.snapshot_query_hash()
                        for snapshot in snapshots
                        if snapshot.split_name == "final_holdout"
                    ),
                    "snapshot_data_hash": next(
                        snapshot.snapshot_data_hash()
                        for snapshot in snapshots
                        if snapshot.split_name == "final_holdout"
                    ),
                    "snapshot_fingerprint_hash": next(
                        snapshot.snapshot_fingerprint_hash()
                        for snapshot in snapshots
                        if snapshot.split_name == "final_holdout"
                    ),
                    "quality_hash": next(
                        report.content_hash
                        for report in quality_reports
                        if report.payload["split_name"] == "final_holdout"
                    ),
                }
                if any(snapshot.split_name == "final_holdout" for snapshot in snapshots)
                else {}
            ),
        )
        if manifest.dataset.split.final_holdout is not None
        and split_hashes.get("final_holdout") is not None
        else {}
    )
    dataset_quality_status, dataset_quality_reasons = _combined_dataset_quality_gate(
        {report.payload["split_name"]: report for report in quality_reports}
    )
    dataset_adapter_provenance = _dataset_adapter_provenance_payload(
        manifest=manifest,
        snapshots=snapshots,
        quality_reports=quality_reports,
    )
    dataset_adapter_provenance_hash = sha256_prefixed(dataset_adapter_provenance)
    dataset_artifact = {
        "artifact_ids": sorted(
            {
                snapshot.artifact_id
                for snapshot in snapshots
                if snapshot.artifact_id is not None
            }
        ),
        "artifact_content_hashes": sorted(
            {
                snapshot.artifact_content_hash
                for snapshot in snapshots
                if snapshot.artifact_content_hash is not None
            }
        ),
        "artifact_schema_hashes": sorted(
            {
                snapshot.artifact_schema_hash
                for snapshot in snapshots
                if snapshot.artifact_schema_hash is not None
            }
        ),
        "artifact_manifest_hashes": sorted(
            {
                snapshot.artifact_manifest_hash
                for snapshot in snapshots
                if snapshot.artifact_manifest_hash is not None
            }
        ),
    }
    for plural, singular in (
        ("artifact_ids", "artifact_id"),
        ("artifact_content_hashes", "artifact_content_hash"),
        ("artifact_schema_hashes", "artifact_schema_hash"),
        ("artifact_manifest_hashes", "artifact_manifest_hash"),
    ):
        values = dataset_artifact[plural]
        dataset_artifact[singular] = values[0] if len(values) == 1 else None
    verification_evidence = [
        snapshot.verification.as_dict()
        for snapshot in snapshots
        if snapshot.verification
    ]
    dataset_artifact["verification_statuses"] = sorted(
        {item["overall_status"] for item in verification_evidence}
    )
    dataset_artifact["verification_status"] = (
        dataset_artifact["verification_statuses"][0]
        if len(dataset_artifact["verification_statuses"]) == 1
        else None
    )
    dataset_artifact["verification"] = (
        verification_evidence[0]
        if len(verification_evidence) == 1
        else verification_evidence
    )
    top_of_book_quality_summary = _top_of_book_quality_summary(
        {str(report.payload["split_name"]): report for report in quality_reports}
    )
    top_of_book_requested = manifest.dataset.top_of_book is not None
    top_of_book_joined_count = sum(
        int(report.payload.get("top_of_book_joined_count") or 0)
        for report in quality_reports
    )
    l2_depth_rows_available = any(
        bool(report.payload.get("l2_depth_rows_available"))
        for report in quality_reports
    )
    l2_depth_complete_snapshots_available = bool(
        top_of_book_quality_summary.get("l2_depth_complete_snapshots_available")
    )
    repository_version = _repository_version()
    calibration_hash = (
        str(execution_calibration.get("content_hash"))
        if isinstance(execution_calibration, dict)
        and execution_calibration.get("content_hash")
        else None
    )
    report_execution_contract = _execution_reality_contract(
        manifest=manifest,
        scenario=_base_report_scenario(manifest),
        calibration_hash=calibration_hash,
        top_of_book_available=top_of_book_joined_count > 0,
        depth_available=l2_depth_complete_snapshots_available,
    )
    report_capability_contract = _execution_capability_contract_from_reality(
        report_execution_contract
    )
    parameter_grid_size = _parameter_grid_size(manifest)
    failed_count = sum(
        1
        for candidate in candidates
        if candidate.get("acceptance_gate_result") != "PASS"
    )
    declared_attempt_index = _optional_int(manifest.raw.get("attempt_index"))
    declared_holdout_reuse_count = _optional_int(
        manifest.raw.get("holdout_reuse_count")
    )
    attempt_index = int(
        (experiment_registry_reservation or {}).get("computed_attempt_index")
        or declared_attempt_index
        or 1
    )
    holdout_reuse_count = int(
        (experiment_registry_reservation or {}).get("computed_holdout_reuse_count")
        or declared_holdout_reuse_count
        or 0
    )
    dataset_reuse_policy = str(
        manifest.raw.get("dataset_reuse_policy")
        or "single_final_holdout_for_experiment_family"
    )
    identity = research_identity_from_manifest(manifest)
    experiment_family_id = str(identity["experiment_family_id"])
    hypothesis_id = str(identity["hypothesis_id"])
    hypothesis_status = str(identity["hypothesis_status"])
    registry_row = (
        experiment_registry_reservation.get("row")
        if isinstance(experiment_registry_reservation, dict)
        and isinstance(experiment_registry_reservation.get("row"), dict)
        else {}
    )
    experiment_registry_fields: dict[str, Any] = {}
    if experiment_registry_reservation is not None:
        content_pending = bool(
            registry_row.get("final_holdout_content_pending_until_completion")
        )
        experiment_registry_fields = {
            "experiment_registry_path": experiment_registry_reservation.get("path"),
            "experiment_registry_prior_hash": experiment_registry_reservation.get(
                "prior_hash"
            ),
            "experiment_registry_row_hash": experiment_registry_reservation.get(
                "row_hash"
            ),
            "experiment_registry_completion_row_hash": None,
            "final_holdout_fingerprint": final_holdout_hashes.get(
                "final_holdout_fingerprint"
            ),
            "final_holdout_identity_hash": final_holdout_hashes.get(
                "final_holdout_identity_hash"
            ),
            "final_holdout_content_hash": final_holdout_hashes.get(
                "final_holdout_content_hash"
            ),
            "final_holdout_reuse_key_hash_v1": registry_row.get(
                "final_holdout_reuse_key_hash_v1"
            ),
            "final_holdout_reuse_key_hash": final_holdout_hashes.get(
                "final_holdout_reuse_key_hash"
            ),
            "final_holdout_reuse_key_schema_version": final_holdout_hashes.get(
                "final_holdout_reuse_key_schema_version"
            ),
            "final_holdout_reuse_key_hash_v2": final_holdout_hashes.get(
                "final_holdout_reuse_key_hash_v2"
            ),
            "dataset_artifact_evidence_hash": final_holdout_hashes.get(
                "dataset_artifact_evidence_hash"
            ),
            "final_holdout_query_hash": final_holdout_hashes.get(
                "final_holdout_query_hash"
            ),
            "final_holdout_data_hash": final_holdout_hashes.get(
                "final_holdout_data_hash"
            ),
            "final_holdout_fingerprint_hash": final_holdout_hashes.get(
                "final_holdout_fingerprint_hash"
            ),
            "final_holdout_quality_hash": final_holdout_hashes.get(
                "final_holdout_quality_hash"
            ),
            "objective_metric": final_holdout_hashes.get("objective_metric"),
            "pre_exposure_reservation_key_hash": registry_row.get(
                "pre_exposure_reservation_key_hash"
            ),
            "pre_exposure_reservation_key_schema_version": registry_row.get(
                "pre_exposure_reservation_key_schema_version"
            ),
            "train_split_hash": registry_row.get("train_split_hash"),
            "validation_split_hash": registry_row.get("validation_split_hash"),
            "final_holdout_split_hash": split_hashes.get("final_holdout"),
            "hypothesis_identity_source": identity["hypothesis_identity_source"],
            "experiment_family_identity_source": identity[
                "experiment_family_identity_source"
            ],
            "computed_attempt_index": attempt_index,
            "computed_holdout_reuse_count": holdout_reuse_count,
            "declared_attempt_index": declared_attempt_index,
            "declared_holdout_reuse_count": declared_holdout_reuse_count,
            "registry_gate_result": experiment_registry_reservation.get("gate_result")
            or "PASS",
            "registry_gate_fail_reasons": list(
                experiment_registry_reservation.get("gate_fail_reasons") or []
            ),
            "research_freedom_hash": experiment_registry_reservation.get(
                "research_freedom_hash"
            ),
            "final_holdout_content_pending_until_completion": content_pending,
        }
    elif manager is None or (
        manifest.research_classification == "research_only"
        and manifest.dataset.split.final_holdout is not None
    ):
        experiment_registry_fields = {
            "registry_gate_result": "WARN",
            "registry_gate_fail_reasons": ["experiment_registry_missing"],
            "computed_attempt_index": attempt_index,
            "computed_holdout_reuse_count": holdout_reuse_count,
            "declared_attempt_index": declared_attempt_index,
            "declared_holdout_reuse_count": declared_holdout_reuse_count,
        }
    lineage = build_research_lineage(
        experiment_id=manifest.experiment_id,
        experiment_family_id=experiment_family_id,
        hypothesis_id=hypothesis_id,
        hypothesis_status=hypothesis_status,
        hypothesis_identity_source=identity["hypothesis_identity_source"],
        experiment_family_identity_source=identity["experiment_family_identity_source"],
        pre_registered_at=identity.get("pre_registered_at"),
        manifest_path=manifest_path,
        manifest_hash=manifest.manifest_hash(),
        manifest_canonical_hash=manifest.manifest_hash(),
        dataset_snapshot_id=manifest.dataset.snapshot_id,
        dataset_content_hash=dataset_hash,
        dataset_quality_hash=dataset_quality_hash,
        dataset_split_hash=sha256_prefixed(
            {
                snapshot.split_name: snapshot.date_range.as_dict()
                for snapshot in snapshots
            }
        ),
        data_source_fingerprint=sha256_prefixed(
            {
                "source": manifest.dataset.source,
                "market": manifest.market,
                "interval": manifest.interval,
                "snapshot_id": manifest.dataset.snapshot_id,
                "adapter_provenance_hash": dataset_adapter_provenance_hash,
            }
        ),
        dataset_adapter_provenance_hash=dataset_adapter_provenance_hash,
        dataset_artifact=dataset_artifact,
        dataset_split_evidence={
            snapshot.split_name: {
                "requested_range": snapshot.date_range.as_dict(),
                "snapshot_data_hash": snapshot.snapshot_data_hash(),
                "snapshot_query_hash": snapshot.snapshot_query_hash(),
                "snapshot_fingerprint_hash": snapshot.snapshot_fingerprint_hash(),
                "quality_hash": next(
                    report.content_hash
                    for report in quality_reports
                    if report.payload["split_name"] == snapshot.split_name
                ),
                "verification_status": snapshot.verification.overall_status.value
                if snapshot.verification
                else "UNAVAILABLE",
            }
            for snapshot in snapshots
        },
        repository_version=repository_version,
        command_name=command_name or f"research-{report_kind}",
        command_args=command_args or {},
        environment=(
            execution_plan.payload.get("run_environment")
            if execution_plan is not None
            else {"repository_version": repository_version}
        ),
        cost_execution_model_hash=sha256_prefixed(manifest.execution_model.as_dict()),
        portfolio_policy_hash=portfolio_policy_hash,
        simulation_policy_hash=simulation_policy_hash,
        execution_calibration_artifact_hash=calibration_hash,
        search_budget=parameter_grid_size,
        parameter_grid_size=parameter_grid_size,
        attempt_index=attempt_index,
        failed_candidate_count=failed_count,
        holdout_reuse_count=holdout_reuse_count,
        experiment_registry_path=experiment_registry_fields.get(
            "experiment_registry_path"
        ),
        experiment_registry_prior_hash=experiment_registry_fields.get(
            "experiment_registry_prior_hash"
        ),
        experiment_registry_row_hash=experiment_registry_fields.get(
            "experiment_registry_row_hash"
        ),
        experiment_registry_completion_row_hash=experiment_registry_fields.get(
            "experiment_registry_completion_row_hash"
        ),
        final_holdout_fingerprint=experiment_registry_fields.get(
            "final_holdout_fingerprint"
        ),
        final_holdout_split_hash=experiment_registry_fields.get(
            "final_holdout_split_hash"
        ),
        computed_attempt_index=experiment_registry_fields.get("computed_attempt_index"),
        computed_holdout_reuse_count=experiment_registry_fields.get(
            "computed_holdout_reuse_count"
        ),
        declared_attempt_index=experiment_registry_fields.get("declared_attempt_index"),
        declared_holdout_reuse_count=experiment_registry_fields.get(
            "declared_holdout_reuse_count"
        ),
        research_freedom_hash=experiment_registry_fields.get("research_freedom_hash"),
        registry_gate_result=experiment_registry_fields.get("registry_gate_result"),
        registry_gate_fail_reasons=experiment_registry_fields.get(
            "registry_gate_fail_reasons"
        ),
        dataset_reuse_policy=dataset_reuse_policy,
        final_holdout_identity_hash=experiment_registry_fields.get(
            "final_holdout_identity_hash"
        ),
        final_holdout_content_hash=experiment_registry_fields.get(
            "final_holdout_content_hash"
        ),
        final_holdout_reuse_key_hash=experiment_registry_fields.get(
            "final_holdout_reuse_key_hash"
        ),
        dataset_artifact_evidence_hash=experiment_registry_fields.get(
            "dataset_artifact_evidence_hash"
        ),
        final_holdout_query_hash=experiment_registry_fields.get(
            "final_holdout_query_hash"
        ),
        final_holdout_data_hash=experiment_registry_fields.get(
            "final_holdout_data_hash"
        ),
        final_holdout_fingerprint_hash=experiment_registry_fields.get(
            "final_holdout_fingerprint_hash"
        ),
        final_holdout_quality_hash=experiment_registry_fields.get(
            "final_holdout_quality_hash"
        ),
        experiment_registry_bound_evidence_hash=experiment_registry_fields.get(
            "experiment_registry_bound_evidence_hash"
        ),
        experiment_registry_evidence_hash_phase=experiment_registry_fields.get(
            "experiment_registry_evidence_hash_phase"
        ),
        created_at=generated_at,
    )
    audit_trace_indexes = _collect_audit_trace_indexes(candidates)
    audit_trace_manifest: dict[str, Any] | None = None
    audit_trace_manifest_path: Path | None = None
    audit_verification: dict[str, Any] | None = None
    audit_reasons: list[str] = []
    if manifest.research_run.audit_trail.complete_external:
        if manager is not None:
            audit_trace_manifest = write_trace_manifest(
                manager=manager,
                experiment_id=manifest.experiment_id,
                manifest_hash=manifest.manifest_hash(),
                dataset_content_hash=dataset_hash,
                trace_indexes=audit_trace_indexes,
                policy=manifest.research_run.audit_trail,
                artifact_context=artifact_context,
            )
            audit_trace_manifest_path = trace_manifest_path(
                manager=manager, experiment_id=manifest.experiment_id
            )
            audit_verification = verify_audit_trail(
                manager=manager,
                experiment_id=manifest.experiment_id,
                expected_manifest_hash=manifest.manifest_hash(),
            )
            audit_reasons = [
                str(item) for item in audit_verification.get("reasons") or []
            ]
        else:
            audit_reasons = ["audit_trail_trace_manifest_missing"]
    elif (
        manifest.research_run.audit_trail.required_for_validation
        and statistical_validation_required(manifest)
    ):
        audit_reasons = ["audit_trail_required_for_validation"]
    statistical_contract = (
        manifest.statistical_validation.as_dict()
        if manifest.statistical_validation is not None
        else None
    )
    stress_contract = (
        manifest.stress_suite.as_dict() if manifest.stress_suite is not None else None
    )
    stress_contract_hash = (
        sha256_prefixed(stress_contract) if stress_contract is not None else None
    )
    benchmark_metrics = _benchmark_metrics_for_splits(
        snapshots,
        manifest=manifest,
        strategy_registry=strategy_registry,
        candidates=candidates,
        manager=governance_authority_manager or manager,
    )
    _attach_benchmark_metrics(
        candidates=candidates,
        benchmark_metrics=benchmark_metrics,
        required=bool(
            manifest.benchmark_suite
            and manifest.benchmark_suite.required_for_validation
        ),
    )
    required_scenario_ids = sorted(
        {
            str(scenario_id)
            for candidate in candidates
            for scenario_id in candidate.get("required_scenario_ids", [])
        }
    )
    statistical_evidence: dict[str, Any] | None = None
    statistical_evidence_path: Path | None = None
    return_panel: dict[str, Any] | None = None
    return_panel_path: Path | None = None
    family_registry_path: Path | None = None
    family_registry_prior_hash: str | None = None
    family_registry_row_hash: str | None = None
    universe_hash: str | None = None
    if statistical_contract is not None:
        return_panel = build_candidate_return_panel(
            experiment_id=manifest.experiment_id,
            manifest_hash=manifest.manifest_hash(),
            dataset_content_hash=dataset_hash,
            dataset_quality_hash=dataset_quality_hash,
            split="validation",
            benchmark=str(statistical_contract["benchmark"]),
            candidates=candidates,
            manager=manager,
        )
        if manager is not None:
            return_panel_path = write_candidate_return_panel(
                manager=manager,
                experiment_id=manifest.experiment_id,
                panel=return_panel,
                artifact_context=artifact_context,
            )
            if (
                statistical_contract.get("multiple_testing_scope")
                == "experiment_family"
            ):
                family_registry_path = family_trial_registry_path(
                    manager=manager,
                    experiment_family_id=experiment_family_id,
                )
                family_registry_prior_hash = registry_content_hash(family_registry_path)
        universe_hash = selection_universe_hash(
            manifest_hash=manifest.manifest_hash(),
            dataset_content_hash=dataset_hash,
            dataset_quality_hash=dataset_quality_hash,
            experiment_family_id=experiment_family_id,
            hypothesis_id=hypothesis_id,
            hypothesis_status=hypothesis_status,
            candidates=candidates,
            required_scenario_ids=required_scenario_ids,
            primary_metric_source="validation_metrics",
            benchmark=str(statistical_contract["benchmark"]),
            statistical_validation_contract=statistical_contract,
        )
        statistical_evidence = build_statistical_selection_evidence(
            manifest=manifest,
            candidates=candidates,
            manifest_hash=manifest.manifest_hash(),
            dataset_content_hash=dataset_hash,
            dataset_quality_hash=dataset_quality_hash,
            experiment_family_id=experiment_family_id,
            hypothesis_id=hypothesis_id,
            hypothesis_status=hypothesis_status,
            hypothesis_identity_source=identity["hypothesis_identity_source"],
            experiment_family_identity_source=identity[
                "experiment_family_identity_source"
            ],
            selection_hash=universe_hash,
            required_scenario_ids=required_scenario_ids,
            search_budget=parameter_grid_size,
            parameter_grid_size=parameter_grid_size,
            attempt_index=attempt_index,
            holdout_reuse_count=holdout_reuse_count,
            dataset_reuse_policy=dataset_reuse_policy,
            return_panel=return_panel,
            return_panel_path=return_panel_path,
            family_trial_registry_prior_hash=family_registry_prior_hash,
            family_trial_registry_path=family_registry_path,
            family_trial_registry_row_hash=family_registry_row_hash,
            experiment_registry=experiment_registry_fields or None,
        )
        if (
            statistical_evidence is not None
            and audit_reasons
            and manifest.research_run.audit_trail.required_for_validation
        ):
            statistical_evidence["statistical_gate_result"] = "FAIL"
            statistical_evidence["gate_fail_reasons"] = sorted(
                set(
                    str(item)
                    for item in statistical_evidence.get("gate_fail_reasons") or []
                )
                | set(audit_reasons)
                | {"audit_trail_required_for_validation"}
            )
            statistical_evidence["audit_trail_status"] = "FAIL"
            statistical_evidence["audit_trail_fail_reasons"] = sorted(
                set(audit_reasons)
            )
            statistical_evidence["content_hash"] = sha256_prefixed(
                content_hash_payload(
                    {
                        k: v
                        for k, v in statistical_evidence.items()
                        if k != "content_hash"
                    }
                )
            )
        if statistical_evidence is not None and manager is not None:
            statistical_evidence_path = write_statistical_selection_evidence(
                manager=manager,
                experiment_id=manifest.experiment_id,
                evidence=statistical_evidence,
                artifact_context=artifact_context,
            )
            if (
                statistical_contract.get("multiple_testing_scope")
                == "experiment_family"
            ):
                statistical_evidence["family_trial_registry_bound_evidence_hash"] = (
                    statistical_evidence.get("content_hash")
                )
                registry_result = append_family_trial_registry_row(
                    manager=manager,
                    experiment_family_id=experiment_family_id,
                    experiment_id=manifest.experiment_id,
                    manifest_hash=manifest.manifest_hash(),
                    hypothesis_id=str(hypothesis_id)
                    if hypothesis_id is not None
                    else None,
                    hypothesis_status=str(hypothesis_status)
                    if hypothesis_status is not None
                    else None,
                    attempt_index=attempt_index,
                    holdout_reuse_count=holdout_reuse_count,
                    dataset_content_hash=dataset_hash,
                    parameter_space_hash=sha256_prefixed(manifest.parameter_space),
                    candidate_count=len(candidates),
                    return_panel_hash=str(return_panel.get("content_hash"))
                    if isinstance(return_panel, dict)
                    else None,
                    statistical_evidence_hash=str(
                        statistical_evidence.get(
                            "family_trial_registry_bound_evidence_hash"
                        )
                    ),
                    result_status=str(
                        statistical_evidence.get("statistical_gate_result") or "UNKNOWN"
                    ),
                    created_at=generated_at,
                )
                family_registry_row_hash = str(registry_result.get("row_hash") or "")
                statistical_evidence["family_trial_registry_row_hash"] = (
                    family_registry_row_hash
                )
                statistical_evidence["content_hash"] = sha256_prefixed(
                    content_hash_payload(
                        {
                            k: v
                            for k, v in statistical_evidence.items()
                            if k != "content_hash"
                        }
                    )
                )
                statistical_evidence_path = write_statistical_selection_evidence(
                    manager=manager,
                    experiment_id=manifest.experiment_id,
                    evidence=statistical_evidence,
                    artifact_context=artifact_context,
                )
        if (
            statistical_evidence is not None
            and manager is not None
            and experiment_registry_reservation is not None
        ):
            pre_completion_evidence_hash = str(
                statistical_evidence.get("content_hash") or ""
            )
            completion_result = append_attempt_completion(
                manager=manager,
                reservation=experiment_registry_reservation,
                updates={
                    "dataset_content_hash": dataset_hash,
                    "dataset_quality_hash": dataset_quality_hash,
                    "dataset_artifact_evidence_hash": experiment_registry_fields.get(
                        "dataset_artifact_evidence_hash"
                    ),
                    "final_holdout_query_hash": experiment_registry_fields.get(
                        "final_holdout_query_hash"
                    ),
                    "final_holdout_data_hash": experiment_registry_fields.get(
                        "final_holdout_data_hash"
                    ),
                    "final_holdout_fingerprint_hash": experiment_registry_fields.get(
                        "final_holdout_fingerprint_hash"
                    ),
                    "final_holdout_quality_hash": experiment_registry_fields.get(
                        "final_holdout_quality_hash"
                    ),
                    "final_holdout_reuse_key_hash": experiment_registry_fields.get(
                        "final_holdout_reuse_key_hash"
                    ),
                    "final_holdout_reuse_key_schema_version": experiment_registry_fields.get(
                        "final_holdout_reuse_key_schema_version"
                    ),
                    "final_holdout_split_hash": experiment_registry_fields.get(
                        "final_holdout_split_hash"
                    ),
                    "final_holdout_content_hash": experiment_registry_fields.get(
                        "final_holdout_content_hash"
                    ),
                    "candidate_count": len(candidates),
                    "return_panel_hash": str(return_panel.get("content_hash"))
                    if isinstance(return_panel, dict)
                    else None,
                    "statistical_evidence_hash": pre_completion_evidence_hash,
                    "statistical_evidence_hash_phase": EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE,
                    "statistical_gate_result": statistical_evidence.get(
                        "statistical_gate_result"
                    ),
                },
                result_status="COMPLETED",
                created_at=generated_at,
            )
            experiment_registry_fields["experiment_registry_completion_row_hash"] = (
                completion_result.get("row_hash")
            )
            experiment_registry_fields["computed_holdout_reuse_count"] = (
                completion_result["row"].get("computed_holdout_reuse_count")
            )
            experiment_registry_fields["research_freedom_hash"] = research_freedom_hash(
                {
                    **completion_result["row"],
                    "experiment_registry_path": experiment_registry_fields.get(
                        "experiment_registry_path"
                    ),
                    "experiment_registry_prior_hash": completion_result.get(
                        "prior_hash"
                    ),
                    "experiment_registry_row_hash": experiment_registry_fields.get(
                        "experiment_registry_row_hash"
                    ),
                    "experiment_registry_completion_row_hash": completion_result.get(
                        "row_hash"
                    ),
                }
            )
            experiment_registry_fields["experiment_registry_bound_evidence_hash"] = (
                pre_completion_evidence_hash
            )
            experiment_registry_fields["experiment_registry_evidence_hash_phase"] = (
                EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE
            )
            lineage.update(
                {
                    "experiment_registry_completion_row_hash": experiment_registry_fields.get(
                        "experiment_registry_completion_row_hash"
                    ),
                    "research_freedom_hash": experiment_registry_fields.get(
                        "research_freedom_hash"
                    ),
                    "experiment_registry_bound_evidence_hash": experiment_registry_fields.get(
                        "experiment_registry_bound_evidence_hash"
                    ),
                    "experiment_registry_evidence_hash_phase": experiment_registry_fields.get(
                        "experiment_registry_evidence_hash_phase"
                    ),
                }
            )
            lineage.pop("lineage_hash", None)
            lineage["lineage_hash"] = compute_lineage_hash(lineage)
            statistical_evidence.update(experiment_registry_fields)
            statistical_evidence["content_hash"] = sha256_prefixed(
                content_hash_payload(
                    {
                        k: v
                        for k, v in statistical_evidence.items()
                        if k != "content_hash"
                    }
                )
            )
            statistical_evidence_path = write_statistical_selection_evidence(
                manager=manager,
                experiment_id=manifest.experiment_id,
                evidence=statistical_evidence,
                artifact_context=artifact_context,
            )
        attachment_observability = _attach_statistical_selection_to_candidates(
            candidates=candidates,
            required=statistical_validation_required(manifest),
            contract=statistical_contract,
            selection_hash=universe_hash,
            evidence=statistical_evidence,
            evidence_path=statistical_evidence_path,
        )
        if isinstance(execution_observability, dict):
            execution_observability.setdefault("stage_timings", []).extend(
                attachment_observability.substage_timings
            )
            profile_observability = execution_observability.setdefault(
                "candidate_profile_hash_observability",
                _empty_hash_observability(),
            )
            _merge_hash_observability(
                profile_observability,
                attachment_observability.candidate_profile_hash_observability,
            )
            profile_observability["post_statistical_profile_hash"] = dict(
                attachment_observability.candidate_profile_hash_observability
            )
    if (
        experiment_registry_reservation is not None
        and manager is not None
        and not experiment_registry_fields.get(
            "experiment_registry_completion_row_hash"
        )
    ):
        completion_result = append_attempt_completion(
            manager=manager,
            reservation=experiment_registry_reservation,
            updates={
                "dataset_content_hash": dataset_hash,
                "dataset_quality_hash": dataset_quality_hash,
                "dataset_artifact_evidence_hash": experiment_registry_fields.get(
                    "dataset_artifact_evidence_hash"
                ),
                "final_holdout_query_hash": experiment_registry_fields.get(
                    "final_holdout_query_hash"
                ),
                "final_holdout_data_hash": experiment_registry_fields.get(
                    "final_holdout_data_hash"
                ),
                "final_holdout_fingerprint_hash": experiment_registry_fields.get(
                    "final_holdout_fingerprint_hash"
                ),
                "final_holdout_quality_hash": experiment_registry_fields.get(
                    "final_holdout_quality_hash"
                ),
                "final_holdout_reuse_key_hash": experiment_registry_fields.get(
                    "final_holdout_reuse_key_hash"
                ),
                "final_holdout_reuse_key_schema_version": experiment_registry_fields.get(
                    "final_holdout_reuse_key_schema_version"
                ),
                "final_holdout_split_hash": experiment_registry_fields.get(
                    "final_holdout_split_hash"
                ),
                "final_holdout_content_hash": experiment_registry_fields.get(
                    "final_holdout_content_hash"
                ),
                "candidate_count": len(candidates),
                "return_panel_hash": None,
                "statistical_evidence_hash": None,
                "statistical_evidence_hash_phase": EXPERIMENT_REGISTRY_EVIDENCE_HASH_PHASE,
                "statistical_gate_result": None,
            },
            result_status="COMPLETED",
            created_at=generated_at,
        )
        experiment_registry_fields["experiment_registry_completion_row_hash"] = (
            completion_result.get("row_hash")
        )
        experiment_registry_fields["computed_holdout_reuse_count"] = completion_result[
            "row"
        ].get("computed_holdout_reuse_count")
        experiment_registry_fields["research_freedom_hash"] = research_freedom_hash(
            {
                **completion_result["row"],
                "experiment_registry_path": experiment_registry_fields.get(
                    "experiment_registry_path"
                ),
                "experiment_registry_prior_hash": completion_result.get("prior_hash"),
                "experiment_registry_row_hash": experiment_registry_fields.get(
                    "experiment_registry_row_hash"
                ),
                "experiment_registry_completion_row_hash": completion_result.get(
                    "row_hash"
                ),
            }
        )
        lineage.update(
            {
                "experiment_registry_completion_row_hash": experiment_registry_fields[
                    "experiment_registry_completion_row_hash"
                ],
                "research_freedom_hash": experiment_registry_fields[
                    "research_freedom_hash"
                ],
            }
        )
        lineage.pop("lineage_hash", None)
        lineage["lineage_hash"] = compute_lineage_hash(lineage)
    final_selection = apply_final_selection_contract(
        contract=manifest.final_selection,
        candidates=candidates,
        report_context={
            "dataset_quality_gate_status": dataset_quality_status,
            "statistical_gate_result": statistical_evidence.get(
                "statistical_gate_result"
            )
            if statistical_evidence
            else None,
        },
        validation_required=manifest.research_classification != "research_only",
    )
    selection_artifact = build_selection_artifact(
        manifest_hash=manifest.manifest_hash(),
        selection_result=final_selection,
        candidates=candidates,
    )
    best = next(
        (
            candidate
            for candidate in candidates
            if candidate.get("parameter_candidate_id")
            == final_selection.get("selected_candidate_id")
        ),
        None,
    )
    if best is None and final_selection.get("gate_result") == "WARN":
        best = next(
            (
                candidate
                for candidate in candidates
                if candidate["acceptance_gate_result"] == "PASS"
            ),
            None,
        )
    stress_summary_candidate = best
    if (
        stress_summary_candidate is None
        and stress_suite_required(manifest)
        and candidates
    ):
        stress_summary_candidate = candidates[0]
    warnings = {
        warning for candidate in candidates for warning in candidate.get("warnings", [])
    }
    warnings.update(manifest.portfolio_policy.warning_codes())
    warnings.update(_resource_budget_warnings(manifest))
    if experiment_registry_fields.get("registry_gate_result") == "WARN":
        warnings.update(
            str(item)
            for item in experiment_registry_fields.get("registry_gate_fail_reasons")
            or []
        )
    if isinstance(statistical_evidence, dict) and not statistical_evidence.get(
        "official_statistical_evidence_wrc_generation_available",
        False,
    ):
        warnings.add(STATISTICAL_EVIDENCE_GENERATION_UNAVAILABLE_WARNING)
    warnings = sorted(warnings)
    signal_depth_summary = _report_signal_depth_summary(candidates)
    strategy_plugin = strategy_registry.resolve(manifest.strategy_name)
    strategy_spec = strategy_plugin.spec
    depth_walk_used = bool(signal_depth_summary.get("depth_walk_execution_model_used"))
    depth_available_semantics = (
        "depth_walk_execution_model_used_with_signal_level_l2_depth"
        if depth_walk_used
        else "stored_l2_depth_complete_snapshots_exist_not_execution_model_used"
    )
    cost_authority = _cost_authority_resolution(manifest)
    selection_metric_policy = {
        "primary_metric_source": "validation_metrics",
        "primary_metric_source_semantics": "primary_base_scenario_alias",
        "primary_metric_scenario_role": "base",
        "aggregate_gate_source": "required_scenario_policy",
        "candidate_eligibility_gate": "aggregate_acceptance_gate_result",
    }
    resource_budget = _resource_budget_report(manifest)
    resource_summary = _resource_integrity_summary(candidates)
    top_level_classification = _top_level_classification(candidates)
    payload = {
        "report_kind": report_kind,
        "experiment_id": manifest.experiment_id,
        "run_id": (command_args or {}).get("run_id"),
        "run_purpose": manifest.research_run.run_purpose,
        "hypothesis": manifest.hypothesis,
        "manifest_hash": manifest.manifest_hash(),
        "dataset_snapshot_id": manifest.dataset.snapshot_id,
        "dataset_content_hash": dataset_hash,
        "dataset_content_hash_semantics": "combined_run_dataset_fingerprint",
        "dataset_quality_hash": dataset_quality_hash,
        "dataset_artifact": dataset_artifact,
        "dataset_artifact_evidence_hash": experiment_registry_fields.get(
            "dataset_artifact_evidence_hash"
        ),
        "final_holdout_query_hash": experiment_registry_fields.get(
            "final_holdout_query_hash"
        ),
        "final_holdout_data_hash": experiment_registry_fields.get(
            "final_holdout_data_hash"
        ),
        "final_holdout_fingerprint_hash": experiment_registry_fields.get(
            "final_holdout_fingerprint_hash"
        ),
        "final_holdout_quality_hash": experiment_registry_fields.get(
            "final_holdout_quality_hash"
        ),
        "dataset_adapter_provenance": dataset_adapter_provenance,
        "dataset_adapter_provenance_hash": dataset_adapter_provenance_hash,
        "dataset_quality_gate_status": dataset_quality_status,
        "dataset_quality_gate_reasons": dataset_quality_reasons,
        "dataset_quality_reports": {
            str(report.payload["split_name"]): report.payload
            for report in quality_reports
        },
        "market": manifest.market,
        "interval": manifest.interval,
        "dataset_splits": {
            snapshot.split_name: {
                "date_range": snapshot.date_range.as_dict(),
                "requested_range": snapshot.date_range.as_dict(),
                "candle_count": len(snapshot.candles),
                "artifact_id": snapshot.artifact_id,
                "artifact_content_hash": snapshot.artifact_content_hash,
                "artifact_schema_hash": snapshot.artifact_schema_hash,
                "artifact_manifest_hash": snapshot.artifact_manifest_hash,
                "source_provenance_hash": snapshot.source_provenance_hash,
                "verification_status": snapshot.verification.overall_status.value
                if snapshot.verification
                else "UNAVAILABLE",
                "verification": snapshot.verification.as_dict()
                if snapshot.verification
                else None,
                "content_hash": snapshot.snapshot_fingerprint_hash(),
                "snapshot_data_hash": snapshot.snapshot_data_hash(),
                "snapshot_query_hash": snapshot.snapshot_query_hash(),
                "snapshot_fingerprint_hash": snapshot.snapshot_fingerprint_hash(),
                "quality_hash": next(
                    report.content_hash
                    for report in quality_reports
                    if report.payload["split_name"] == snapshot.split_name
                ),
            }
            for snapshot in snapshots
        },
        "data_limitations": {
            "candle_only": not top_of_book_requested,
            "top_of_book_requested": top_of_book_requested,
            "top_of_book_required": bool(manifest.dataset.top_of_book.required)
            if manifest.dataset.top_of_book
            else False,
            "top_of_book_available": top_of_book_joined_count > 0,
            "top_of_book_is_full_depth": False,
            "orderbook_depth_available": l2_depth_complete_snapshots_available,
            "depth_available": l2_depth_complete_snapshots_available,
            "depth_available_semantics": depth_available_semantics,
            "l2_depth_evidence_available": l2_depth_complete_snapshots_available,
            "depth_evidence_available": l2_depth_complete_snapshots_available,
            "l2_depth_rows_available": l2_depth_rows_available,
            "l2_depth_complete_snapshots_available": l2_depth_complete_snapshots_available,
            "l2_depth_snapshot_count": top_of_book_quality_summary.get(
                "l2_depth_snapshot_count"
            ),
            "l2_depth_row_count": top_of_book_quality_summary.get("l2_depth_row_count"),
            "l2_depth_first_ts": top_of_book_quality_summary.get("l2_depth_first_ts"),
            "l2_depth_last_ts": top_of_book_quality_summary.get("l2_depth_last_ts"),
            "l2_depth_sources": top_of_book_quality_summary.get("l2_depth_sources"),
            "l2_depth_content_hashes": top_of_book_quality_summary.get(
                "l2_depth_content_hashes"
            ),
            "signal_level_depth_coverage_pct": signal_depth_summary.get(
                "signal_level_depth_coverage_pct"
            ),
            "signal_level_depth_coverage_status": signal_depth_summary.get(
                "signal_level_depth_coverage_status"
            ),
            "depth_snapshot_selection_policy": top_of_book_quality_summary.get(
                "depth_snapshot_selection_policy"
            ),
            "depth_liquidity_sufficiency_status": signal_depth_summary.get(
                "depth_liquidity_sufficiency_status"
            ),
            "depth_walk_execution_model_available": top_of_book_quality_summary.get(
                "depth_walk_execution_model_available"
            ),
            "depth_walk_execution_model_used": depth_walk_used,
            "depth_full_fill_count": signal_depth_summary.get("depth_full_fill_count"),
            "depth_partial_fill_count": signal_depth_summary.get(
                "depth_partial_fill_count"
            ),
            "depth_unfilled_count": signal_depth_summary.get("depth_unfilled_count"),
            "depth_missing_snapshot_count": signal_depth_summary.get(
                "depth_missing_snapshot_count"
            ),
            "depth_evidence_refs": signal_depth_summary.get("depth_evidence_refs"),
            "full_orderbook_depth_available": False,
            "queue_position_available": False,
            "trade_ticks_available": False,
            "market_impact_model_available": False,
            "trade_tick_evidence_available": False,
            "queue_evidence_available": False,
            "impact_model_evidence_available": False,
            "intra_candle_path_available": False,
            "execution_reference_price": manifest.execution_timing.fill_reference_policy,
            "intra_candle_policy": _policy_intra_candle_limitation(
                manifest.execution_timing.fill_reference_policy
            ),
            "portfolio_event_time_policy": "fills_apply_when_fill_reference_ts_reaches_mark_or_decision_boundary",
            "subprocess_candidate_isolation": _subprocess_candidate_isolation_status(
                execution_observability
            ),
            "top_of_book_join_tolerance_ms": (
                manifest.dataset.top_of_book.join_tolerance_ms
                if manifest.dataset.top_of_book
                else None
            ),
        },
        "top_of_book_quality_summary": top_of_book_quality_summary,
        "execution_timing_policy": manifest.execution_timing.as_dict(),
        "execution_reality_contract": report_execution_contract,
        "execution_contract_hash": report_execution_contract["execution_contract_hash"],
        "execution_capability_contract": report_capability_contract,
        "execution_capability_contract_hash": report_capability_contract[
            "execution_capability_contract_hash"
        ],
        "evidence_tier": report_capability_contract["evidence_tier"],
        "unavailable_required_capabilities": report_capability_contract[
            "unavailable_required_capabilities"
        ],
        "execution_limitations": report_capability_contract["limitations"],
        "market_impact_required": manifest.execution_timing.market_impact_required,
        "market_impact_model_available": report_capability_contract[
            "available_capabilities"
        ]["market_impact_model"],
        "top_of_book_is_full_depth": report_capability_contract[
            "available_capabilities"
        ]["top_of_book_is_full_depth"],
        "execution_reality_level": _report_execution_reality_level(candidates),
        "execution_reality_gate_status": _report_execution_reality_gate_status(
            candidates
        ),
        "execution_reality_gate_reasons": _report_execution_reality_gate_reasons(
            candidates
        ),
        "signal_quote_coverage_summary": _report_signal_quote_coverage_summary(
            candidates
        ),
        "signal_depth_coverage_summary": signal_depth_summary,
        "execution_event_summary": _report_execution_event_summary(candidates),
        "strategy_name": manifest.strategy_name,
        "strategy_version": strategy_plugin.version,
        "declared_strategy_version": manifest.strategy_version,
        "regime_classifier_version": MARKET_REGIME_VERSION,
        "regime_acceptance_gate": manifest.acceptance_gate.regime_acceptance_gate.as_dict(),
        "execution_model": manifest.execution_model.as_dict(),
        "execution_model_source": manifest.execution_model.source,
        "cost_assumption_contract": manifest.execution_model.as_dict(),
        "base_cost_assumption": _report_base_cost_assumption(candidates),
        "cost_authority_source": cost_authority["cost_authority_source"],
        "cost_authority_resolution": cost_authority["cost_authority_resolution"],
        "runtime_base_cost_assumption": cost_authority["runtime_base_cost_assumption"],
        "legacy_cost_model_present": cost_authority["legacy_cost_model_present"],
        "legacy_cost_model_authority": cost_authority["legacy_cost_model_authority"],
        "scenario_cost_assumption_contract_hash": cost_authority[
            "scenario_cost_assumption_contract_hash"
        ],
        "portfolio_policy": portfolio_policy,
        "portfolio_policy_hash": portfolio_policy_hash,
        "risk_policy": manifest.risk_policy.as_dict(),
        "risk_policy_hash": manifest.risk_policy_hash(),
        "simulation_policy_hash": simulation_policy_hash,
        "research_run": manifest.research_run.as_dict(),
        "resource_budget": resource_budget,
        "diagnostic_mode": manifest.research_run.diagnostic_mode,
        "diagnostic_only": manifest.research_run.diagnostic_mode == "exploratory",
        "validation_gate_non_authoritative": manifest.research_run.diagnostic_mode
        == "exploratory",
        "execution_policy": manifest.research_run.execution.as_dict(),
        "execution_plan": execution_plan.as_dict()
        if execution_plan is not None
        else None,
        "workload_estimate": _report_workload_estimate(
            manifest=manifest,
            snapshots=snapshots,
            candidates=candidates,
            report_kind=report_kind,
            execution_plan=execution_plan,
            execution_observability=execution_observability,
        ),
        "run_environment": (
            execution_plan.payload.get("run_environment")
            if execution_plan is not None
            else {
                "repository_version": repository_version,
                "manifest_hash": manifest.manifest_hash(),
                "execution_mode": manifest.research_run.execution.mode,
                "effective_max_workers": manifest.research_run.execution.max_workers,
                "work_unit_type": manifest.research_run.execution.work_unit,
            }
        ),
        "execution_observability": execution_observability
        or {"stage_timings": [], "work_units": []},
        "resource_integrity_summary": resource_summary,
        **top_level_classification,
        "audit_trail_policy": manifest.research_run.audit_trail.as_dict(),
        "audit_trail_status": "PASS"
        if manifest.research_run.audit_trail.complete_external and not audit_reasons
        else (
            "DISABLED"
            if not manifest.research_run.audit_trail.complete_external
            else "FAIL"
        ),
        "audit_trail_fail_reasons": sorted(set(audit_reasons)),
        "audit_trail_trace_manifest_hash": (
            audit_trace_manifest.get("content_hash")
            if isinstance(audit_trace_manifest, dict)
            else None
        ),
        "audit_trail_trace_manifest_ref": (
            _data_dir_relative_ref(manager, audit_trace_manifest_path)
            if manager is not None and audit_trace_manifest_path is not None
            else None
        ),
        "audit_trail_trace_manifest_path": str(audit_trace_manifest_path.resolve())
        if audit_trace_manifest_path
        else None,
        "audit_trail_trace_index_count": len(audit_trace_indexes),
        "audit_trail_verification": audit_verification,
        "metrics_schema_version": METRICS_SCHEMA_VERSION,
        "metrics_gate_policy": metrics_gate_policy_from_acceptance_gate(
            manifest.acceptance_gate
        ),
        "metrics_gate_policy_hash": metrics_gate_policy_hash(
            metrics_gate_policy_from_acceptance_gate(manifest.acceptance_gate)
        ),
        "metrics_contract_required": bool(
            manifest.acceptance_gate.metrics_contract_required
        ),
        "stress_suite_required": stress_suite_required(manifest),
        "benchmark_suite_required": bool(
            manifest.benchmark_suite
            and manifest.benchmark_suite.required_for_validation
        ),
        "benchmark_suite_contract": (
            manifest.benchmark_suite.as_dict()
            if manifest.benchmark_suite is not None
            else None
        ),
        "benchmark_suite_gate_result": best.get("benchmark_suite_gate_result")
        if best
        else None,
        "benchmark_suite_fail_reasons": best.get("benchmark_suite_fail_reasons")
        if best
        else [],
        "stress_suite_contract": stress_contract,
        "stress_suite_contract_hash": stress_contract_hash,
        "final_selection_required": bool(
            manifest.final_selection.required_for_validation
            if manifest.final_selection is not None
            else False
        ),
        "final_selection_contract": final_selection.get("final_selection_contract"),
        "final_selection_contract_hash": final_selection.get(
            "final_selection_contract_hash"
        ),
        "final_selection_gate_result": final_selection.get("gate_result"),
        "final_selection_fail_reasons": final_selection.get("fail_reasons") or [],
        "selected_candidate_id": final_selection.get("selected_candidate_id"),
        "selected_candidate_score_hash": final_selection.get(
            "selected_candidate_score_hash"
        ),
        "candidate_final_scores_hash": final_selection.get(
            "candidate_final_scores_hash"
        ),
        "candidate_final_scores": final_selection.get("candidate_final_scores") or [],
        "selection_artifact": selection_artifact,
        "selection_artifact_hash": selection_artifact.get("content_hash")
        if selection_artifact
        else None,
        "statistical_validation_required": statistical_validation_required(manifest),
        "statistical_validation_contract": statistical_contract,
        "benchmark": statistical_evidence.get("benchmark")
        if statistical_evidence
        else None,
        "primary_metric": statistical_evidence.get("primary_metric")
        if statistical_evidence
        else None,
        "primary_metric_source": statistical_evidence.get("primary_metric_source")
        if statistical_evidence
        else None,
        "primary_metric_source_semantics": (
            statistical_evidence.get("primary_metric_source_semantics")
            if statistical_evidence
            else None
        ),
        "primary_metric_scenario_role": (
            statistical_evidence.get("primary_metric_scenario_role")
            if statistical_evidence
            else None
        ),
        "primary_metric_scenario_id": (
            statistical_evidence.get("primary_metric_scenario_id")
            if statistical_evidence
            else None
        ),
        "aggregate_gate_source": (
            statistical_evidence.get("aggregate_gate_source")
            if statistical_evidence
            else selection_metric_policy["aggregate_gate_source"]
        ),
        "selection_metric_policy": selection_metric_policy,
        "selection_universe_hash": universe_hash,
        "candidate_metric_values_hash": (
            statistical_evidence.get("candidate_metric_values_hash")
            if statistical_evidence
            else None
        ),
        "candidate_metric_values_summary": (
            statistical_evidence.get("candidate_metric_values_summary")
            if statistical_evidence
            else None
        ),
        "metric_value_count": statistical_evidence.get("metric_value_count")
        if statistical_evidence
        else None,
        "missing_metric_count": statistical_evidence.get("missing_metric_count")
        if statistical_evidence
        else None,
        "statistical_evidence_hash": statistical_evidence.get("content_hash")
        if statistical_evidence
        else None,
        "statistical_evidence_path": str(statistical_evidence_path.resolve())
        if statistical_evidence_path
        else None,
        "return_panel_hash": return_panel.get("content_hash") if return_panel else None,
        "return_panel_path": str(return_panel_path.resolve())
        if return_panel_path
        else None,
        "return_panel_split": return_panel.get("split") if return_panel else None,
        "return_unit": return_panel.get("return_unit") if return_panel else None,
        "return_panel_observation_count": return_panel.get("observation_count")
        if return_panel
        else None,
        "benchmark_metrics": benchmark_metrics,
        "evidence_grade": statistical_evidence.get("evidence_grade")
        if statistical_evidence
        else None,
        "statistical_method": statistical_evidence.get("statistical_method")
        if statistical_evidence
        else None,
        "family_trial_registry_path": str(family_registry_path.resolve())
        if family_registry_path
        else None,
        "family_trial_registry_prior_hash": family_registry_prior_hash,
        "family_trial_registry_row_hash": family_registry_row_hash,
        **experiment_registry_fields,
        "statistical_gate_result": statistical_evidence.get("statistical_gate_result")
        if statistical_evidence
        else None,
        "statistical_gate_fail_reasons": statistical_evidence.get("gate_fail_reasons")
        if statistical_evidence
        else [],
        "white_reality_check_p_value": (
            statistical_evidence.get("white_reality_check_p_value")
            if statistical_evidence
            else None
        ),
        "summary_metric_max_bootstrap_p_value": (
            statistical_evidence.get("summary_metric_max_bootstrap_p_value")
            if statistical_evidence
            else None
        ),
        "white_reality_check_method": (
            statistical_evidence.get("white_reality_check_method")
            if statistical_evidence
            else None
        ),
        "bootstrap_sampling_contract_hash": (
            statistical_evidence.get("bootstrap_sampling_contract_hash")
            if statistical_evidence
            else None
        ),
        "statistical_evidence_limitations": (
            statistical_evidence.get("statistical_evidence_limitations")
            if statistical_evidence
            else []
        ),
        "official_statistical_evidence_wrc_generation_available": (
            statistical_evidence.get(
                "official_statistical_evidence_wrc_generation_available"
            )
            if statistical_evidence
            else False
        ),
        "effective_trial_count": statistical_evidence.get("effective_trial_count")
        if statistical_evidence
        else None,
        "research_classification": manifest.research_classification,
        "execution_calibration_required": manifest.execution_model.calibration_required,
        "market_regime_bucket_performance": (
            best.get("market_regime_bucket_performance") if best else None
        ),
        "market_regime_coverage": best.get("market_regime_coverage") if best else None,
        "walk_forward_regime_coverage": (
            best.get("walk_forward_metrics", {}).get("windows")
            if best and isinstance(best.get("walk_forward_metrics"), dict)
            else None
        ),
        "regime_gate_result": best.get("regime_gate_result") if best else None,
        "allowed_live_regimes": best.get("allowed_live_regimes") if best else None,
        "blocked_live_regimes": best.get("blocked_live_regimes") if best else None,
        "candidate_count": len(candidates),
        "experiment_family_id": lineage.get("experiment_family_id"),
        "hypothesis_id": lineage.get("hypothesis_id"),
        "hypothesis_status": lineage.get("hypothesis_status"),
        "hypothesis_identity_source": identity["hypothesis_identity_source"],
        "experiment_family_identity_source": identity[
            "experiment_family_identity_source"
        ],
        "pre_registered_gate": bool(identity.get("pre_registration_verified")),
        "hypothesis_spec": manifest.hypothesis_spec.as_dict()
        if manifest.hypothesis_spec is not None
        else None,
        "hypothesis_contract_hash": identity.get("hypothesis_contract_hash"),
        "hypothesis_semantic_fingerprint": identity.get(
            "hypothesis_semantic_fingerprint"
        ),
        "hypothesis_version": identity.get("hypothesis_version"),
        "experiment_specification_completeness": {
            "status": "COMPLETE"
            if manifest.hypothesis_spec is not None
            else "LEGACY_INCOMPLETE",
            "explicit_contract_fields": sorted(
                field
                for field in (
                    "strategy_version",
                    "execution_timing",
                    "portfolio_policy",
                    "risk_policy",
                    "cost_model",
                    "execution_model",
                    "acceptance_gate",
                )
                if manifest.raw.get(field) is not None
            ),
        },
        "search_budget": lineage.get("search_budget"),
        "parameter_space_hash": sha256_prefixed(manifest.parameter_space),
        "parameter_grid_size": lineage.get("parameter_grid_size"),
        "attempt_index": lineage.get("attempt_index"),
        "failed_candidate_count": lineage.get("failed_candidate_count"),
        "holdout_reuse_count": lineage.get("holdout_reuse_count"),
        "dataset_reuse_policy": lineage.get("dataset_reuse_policy"),
        "best_candidate_id": best.get("parameter_candidate_id") if best else None,
        "best_behavior_hash": best.get("behavior_hash") if best else None,
        "strategy_spec": best.get("strategy_spec") if best else strategy_spec.as_dict(),
        "strategy_spec_hash": best.get("strategy_spec_hash")
        if best
        else strategy_spec.spec_hash(),
        "strategy_plugin_contract": (
            best.get("strategy_plugin_contract")
            if best
            else strategy_plugin.contract_payload()
        ),
        "strategy_plugin_contract_hash": (
            best.get("strategy_plugin_contract_hash")
            if best
            else strategy_plugin.contract_hash()
        ),
        "strategy_registry_hash": (
            best.get("strategy_registry_hash")
            if best
            else strategy_registry.execution_scope_hash(manifest.strategy_name)
        ),
        "compiled_strategy_contract": best.get("compiled_strategy_contract")
        if best
        else None,
        "compiled_strategy_contract_hash": best.get("compiled_strategy_contract_hash")
        if best
        else None,
        "exit_policy": best.get("exit_policy") if best else None,
        "exit_policy_hash": best.get("exit_policy_hash") if best else None,
        "best_validation_metrics_v2": best.get("validation_metrics_v2")
        if best
        else None,
        "closed_trade_diagnostics_summary": _closed_trade_diagnostics_summary(
            best or {}
        ),
        "stress_suite_gate_result": (
            stress_summary_candidate.get("stress_suite_gate_result")
            if stress_summary_candidate
            else None
        ),
        "stress_suite_fail_reasons": (
            stress_summary_candidate.get("stress_suite_fail_reasons")
            if stress_summary_candidate
            else []
        ),
        "best_validation_stress_suite": (
            stress_summary_candidate.get("validation_stress_suite")
            if stress_summary_candidate
            else None
        ),
        "candidate_acceptance_gate_result": "PASS" if best else "FAIL",
        "statistical_selection_gate_result": statistical_evidence.get(
            "statistical_gate_result"
        )
        if statistical_evidence
        else None,
        "walk_forward_gate_result": best.get("walk_forward_gate_result")
        if best
        else None,
        "validation_eligibility_gate_result": "FAIL",
        "validation_blocking_reasons": [],
        "gate_result": "FAIL",
        "warnings": warnings,
        "candidates": [
            _selection_only_candidate_payload(candidate) for candidate in candidates
        ],
        "repository_version": repository_version,
        "lineage": lineage,
        "lineage_hash": lineage["lineage_hash"],
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
    }
    validation_blocking_reasons = _validation_blocking_reasons(
        best=best,
        statistical_required=statistical_validation_required(manifest),
        statistical_evidence=statistical_evidence,
        report=payload,
    )
    payload["validation_blocking_reasons"] = validation_blocking_reasons
    validation_pass = best is not None and not validation_blocking_reasons
    payload["validation_eligibility_gate_result"] = (
        "PASS" if validation_pass else "FAIL"
    )
    payload["gate_result"] = "PASS" if validation_pass else "FAIL"
    if report_kind == "backtest":
        payload["validation_run_complete"] = False
        payload["diagnostic_only"] = True
        payload["standalone_backtest_not_full_validation"] = True
        payload["next_required_stage"] = (
            "research-walk-forward"
            if manifest.acceptance_gate.walk_forward_required
            else "research-validate"
        )
        if manifest.acceptance_gate.walk_forward_required:
            reason = "walk_forward_required_but_not_executed_in_this_run"
            payload["validation_blocking_reasons"] = sorted(
                set(payload["validation_blocking_reasons"] + [reason])
            )
            payload["validation_eligibility_gate_result"] = "FAIL"
            payload["gate_result"] = "FAIL"
    return payload


def _report_workload_estimate(
    *,
    manifest: ExperimentManifest,
    snapshots: tuple[DatasetSnapshot, ...],
    candidates: list[dict[str, Any]],
    report_kind: str,
    execution_plan: ResearchExecutionPlan | None,
    execution_observability: dict[str, Any] | None,
) -> dict[str, Any]:
    if execution_plan is not None and isinstance(
        execution_plan.payload.get("workload_estimate"), dict
    ):
        estimate = dict(execution_plan.payload["workload_estimate"])
    else:
        snapshot_candles = sum(len(snapshot.candles) for snapshot in snapshots)
        scenario_count = len(
            required_execution_scenarios(manifest.execution_model.scenarios)
        )
        split_count = len(snapshots)
        work_unit_count = len(candidates) * scenario_count
        estimated_tick_events = snapshot_candles * len(candidates) * scenario_count
        audit_mode = manifest.research_run.audit_trail.mode
        full_decisions = (
            manifest.research_run.artifact_policy.full_decisions_external_jsonl
        )
        estimated_audit_stream_rows = (
            snapshot_candles * len(candidates) * scenario_count * 3
            if audit_mode == "complete_external"
            else 0
        )
        estimated_artifact_write_count = (
            3
            + work_unit_count
            + (
                1 + work_unit_count * split_count * 3
                if audit_mode == "complete_external"
                else 0
            )
            + (work_unit_count * split_count if full_decisions else 0)
        )
        estimated_hash_payload_bytes = (
            snapshot_candles * 128 + work_unit_count * split_count * 512 + 4096
        )
        pre_parallel_dataset_hash_payload_bytes = (
            snapshot_candles * 128 + split_count * 2048
        )
        estimate = {
            "schema_version": 2,
            "candidate_count": len(candidates),
            "scenario_count": scenario_count,
            "split_count": split_count,
            "walk_forward_window_count": sum(
                1 for snapshot in snapshots if snapshot.split_name.startswith("window_")
            )
            // 2,
            "estimated_strategy_runs": _estimated_strategy_runs(
                candidate_count=len(candidates),
                scenario_count=scenario_count,
                split_count=split_count,
                include_walk_forward=report_kind == "walk_forward",
                walk_forward_split_count=sum(
                    1
                    for snapshot in snapshots
                    if snapshot.split_name.startswith("window_")
                ),
            ),
            "estimated_tick_events": estimated_tick_events,
            "approx_snapshot_candle_count": snapshot_candles,
            "audit_mode": audit_mode,
            "report_detail": manifest.research_run.report_detail,
            "full_decisions_external_jsonl": full_decisions,
            "estimated_audit_stream_rows": estimated_audit_stream_rows,
            "estimated_artifact_write_count": estimated_artifact_write_count,
            "estimated_hash_payload_bytes": estimated_hash_payload_bytes,
            "pre_parallel_work_unit_count": work_unit_count,
            "pre_parallel_split_hash_count": split_count,
            "pre_parallel_dataset_hash_payload_bytes": pre_parallel_dataset_hash_payload_bytes,
            "pre_parallel_dataset_hash_call_count": split_count,
            "pre_parallel_parent_serial_estimate_status": "precomputed_split_hashes",
            "estimated_artifact_bytes": _estimated_artifact_bytes(
                candidate_count=len(candidates),
                scenario_count=scenario_count,
                split_count=split_count,
                audit_mode=audit_mode,
                estimated_audit_stream_rows=estimated_audit_stream_rows,
                estimated_artifact_write_count=estimated_artifact_write_count,
                estimated_hash_payload_bytes=estimated_hash_payload_bytes,
                full_decisions_external_jsonl=full_decisions,
            ),
            "estimated_snapshot_hash_count": split_count,
            "uses_validation_evaluator": None,
            "uses_real_parallel_executor": None,
        }
    if isinstance(execution_observability, dict):
        estimate["uses_validation_evaluator"] = bool(
            execution_observability.get("validation_evaluator_used")
        )
        estimate["uses_real_parallel_executor"] = bool(
            execution_observability.get("parallel_executor_used")
        )
    return estimate


def _subprocess_candidate_isolation_status(
    execution_observability: dict[str, Any] | None,
) -> str:
    if not isinstance(execution_observability, dict):
        return "subprocess_candidate_isolation_missing"
    work_units = execution_observability.get("work_units")
    if not isinstance(work_units, list) or not work_units:
        return "subprocess_candidate_isolation_missing"
    for item in work_units:
        if not isinstance(item, dict):
            return "subprocess_candidate_isolation_missing"
        evidence = item.get("worker_process_evidence")
        if not isinstance(evidence, dict):
            return "subprocess_candidate_isolation_missing"
        required = (
            "worker_pid",
            "command_or_callable_identity",
            "input_hash",
            "output_hash",
            "exit_status",
            "resource_status",
            "terminal_audit_trace_status",
        )
        if any(evidence.get(field) in (None, "") for field in required):
            return "subprocess_candidate_isolation_missing"
    return "worker_process_evidence_present"


def _primary_base_cost_assumption(candidate: dict[str, Any]) -> dict[str, Any] | None:
    scenario_results = candidate.get("scenario_results")
    if not isinstance(scenario_results, list):
        return None
    for result in scenario_results:
        if not isinstance(result, dict) or result.get("scenario_role") != "base":
            continue
        assumption = result.get("cost_assumption")
        return dict(assumption) if isinstance(assumption, dict) else None
    return None


def _validation_blocking_reasons(
    *,
    best: dict[str, Any] | None,
    statistical_required: bool,
    statistical_evidence: dict[str, Any] | None,
    report: dict[str, Any] | None = None,
) -> list[str]:
    reasons: list[str] = []
    if best is None:
        reasons.append("candidate_acceptance_gate_failed")
    if isinstance(report, dict) and report.get("final_selection_required"):
        if report.get("final_selection_gate_result") != "PASS":
            reasons.extend(
                str(item) for item in report.get("final_selection_fail_reasons") or []
            )
            reasons.append("final_selection_gate_not_passed")
    if isinstance(report, dict) and report.get("benchmark_suite_required"):
        if best is None or best.get("benchmark_suite_gate_result") != "PASS":
            reasons.extend(
                str(item)
                for item in (best or {}).get("benchmark_suite_fail_reasons") or []
            )
            reasons.append("benchmark_suite_gate_not_passed")
    if statistical_required:
        if not isinstance(statistical_evidence, dict):
            reasons.append("statistical_evidence_missing")
        elif best is not None and isinstance(report, dict):
            reasons.extend(
                validate_statistical_evidence_for_candidate(
                    candidate=best,
                    report=report,
                    evidence=statistical_evidence,
                    require_experiment_registry_binding=False,
                )
            )
        elif statistical_evidence.get("statistical_gate_result") != "PASS":
            reasons.extend(
                str(item)
                for item in statistical_evidence.get("gate_fail_reasons") or []
            )
        if (
            statistical_required
            and isinstance(statistical_evidence, dict)
            and not any(
                str(reason).startswith("statistical_")
                or str(reason).startswith("return_panel")
                for reason in reasons
            )
            and statistical_evidence.get("statistical_gate_result") != "PASS"
        ):
            reasons.append("statistical_selection_failed")
    return sorted(set(reasons))


def _attach_statistical_selection_to_candidates(
    *,
    candidates: list[dict[str, Any]],
    required: bool,
    contract: dict[str, Any],
    selection_hash: str,
    evidence: dict[str, Any] | None,
    evidence_path: Path | None,
) -> StatisticalSelectionAttachmentObservability:
    evidence_hash = evidence.get("content_hash") if isinstance(evidence, dict) else None
    gate_result = (
        evidence.get("statistical_gate_result") if isinstance(evidence, dict) else None
    )
    gate_reasons = (
        evidence.get("gate_fail_reasons") if isinstance(evidence, dict) else []
    )
    p_value = (
        evidence.get("white_reality_check_p_value")
        if isinstance(evidence, dict)
        else None
    )
    summary_p_value = (
        evidence.get("summary_metric_max_bootstrap_p_value")
        if isinstance(evidence, dict)
        else None
    )
    effective_trial_count = (
        evidence.get("effective_trial_count") if isinstance(evidence, dict) else None
    )
    candidate_metric_values_hash = (
        evidence.get("candidate_metric_values_hash")
        if isinstance(evidence, dict)
        else None
    )
    candidate_metric_values_summary = (
        evidence.get("candidate_metric_values_summary")
        if isinstance(evidence, dict)
        else None
    )
    metric_value_count = (
        evidence.get("metric_value_count") if isinstance(evidence, dict) else None
    )
    missing_metric_count = (
        evidence.get("missing_metric_count") if isinstance(evidence, dict) else None
    )
    method = (
        evidence.get("white_reality_check_method")
        if isinstance(evidence, dict)
        else None
    )
    evidence_grade = (
        evidence.get("evidence_grade") if isinstance(evidence, dict) else None
    )
    statistical_method = (
        evidence.get("statistical_method") if isinstance(evidence, dict) else None
    )
    return_panel_hash = (
        evidence.get("return_panel_hash") if isinstance(evidence, dict) else None
    )
    return_panel_path = (
        evidence.get("return_panel_path") if isinstance(evidence, dict) else None
    )
    return_unit = evidence.get("return_unit") if isinstance(evidence, dict) else None
    return_panel_observation_count = (
        evidence.get("return_panel_observation_count")
        if isinstance(evidence, dict)
        else None
    )
    bootstrap_sampling_contract_hash = (
        evidence.get("bootstrap_sampling_contract_hash")
        if isinstance(evidence, dict)
        else None
    )
    family_trial_registry_path = (
        evidence.get("family_trial_registry_path")
        if isinstance(evidence, dict)
        else None
    )
    family_trial_registry_prior_hash = (
        evidence.get("family_trial_registry_prior_hash")
        if isinstance(evidence, dict)
        else None
    )
    family_trial_registry_row_hash = (
        evidence.get("family_trial_registry_row_hash")
        if isinstance(evidence, dict)
        else None
    )
    registry_fields = (
        {
            key: evidence.get(key)
            for key in (
                "experiment_registry_path",
                "experiment_registry_prior_hash",
                "experiment_registry_row_hash",
                "experiment_registry_completion_row_hash",
                "experiment_registry_bound_evidence_hash",
                "experiment_registry_evidence_hash_phase",
                "final_holdout_fingerprint",
                "final_holdout_identity_hash",
                "final_holdout_content_hash",
                "final_holdout_reuse_key_hash_v1",
                "final_holdout_reuse_key_hash",
                "final_holdout_reuse_key_schema_version",
                "final_holdout_reuse_key_hash_v2",
                "objective_metric",
                "final_holdout_split_hash",
                "computed_attempt_index",
                "computed_holdout_reuse_count",
                "declared_attempt_index",
                "declared_holdout_reuse_count",
                "hypothesis_identity_source",
                "experiment_family_identity_source",
                "research_freedom_hash",
                "registry_gate_result",
                "registry_gate_fail_reasons",
            )
        }
        if isinstance(evidence, dict)
        else {}
    )
    limitations = (
        evidence.get("statistical_evidence_limitations")
        if isinstance(evidence, dict)
        else []
    )
    official_statistical_evidence_wrc_generation_available = (
        evidence.get("official_statistical_evidence_wrc_generation_available")
        if isinstance(evidence, dict)
        else False
    )
    profile_build_wall_seconds = 0.0
    profile_hash_wall_seconds = 0.0
    profile_hash_observability = _empty_hash_observability()
    for candidate in candidates:
        candidate["statistical_validation_required"] = required
        candidate["statistical_validation_contract"] = contract
        candidate["benchmark"] = (
            evidence.get("benchmark") if isinstance(evidence, dict) else None
        )
        candidate["primary_metric"] = (
            evidence.get("primary_metric") if isinstance(evidence, dict) else None
        )
        candidate["primary_metric_source"] = (
            evidence.get("primary_metric_source")
            if isinstance(evidence, dict)
            else None
        )
        candidate["selection_universe_hash"] = selection_hash
        candidate["candidate_metric_values_hash"] = candidate_metric_values_hash
        candidate["candidate_metric_values_summary"] = candidate_metric_values_summary
        candidate["candidate_count"] = len(candidates)
        candidate["metric_value_count"] = metric_value_count
        candidate["missing_metric_count"] = missing_metric_count
        candidate["statistical_evidence_hash"] = evidence_hash
        candidate["statistical_evidence_path"] = (
            str(evidence_path.resolve()) if evidence_path is not None else None
        )
        candidate["evidence_grade"] = evidence_grade
        candidate["statistical_method"] = statistical_method
        candidate["return_panel_hash"] = return_panel_hash
        candidate["return_panel_path"] = return_panel_path
        candidate["return_unit"] = return_unit
        candidate["return_panel_observation_count"] = return_panel_observation_count
        candidate["family_trial_registry_path"] = family_trial_registry_path
        candidate["family_trial_registry_prior_hash"] = family_trial_registry_prior_hash
        candidate["family_trial_registry_row_hash"] = family_trial_registry_row_hash
        candidate.update(registry_fields)
        candidate["statistical_gate_result"] = gate_result
        candidate["statistical_gate_fail_reasons"] = (
            list(gate_reasons) if isinstance(gate_reasons, list) else []
        )
        candidate["white_reality_check_p_value"] = p_value
        candidate["summary_metric_max_bootstrap_p_value"] = summary_p_value
        candidate["white_reality_check_method"] = method
        candidate["bootstrap_sampling_contract_hash"] = bootstrap_sampling_contract_hash
        candidate["statistical_evidence_limitations"] = (
            list(limitations) if isinstance(limitations, list) else []
        )
        candidate["official_statistical_evidence_wrc_generation_available"] = bool(
            official_statistical_evidence_wrc_generation_available
        )
        candidate["effective_trial_count"] = effective_trial_count
        candidate.pop("candidate_profile_hash", None)
        profile_build_started = time.perf_counter()
        final_profile = build_candidate_profile(candidate)
        profile_build_wall_seconds += time.perf_counter() - profile_build_started

        profile_hash_started = time.perf_counter()
        with observe_hashing() as profile_hash_observer:
            candidate["candidate_profile_hash"] = sha256_prefixed(
                final_profile,
                label="candidate_profile_hash.post_statistical_profile_hash",
            )
        profile_hash_wall_seconds += time.perf_counter() - profile_hash_started
        _merge_hash_observability(
            profile_hash_observability, profile_hash_observer.as_dict()
        )
    return StatisticalSelectionAttachmentObservability(
        substage_timings=[
            {
                "stage": "candidate_profile_hash.post_statistical_profile_build",
                "wall_seconds": round(profile_build_wall_seconds, 6),
                "candidate_count": len(candidates),
            },
            {
                "stage": "candidate_profile_hash.post_statistical_profile_hash",
                "wall_seconds": round(profile_hash_wall_seconds, 6),
                "candidate_count": len(candidates),
                **profile_hash_observability,
            },
        ],
        candidate_profile_hash_observability=profile_hash_observability,
    )


def _report_base_cost_assumption(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        assumption = candidate.get("base_cost_assumption")
        if isinstance(assumption, dict):
            return dict(assumption)
    return None


def _closed_trade_diagnostics_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    trades = [
        trade
        for trade in candidate.get("validation_closed_trades") or ()
        if isinstance(trade, dict)
    ]
    exit_rule_distribution: dict[str, int] = {}
    holding_by_rule: dict[str, list[float]] = {}
    loss_by_regime: dict[str, float] = {}
    mae_values: list[float] = []
    mfe_values: list[float] = []
    max_loss_trade: dict[str, Any] | None = None
    for trade in trades:
        rule = str(trade.get("exit_rule") or "unknown")
        exit_rule_distribution[rule] = exit_rule_distribution.get(rule, 0) + 1
        holding = _optional_float(trade.get("holding_minutes"))
        if holding is not None:
            holding_by_rule.setdefault(rule, []).append(holding)
        net_pnl = _optional_float(trade.get("net_pnl"))
        if net_pnl is not None and net_pnl < 0.0:
            key = f"{trade.get('entry_regime') or 'unknown'}->{trade.get('exit_regime') or 'unknown'}"
            loss_by_regime[key] = loss_by_regime.get(key, 0.0) + net_pnl
            if max_loss_trade is None or net_pnl < float(
                max_loss_trade.get("net_pnl") or 0.0
            ):
                max_loss_trade = dict(trade)
        mae = _optional_float(trade.get("mae"))
        mfe = _optional_float(trade.get("mfe"))
        if mae is not None:
            mae_values.append(mae)
        if mfe is not None:
            mfe_values.append(mfe)
    top_losing = sorted(
        (
            dict(trade)
            for trade in trades
            if _optional_float(trade.get("net_pnl")) is not None
        ),
        key=lambda item: float(item.get("net_pnl") or 0.0),
    )[:5]
    return {
        "closed_trade_count": len(trades),
        "top_losing_trades": top_losing,
        "exit_rule_distribution": exit_rule_distribution,
        "avg_holding_minutes_by_exit_rule": {
            rule: sum(values) / len(values)
            for rule, values in sorted(holding_by_rule.items())
            if values
        },
        "max_holding_minutes_by_exit_rule": {
            rule: max(values)
            for rule, values in sorted(holding_by_rule.items())
            if values
        },
        "mae_mfe_summary": {
            "mae_min": min(mae_values) if mae_values else None,
            "mae_avg": sum(mae_values) / len(mae_values) if mae_values else None,
            "mfe_max": max(mfe_values) if mfe_values else None,
            "mfe_avg": sum(mfe_values) / len(mfe_values) if mfe_values else None,
        },
        "loss_by_entry_exit_regime": loss_by_regime,
        "max_loss_trade_dependency": max_loss_trade,
        "max_holding_exit_count": exit_rule_distribution.get("max_holding_time", 0),
    }


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _execution_model_from_scenario(
    scenario: ExecutionScenario, *, seed_context: dict[str, Any] | None = None
):
    if scenario.type == "fixed_bps":
        return FixedBpsExecutionModel(
            fee_rate=scenario.fee_rate, slippage_bps=scenario.slippage_bps
        )
    if scenario.type == "stress":
        return StressExecutionModel(
            fee_rate=scenario.fee_rate,
            slippage_bps=scenario.slippage_bps,
            latency_ms=scenario.latency_ms,
            partial_fill_rate=scenario.partial_fill_rate,
            order_failure_rate=scenario.order_failure_rate,
            market_order_extra_cost_bps=scenario.market_order_extra_cost_bps,
            seed=scenario.seed,
            seed_derivation_inputs=seed_context,
        )
    if scenario.type == "depth_walk":
        return DepthWalkExecutionModel(fee_rate=scenario.fee_rate)
    raise ResearchValidationError(
        f"unsupported execution model scenario: {scenario.type}"
    )


def _scenario_payload(scenario: ExecutionScenario) -> dict[str, Any]:
    payload = scenario.as_dict()
    payload["model_params_hash"] = model_params_hash(
        _execution_model_from_scenario(scenario).params_payload()
    )
    return payload


def _scenario_id(scenario: ExecutionScenario, scenario_index: int) -> str:
    digest = model_params_hash(
        _execution_model_from_scenario(scenario).params_payload()
    ).split(":", 1)[-1][:8]
    return f"scenario_{scenario_index + 1:03d}_{scenario.type}_{digest}"


def _seed_context(
    *,
    simulation_seed_scope_hash: str,
    scenario: ExecutionScenario,
    scenario_id: str,
    parameter_candidate_id: str,
    split_name: str,
) -> dict[str, Any]:
    scenario_hash = model_params_hash(
        _execution_model_from_scenario(scenario).params_payload()
    )
    material = {
        "simulation_seed_scope_hash": simulation_seed_scope_hash,
        "scenario_id": scenario_id,
        "scenario_hash": scenario_hash,
        "parameter_candidate_id": parameter_candidate_id,
        "split_name": split_name,
        "base_seed": scenario.seed,
    }
    material["stress_seed_material"] = dict(material)
    material["stress_seed_hash"] = sha256_prefixed(material)
    return material


def _execution_metadata(trades: Any) -> list[dict[str, Any]]:
    metadata: list[dict[str, Any]] = []
    for trade in trades:
        if isinstance(trade, dict) and isinstance(trade.get("execution"), dict):
            item = dict(trade["execution"])
            for key in (
                "record_type",
                "is_execution_attempt",
                "is_filled_trade",
                "is_execution_filled",
                "is_portfolio_applied_trade",
                "is_effective_trade",
                "is_skipped_execution",
                "is_failed_execution",
                "portfolio_effective_ts",
                "portfolio_applied",
                "portfolio_application_status",
                "pending_execution_at_end",
                "pending_execution_after_dataset_end",
                "dataset_final_mark_ts",
            ):
                if key in trade:
                    item[key] = trade[key]
            metadata.append(item)
    return metadata


def _execution_reality_summary(
    *,
    policy,
    execution_metadata: list[dict[str, Any]],
    execution_event_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    coverage = signal_quote_coverage_summary(
        execution_metadata=execution_metadata, policy=policy
    )
    observed_levels = [
        str(item.get("execution_reality_level"))
        for item in execution_metadata
        if item.get("execution_reality_level")
    ]
    sources = [
        str(item.get("fill_reference_source"))
        for item in execution_metadata
        if item.get("fill_reference_source")
    ]
    gate = execution_reality_gate(
        policy=policy,
        observed_levels=observed_levels,
        fill_reference_sources=sources,
        quote_coverage_pct=coverage.get("quote_after_decision_coverage_pct"),
        latency_reference_warnings=[
            str(item.get("latency_reference_policy_warning"))
            for item in execution_metadata
            if item.get("latency_reference_policy_warning")
        ],
    )
    event_summary = execution_event_summary or _execution_event_summary_from_metadata(
        execution_metadata
    )
    depth_summary = _signal_depth_execution_summary(execution_metadata)
    return {
        **coverage,
        **depth_summary,
        **event_summary,
        "execution_reality_gate_status": gate["status"],
        "execution_reality_gate_reasons": gate["reasons"],
        "execution_reality_gate": gate,
    }


def _signal_depth_execution_summary(
    execution_metadata: list[dict[str, Any]],
) -> dict[str, Any]:
    depth_walk = [
        item
        for item in execution_metadata
        if item.get("model_name") == "depth_walk"
        or item.get("execution_liquidity_evidence_type")
        == "l2_depth_walk_queue_unaware"
    ]
    if not depth_walk:
        return {
            "depth_walk_execution_model_used": False,
            "signal_level_depth_coverage_pct": None,
            "signal_level_depth_coverage_status": "not_requested",
            "depth_liquidity_sufficiency_status": "not_requested",
            "depth_full_fill_count": 0,
            "depth_partial_fill_count": 0,
            "depth_unfilled_count": 0,
            "depth_missing_snapshot_count": 0,
            "depth_evidence_refs": [],
        }
    available = [
        item
        for item in depth_walk
        if bool(item.get("depth_available"))
        and item.get("depth_snapshot_ts") is not None
    ]
    coverage_pct = (len(available) / len(depth_walk) * 100.0) if depth_walk else 0.0
    partial = [item for item in depth_walk if item.get("fill_status") == "partial"]
    unfilled = [
        item for item in depth_walk if item.get("fill_status") in {"unfilled", "failed"}
    ]
    missing = [
        item
        for item in depth_walk
        if item.get("execution_reference_failure_reason")
        == "depth_snapshot_missing_for_depth_walk"
    ]
    insufficient = [
        item for item in depth_walk if item.get("depth_sufficient") is False
    ]
    if missing:
        sufficiency_status = "missing_depth"
    elif insufficient:
        sufficiency_status = "insufficient_depth"
    else:
        sufficiency_status = "sufficient_depth"
    return {
        "depth_walk_execution_model_used": True,
        "signal_level_depth_coverage_pct": round(coverage_pct, 8),
        "signal_level_depth_coverage_status": "PASS"
        if coverage_pct == 100.0
        else "FAIL",
        "depth_liquidity_sufficiency_status": sufficiency_status,
        "depth_full_fill_count": sum(
            1 for item in depth_walk if item.get("fill_status") == "filled"
        ),
        "depth_partial_fill_count": len(partial),
        "depth_unfilled_count": len(unfilled),
        "depth_missing_snapshot_count": len(missing),
        "depth_evidence_refs": sorted(
            {
                str(item.get("orderbook_depth_ref"))
                for item in available
                if item.get("orderbook_depth_ref")
            }
        ),
    }


def _execution_event_summary_from_metadata(
    execution_metadata: list[dict[str, Any]],
) -> dict[str, object]:
    filled = [
        item for item in execution_metadata if bool(item.get("is_execution_filled"))
    ]
    portfolio_applied = [
        item
        for item in execution_metadata
        if bool(item.get("is_portfolio_applied_trade"))
    ]
    pending = [
        item
        for item in execution_metadata
        if bool(item.get("is_execution_filled"))
        and not bool(item.get("is_portfolio_applied_trade"))
    ]
    skipped = [
        item for item in execution_metadata if bool(item.get("is_skipped_execution"))
    ]
    failed = [
        item for item in execution_metadata if bool(item.get("is_failed_execution"))
    ]
    closed = [
        item
        for item in portfolio_applied
        if str(item.get("side") or "").upper() == "SELL"
    ]
    pending_at_end = [
        item for item in pending if bool(item.get("pending_execution_at_end"))
    ]
    pending_after_end = [
        item
        for item in pending
        if bool(item.get("pending_execution_after_dataset_end"))
    ]
    return {
        "execution_attempt_count": len(execution_metadata),
        "execution_filled_count": len(filled),
        "filled_execution_count": len(filled),
        "portfolio_applied_trade_count": len(portfolio_applied),
        "pending_execution_count": len(pending),
        "skipped_execution_count": len(skipped),
        "failed_execution_count": len(failed),
        "closed_trade_count": len(closed),
        "pending_execution_at_end_count": len(pending_at_end),
        "pending_execution_after_dataset_end_count": len(pending_after_end),
        "execution_event_timeline_incomplete": bool(pending_after_end),
    }


def _execution_event_gate_reasons(summary: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if bool(summary.get("execution_event_timeline_incomplete")):
        reasons.append("execution_event_timeline_incomplete")
    if int(summary.get("pending_execution_after_dataset_end_count") or 0) > 0:
        reasons.append("pending_execution_after_dataset_end")
    return reasons


def _report_execution_reality_level(candidates: list[dict[str, Any]]) -> str | None:
    for candidate in candidates:
        summary = candidate.get("execution_reality_summary")
        if isinstance(summary, dict) and summary.get("execution_reality_level"):
            return str(summary["execution_reality_level"])
    return None


def _report_execution_reality_gate_status(candidates: list[dict[str, Any]]) -> str:
    statuses = {
        str(summary.get("execution_reality_gate_status"))
        for candidate in candidates
        if isinstance((summary := candidate.get("execution_reality_summary")), dict)
    }
    if "FAIL" in statuses:
        return "FAIL"
    if "PASS" in statuses:
        return "PASS"
    return "UNKNOWN"


def _report_execution_reality_gate_reasons(
    candidates: list[dict[str, Any]],
) -> list[str]:
    return sorted(
        {
            str(reason)
            for candidate in candidates
            if isinstance((summary := candidate.get("execution_reality_summary")), dict)
            for reason in summary.get("execution_reality_gate_reasons") or []
        }
    )


def _report_signal_quote_coverage_summary(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for candidate in candidates:
        summary = candidate.get("execution_reality_summary")
        if isinstance(summary, dict):
            return {
                key: summary.get(key)
                for key in (
                    "signal_event_count",
                    "fillable_signal_event_count",
                    "missing_quote_on_signal_count",
                    "skipped_execution_signal_count",
                    "missing_quote_warning_count",
                    "quote_after_decision_coverage_pct",
                    "median_quote_age_ms_on_signal",
                    "p95_quote_age_ms_on_signal",
                    "execution_reference_policy",
                    "execution_reality_level",
                    "latency_applied_to_submit_ts_count",
                    "latency_applied_to_fill_reference_count",
                    "depth_walk_execution_model_used",
                    "signal_level_depth_coverage_pct",
                    "signal_level_depth_coverage_status",
                    "depth_liquidity_sufficiency_status",
                    "depth_full_fill_count",
                    "depth_partial_fill_count",
                    "depth_unfilled_count",
                    "depth_missing_snapshot_count",
                    "depth_evidence_refs",
                    "execution_attempt_count",
                    "execution_filled_count",
                    "filled_execution_count",
                    "portfolio_applied_trade_count",
                    "pending_execution_count",
                    "skipped_execution_count",
                    "failed_execution_count",
                    "closed_trade_count",
                    "pending_execution_at_end_count",
                    "pending_execution_after_dataset_end_count",
                    "execution_event_timeline_incomplete",
                )
            }
    return None


def _report_signal_depth_summary(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    for candidate in candidates:
        summary = candidate.get("execution_reality_summary")
        if isinstance(summary, dict) and bool(
            summary.get("depth_walk_execution_model_used")
        ):
            return {
                key: summary.get(key)
                for key in (
                    "depth_walk_execution_model_used",
                    "signal_level_depth_coverage_pct",
                    "signal_level_depth_coverage_status",
                    "depth_liquidity_sufficiency_status",
                    "depth_full_fill_count",
                    "depth_partial_fill_count",
                    "depth_unfilled_count",
                    "depth_missing_snapshot_count",
                    "depth_evidence_refs",
                )
            }
    return {
        "depth_walk_execution_model_used": False,
        "signal_level_depth_coverage_pct": None,
        "signal_level_depth_coverage_status": "not_requested",
        "depth_liquidity_sufficiency_status": "not_requested",
        "depth_full_fill_count": 0,
        "depth_partial_fill_count": 0,
        "depth_unfilled_count": 0,
        "depth_missing_snapshot_count": 0,
        "depth_evidence_refs": [],
    }


def _report_execution_event_summary(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for candidate in candidates:
        summary = candidate.get("execution_event_summary")
        if isinstance(summary, dict):
            return dict(summary)
        reality_summary = candidate.get("execution_reality_summary")
        if isinstance(reality_summary, dict):
            return {
                key: reality_summary.get(key)
                for key in (
                    "execution_attempt_count",
                    "execution_filled_count",
                    "filled_execution_count",
                    "portfolio_applied_trade_count",
                    "pending_execution_count",
                    "skipped_execution_count",
                    "failed_execution_count",
                    "closed_trade_count",
                    "pending_execution_at_end_count",
                    "pending_execution_after_dataset_end_count",
                    "execution_event_timeline_incomplete",
                )
            }
    return None


def _resource_integrity_summary(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(candidates)
    computed = [
        candidate
        for candidate in candidates
        if candidate.get("metrics_v2_source") == "computed"
    ]
    fallback = [
        candidate
        for candidate in candidates
        if candidate.get("metrics_v2_source") == "failure_fallback"
    ]
    resource_limited = [
        candidate for candidate in candidates if _candidate_resource_limited(candidate)
    ]
    by_split: dict[str, int] = {}
    for candidate in candidates:
        resource_guard = candidate.get("resource_guard")
        reasons = candidate.get("resource_integrity_fail_reasons") or []
        guard_reasons = (
            resource_guard.get("reasons") if isinstance(resource_guard, dict) else []
        )
        if "max_runtime_exceeded" not in {
            str(reason) for reason in list(reasons) + list(guard_reasons or [])
        }:
            continue
        split = (
            str(
                resource_guard.get("split")
                or candidate.get("failed_split")
                or "unknown"
            )
            if isinstance(resource_guard, dict)
            else str(candidate.get("failed_split") or "unknown")
        )
        by_split[split] = by_split.get(split, 0) + 1
    slowest = sorted(
        (
            (
                _candidate_elapsed_s(candidate),
                str(
                    candidate.get("parameter_candidate_id")
                    or candidate.get("candidate_id")
                    or ""
                ),
            )
            for candidate in candidates
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    return {
        "computed_candidate_count": len(computed),
        "failure_fallback_candidate_count": len(fallback),
        "resource_limited_candidate_count": len(resource_limited),
        "max_runtime_exceeded_count": sum(by_split.values()),
        "max_runtime_exceeded_by_split": dict(sorted(by_split.items())),
        "computed_candidate_ratio": (len(computed) / total) if total else 0.0,
        "slowest_candidate_ids": [
            candidate_id for _, candidate_id in slowest[:5] if candidate_id
        ],
    }


def _candidate_resource_limited(candidate: dict[str, Any]) -> bool:
    if candidate.get("evaluation_status") == "resource_limited":
        return True
    if candidate.get("failure_reason") == "candidate_resource_limit_exceeded":
        return True
    reasons = set(
        str(item) for item in candidate.get("resource_integrity_fail_reasons") or []
    )
    guard = candidate.get("resource_guard")
    if isinstance(guard, dict):
        reasons.update(str(item) for item in guard.get("reasons") or [])
    return "max_runtime_exceeded" in reasons or any(
        reason in RESOURCE_INTEGRITY_REASON_CODES for reason in reasons
    )


def _candidate_elapsed_s(candidate: dict[str, Any]) -> float:
    values: list[float] = []
    for key in (
        "train_resource_usage",
        "validation_resource_usage",
        "final_holdout_resource_usage",
        "resource_guard",
    ):
        payload = candidate.get(key)
        if not isinstance(payload, dict):
            continue
        value = payload.get("elapsed_s")
        if value is not None:
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                pass
    return max(values, default=0.0)


def _top_level_classification(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    simulation_reasons: set[str] = set()
    resource_reasons: set[str] = set()
    performance_reasons: set[str] = set()
    declaration_reasons: set[str] = set()
    computed_count = 0
    not_evaluated_count = 0
    for candidate in candidates:
        scopes = [candidate]
        scopes.extend(
            item
            for item in candidate.get("scenario_results") or []
            if isinstance(item, dict)
        )
        if _is_computed_candidate(candidate):
            computed_count += 1
        elif _candidate_metrics_not_evaluated(candidate):
            not_evaluated_count += 1
        for item in scopes:
            simulation_reasons.update(
                str(reason)
                for reason in item.get("simulation_integrity_fail_reasons") or []
            )
            resource_reasons.update(
                str(reason)
                for reason in item.get("resource_integrity_fail_reasons") or []
            )
            declaration_reasons.update(
                str(reason)
                for reason in item.get("candidate_eligibility_reasons") or []
            )
            if _is_computed_candidate(candidate):
                performance_reasons.update(
                    str(reason)
                    for reason in item.get("strategy_performance_fail_reasons") or []
                )
    if performance_reasons:
        performance_status = "FAIL"
    elif computed_count:
        performance_status = "PASS"
    elif not_evaluated_count:
        performance_status = "NOT_EVALUATED"
    else:
        performance_status = "SKIPPED"
    return {
        "simulation_integrity_status": "FAIL" if simulation_reasons else "PASS",
        "simulation_integrity_fail_reasons": sorted(simulation_reasons),
        "resource_integrity_status": "FAIL" if resource_reasons else "PASS",
        "resource_integrity_fail_reasons": sorted(resource_reasons),
        "strategy_performance_gate_status": performance_status,
        "strategy_performance_fail_reasons": sorted(performance_reasons),
        "candidate_eligibility_status": "FAIL" if declaration_reasons else "PASS",
        "candidate_eligibility_reasons": sorted(declaration_reasons),
    }


def _is_computed_candidate(candidate: dict[str, Any]) -> bool:
    return (
        candidate.get("metrics_v2_source") == "computed"
        and candidate.get("candidate_failed_before_complete_metrics") is False
        and candidate.get("evaluation_status") == "completed"
        and candidate.get("metrics_status") == "complete"
    )


def _resource_budget_report(manifest: ExperimentManifest) -> dict[str, Any]:
    limits = manifest.research_run.resource_limits
    raw_research_run = (
        manifest.raw.get("research_run") if isinstance(manifest.raw, dict) else None
    )
    raw_limits = (
        raw_research_run.get("resource_limits")
        if isinstance(raw_research_run, dict)
        else None
    )
    manifest_override = (
        isinstance(raw_limits, dict)
        and "max_runtime_s_per_candidate_split" in raw_limits
    )
    override_reason = (
        raw_limits.get("override_reason") if isinstance(raw_limits, dict) else None
    )
    source = "manifest" if manifest_override else "default"
    return {
        "applied_limits": {
            "max_runtime_s_per_candidate_split": limits.max_runtime_s_per_candidate_split,
            "max_trades": limits.max_trades,
            "max_rss_mb": limits.max_rss_mb,
        },
        "authority": "research_run.resource_limits",
        "override_source": source,
        "override_reason": override_reason,
    }


def _resource_budget_warnings(manifest: ExperimentManifest) -> list[str]:
    raw_research_run = (
        manifest.raw.get("research_run") if isinstance(manifest.raw, dict) else None
    )
    raw_limits = (
        raw_research_run.get("resource_limits")
        if isinstance(raw_research_run, dict)
        else None
    )
    if not isinstance(raw_limits, dict):
        return []
    if "max_runtime_s_per_candidate_split" in raw_limits and not raw_limits.get(
        "override_reason"
    ):
        return ["resource_budget_override_reason_missing"]
    return []


def _base_report_scenario(manifest: ExperimentManifest) -> ExecutionScenario:
    for scenario in manifest.execution_model.scenarios:
        if scenario.scenario_role == "base":
            return scenario
    return manifest.execution_model.scenarios[0]


def _execution_reality_contract(
    *,
    manifest: ExperimentManifest,
    scenario: ExecutionScenario,
    calibration_hash: object | None,
    top_of_book_available: bool,
    depth_available: bool = False,
) -> dict[str, Any]:
    top = manifest.dataset.top_of_book
    cost = (
        scenario.cost_assumption.as_dict()
        if scenario.cost_assumption is not None
        else {}
    )
    latency_model: dict[str, Any] = {
        "type": scenario.type,
        "latency_ms": int(scenario.latency_ms),
    }
    partial_fill_model: dict[str, Any] = {
        "type": scenario.type,
        "partial_fill_rate": float(scenario.partial_fill_rate),
    }
    order_failure_model: dict[str, Any] = {
        "type": scenario.type,
        "order_failure_rate": float(scenario.order_failure_rate),
    }
    depth_walk_used = scenario.type == "depth_walk"
    evidence_tier = "l2_depth_walk_no_queue" if depth_walk_used else None
    limitations = [
        "top_of_book_is_quote_evidence_not_liquidity_depth",
        "full_orderbook_depth_unavailable",
        "queue_position_unavailable",
        "trade_ticks_unavailable",
        "market_impact_model_unavailable",
        "intra_candle_path_reconstruction_unavailable",
    ]
    if depth_walk_used:
        limitations.extend(
            [
                "l2_depth_snapshot_available_for_depth_walk"
                if depth_available
                else "l2_depth_snapshot_unavailable_for_depth_walk",
                "l2_depth_walk_queue_unaware",
            ]
        )
    if top is None:
        limitations.append("top_of_book_not_requested")
    return build_execution_reality_contract(
        fill_reference_policy=manifest.execution_timing.fill_reference_policy,
        decision_guard_ms=manifest.execution_timing.decision_guard_ms,
        max_quote_wait_ms=manifest.execution_timing.max_quote_wait_ms,
        missing_quote_policy=manifest.execution_timing.missing_quote_policy,
        min_execution_reality_level_for_validation=manifest.execution_timing.min_execution_reality_level_for_validation,
        allow_same_candle_close_fill=manifest.execution_timing.allow_same_candle_close_fill,
        quote_source=(top.quote_source if top is not None else None),
        quote_age_limit_ms=(
            top.join_tolerance_ms
            if top is not None
            else manifest.execution_timing.max_quote_wait_ms
        ),
        top_of_book_required=bool(top.required) if top is not None else False,
        top_of_book_is_full_depth=False,
        depth_required=bool(
            manifest.execution_timing.depth_required or depth_walk_used
        ),
        trade_tick_required=manifest.execution_timing.trade_tick_required,
        queue_position_required=manifest.execution_timing.queue_position_required,
        market_impact_required=manifest.execution_timing.market_impact_required,
        intra_candle_path_available=False,
        latency_model=latency_model,
        partial_fill_model=partial_fill_model,
        order_failure_model=order_failure_model,
        fee_source=cost.get("fee_source"),
        slippage_source=cost.get("slippage_source"),
        calibration_required=manifest.execution_model.calibration_required,
        calibration_artifact_hash=(
            str(calibration_hash)
            if isinstance(calibration_hash, str)
            and calibration_hash.startswith("sha256:")
            else None
        ),
        execution_reality_level=evidence_tier,
        limitations=limitations,
        extra={
            "quote_evidence_available": bool(top_of_book_available),
            "depth_available": bool(depth_available),
            "depth_available_semantics": (
                "stored_l2_depth_complete_snapshots_available_for_depth_walk"
                if depth_walk_used
                else "stored_l2_depth_complete_snapshots_exist_not_execution_model_used"
            ),
            "depth_evidence_available": bool(depth_available),
            "l2_depth_evidence_available": bool(depth_available),
            "l2_depth_snapshot_available": bool(depth_available and depth_walk_used),
            "l2_depth_complete_snapshots_available": bool(depth_available),
            "depth_walk_execution_model_available": True,
            "depth_walk_execution_model_used": bool(depth_walk_used),
            "full_orderbook_depth_available": False,
            "trade_ticks_available": False,
            "queue_position_available": False,
            "market_impact_model_available": False,
            "intra_candle_path_required": manifest.execution_timing.intra_candle_path_required,
            "research_classification": manifest.research_classification,
            "scenario_role": scenario.scenario_role,
            "scenario_type": scenario.type,
        },
    )


def _execution_capability_contract_from_reality(
    contract: dict[str, Any],
) -> dict[str, Any]:
    capability = contract.get("execution_capability_contract")
    if isinstance(capability, dict):
        return dict(capability)
    return build_execution_capability_contract(
        fill_reference_policy=str(
            contract.get("fill_reference_policy") or "candle_close_legacy"
        ),
        top_of_book_required=bool(contract.get("top_of_book_required")),
        top_of_book_available=bool(contract.get("quote_evidence_available")),
        top_of_book_is_full_depth=bool(contract.get("top_of_book_is_full_depth")),
        l2_depth_snapshot_required=bool(contract.get("depth_required")),
        full_orderbook_depth_required=False,
        trade_ticks_required=bool(contract.get("trade_tick_required")),
        queue_position_required=bool(contract.get("queue_position_required")),
        market_impact_model_required=bool(contract.get("market_impact_required")),
        intra_candle_path_required=bool(contract.get("intra_candle_path_required")),
        l2_depth_snapshot_available=bool(
            contract.get("l2_depth_snapshot_available", contract.get("depth_available"))
        ),
        full_orderbook_depth_available=bool(
            contract.get("full_orderbook_depth_available")
        ),
        trade_ticks_available=bool(contract.get("trade_ticks_available")),
        queue_position_available=bool(contract.get("queue_position_available")),
        market_impact_model_available=bool(
            contract.get("market_impact_model_available")
        ),
        intra_candle_path_available=bool(contract.get("intra_candle_path_available")),
        evidence_tier=str(contract.get("execution_reality_level") or "unknown"),
        limitations=list(contract.get("limitations") or []),
    )


def _policy_intra_candle_limitation(fill_reference_policy: str) -> str:
    if fill_reference_policy == "next_candle_open":
        return "next_candle_open_no_intracandle_path"
    if fill_reference_policy in {
        "first_orderbook_after_decision",
        "latency_adjusted_orderbook",
    }:
        return "top_of_book_snapshot_no_depth_no_queue"
    return "same_candle_close_legacy_no_intracandle_path"


def _execution_calibration_warning_reasons(candidate: dict[str, Any]) -> list[str]:
    if candidate.get("execution_calibration_required"):
        return []
    if candidate.get("execution_calibration_strictness") != "warn":
        return []
    gate = candidate.get("execution_calibration_gate")
    if not isinstance(gate, dict) or gate.get("status") == "PASS":
        return []
    return [
        str(reason)
        for reason in gate.get("reasons") or ["execution_calibration_failed"]
    ]


def _benchmark_metrics_for_splits(
    snapshots: dict[str, DatasetSnapshot] | tuple[DatasetSnapshot, ...],
    *,
    manifest: ExperimentManifest,
    strategy_registry: StrategyRegistry,
    candidates: list[dict[str, Any]],
    manager: ResearchPathManager | None = None,
) -> dict[str, dict[str, Any]]:
    return BenchmarkSuiteRunner(
        manifest=manifest,
        strategy_registry=strategy_registry,
        manager=manager,
    ).run(snapshots, candidates=candidates)


def _attach_benchmark_metrics(
    *,
    candidates: list[dict[str, Any]],
    benchmark_metrics: dict[str, dict[str, Any]],
    required: bool,
) -> None:
    for candidate in candidates:
        candidate_benchmarks: dict[str, dict[str, float | None]] = {}
        candidate_fail_reasons: list[str] = []
        for split_name in ("validation", "final_holdout"):
            split_metrics = benchmark_metrics.get(split_name)
            if not isinstance(split_metrics, dict):
                continue
            candidate_metrics = candidate.get(f"{split_name}_metrics")
            return_pct = None
            if isinstance(candidate_metrics, dict):
                return_pct = _finite_float_or_none(candidate_metrics.get("return_pct"))
            buy_hold = _finite_float_or_none(
                split_metrics.get("buy_and_hold_return_pct")
            )
            cash = _finite_float_or_none(split_metrics.get("cash_return_pct")) or 0.0
            excess_buy_hold = (
                None
                if return_pct is None or buy_hold is None
                else round(return_pct - buy_hold, 12)
            )
            excess_cash = None if return_pct is None else round(return_pct - cash, 12)
            payload = {
                "cash_return_pct": cash,
                "buy_and_hold_return_pct": buy_hold,
                "excess_return_vs_cash_pct": excess_cash,
                "excess_return_vs_buy_and_hold_pct": excess_buy_hold,
                "buy_and_hold_method": split_metrics.get("buy_and_hold_method"),
                "benchmark_execution_contract_hash": split_metrics.get(
                    "benchmark_execution_contract_hash"
                ),
                "buy_and_hold_metrics_hash": split_metrics.get(
                    "buy_and_hold_metrics_hash"
                ),
            }
            random_entry = split_metrics.get("random_entry")
            if isinstance(random_entry, dict):
                payload["random_entry"] = random_entry
                payload["excess_return_vs_random_entry_median_pct"] = (
                    None
                    if return_pct is None
                    or _finite_float_or_none(random_entry.get("return_pct_median"))
                    is None
                    else round(
                        return_pct - float(random_entry["return_pct_median"]), 12
                    )
                )
                if random_entry.get("status") != "PASS":
                    candidate_fail_reasons.extend(
                        str(item) for item in random_entry.get("fail_reasons") or []
                    )
            same_holding_by_candidate = split_metrics.get(
                "same_holding_period_by_candidate"
            )
            same_holding = (
                same_holding_by_candidate.get(
                    str(candidate.get("parameter_candidate_id") or "")
                )
                if isinstance(same_holding_by_candidate, dict)
                else None
            )
            if isinstance(same_holding, dict):
                payload["same_holding_period"] = same_holding
                same_holding_return = _finite_float_or_none(
                    same_holding.get("return_pct")
                )
                payload["excess_return_vs_same_holding_period_pct"] = (
                    None
                    if return_pct is None or same_holding_return is None
                    else round(return_pct - same_holding_return, 12)
                )
                if same_holding.get("status") != "PASS":
                    candidate_fail_reasons.extend(
                        str(item) for item in same_holding.get("fail_reasons") or []
                    )
            simpler = split_metrics.get("simpler_strategy")
            if isinstance(simpler, dict):
                payload["simpler_strategy"] = simpler
                simpler_return = _finite_float_or_none(simpler.get("return_pct"))
                payload["excess_return_vs_simpler_strategy_pct"] = (
                    None
                    if return_pct is None or simpler_return is None
                    else round(return_pct - simpler_return, 12)
                )
                if simpler.get("status") != "PASS":
                    candidate_fail_reasons.extend(
                        str(item) for item in simpler.get("fail_reasons") or []
                    )
            approved = split_metrics.get("approved_strategy")
            if isinstance(approved, dict):
                payload["approved_strategy"] = approved
                approved_return = _finite_float_or_none(approved.get("return_pct"))
                payload["excess_return_vs_approved_strategy_pct"] = (
                    None
                    if return_pct is None or approved_return is None
                    else round(return_pct - approved_return, 12)
                )
                if approved.get("status") != "PASS":
                    candidate_fail_reasons.extend(
                        str(item) for item in approved.get("fail_reasons") or []
                    )
            candidate_benchmarks[split_name] = payload
            if isinstance(candidate_metrics, dict):
                candidate_metrics["benchmark_cash_return_pct"] = cash
                candidate_metrics["benchmark_buy_and_hold_return_pct"] = buy_hold
                candidate_metrics["excess_return_vs_cash_pct"] = excess_cash
                candidate_metrics["excess_return_vs_buy_and_hold_pct"] = excess_buy_hold
                candidate_metrics["benchmark_buy_and_hold_equity_curve"] = (
                    split_metrics.get("buy_and_hold_equity_curve")
                )
                if isinstance(approved, dict):
                    candidate_metrics["benchmark_configured_return_pct"] = approved.get(
                        "return_pct"
                    )
                    candidate_metrics["benchmark_configured_equity_curve"] = (
                        approved.get("equity_curve")
                    )
        candidate["benchmark_metrics"] = candidate_benchmarks
        if required:
            validation = candidate_benchmarks.get("validation")
            if not isinstance(validation, dict):
                candidate_fail_reasons.append(
                    "benchmark_suite_validation_evidence_missing"
                )
            for required_name in (
                "random_entry",
                "same_holding_period",
                "simpler_strategy",
                "approved_strategy",
            ):
                if not isinstance((validation or {}).get(required_name), dict):
                    candidate_fail_reasons.append(
                        f"benchmark_suite_{required_name}_missing"
                    )
        candidate["benchmark_suite_required"] = required
        candidate["benchmark_suite_fail_reasons"] = sorted(set(candidate_fail_reasons))
        candidate["benchmark_suite_gate_result"] = (
            "FAIL"
            if candidate_fail_reasons
            else ("PASS" if required else "NOT_REQUIRED")
        )


def _finite_float_or_none(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return (
        numeric
        if numeric == numeric and numeric not in {float("inf"), float("-inf")}
        else None
    )


def _candidate_rank_key(
    candidate: dict[str, Any],
) -> tuple[int, int, float, float, int, float, float, float, float, float]:
    passed = 0 if candidate.get("acceptance_gate_result") == "PASS" else 1
    validation = candidate.get("validation_metrics") or {}
    metrics_v2 = (
        candidate.get("validation_metrics_v2")
        if isinstance(candidate.get("validation_metrics_v2"), dict)
        else {}
    )
    return_risk = (
        metrics_v2.get("return_risk")
        if isinstance(metrics_v2.get("return_risk"), dict)
        else {}
    )
    trade_quality = (
        metrics_v2.get("trade_quality")
        if isinstance(metrics_v2.get("trade_quality"), dict)
        else {}
    )
    cost_execution = (
        metrics_v2.get("cost_execution")
        if isinstance(metrics_v2.get("cost_execution"), dict)
        else {}
    )
    open_position_rank = 1 if bool(return_risk.get("open_position_at_end")) else 0
    expectancy = trade_quality.get("expectancy_per_trade_krw")
    fee_drag = cost_execution.get("fee_drag_ratio")
    slippage_drag = cost_execution.get("slippage_drag_ratio")
    cagr = return_risk.get("cagr_pct")
    dependency = trade_quality.get("single_trade_dependency_score")
    stress_score = candidate.get("validation_stress_suite")
    risk_adjusted = (
        stress_score.get("risk_adjusted_score")
        if isinstance(stress_score, dict)
        and isinstance(stress_score.get("risk_adjusted_score"), dict)
        else {}
    )
    calmar = risk_adjusted.get("calmar_ratio")
    return (
        passed,
        open_position_rank,
        float(validation.get("max_drawdown_pct") or 0.0),
        -float(expectancy) if expectancy is not None else 0.0,
        -int(validation.get("trade_count") or 0),
        float(fee_drag) if fee_drag is not None else 0.0,
        float(slippage_drag) if slippage_drag is not None else 0.0,
        -float(calmar) if calmar is not None else 0.0,
        -float(cagr)
        if cagr is not None
        else -float(validation.get("return_pct") or 0.0),
        float(dependency) if dependency is not None else 0.0,
    )


def _require_enough_candles(snapshots: Any) -> None:
    for snapshot in snapshots:
        if len(snapshot.candles) == 0:
            raise ResearchValidationError(
                f"dataset split {snapshot.split_name} has no candles"
            )


def _quality_reports(
    *,
    db_path: str | Path | None,
    snapshots: dict[str, DatasetSnapshot],
) -> dict[str, DatasetQualityReport]:
    return {
        split_name: build_dataset_quality_report(db_path=db_path, snapshot=snapshot)
        for split_name, snapshot in snapshots.items()
    }


def _dataset_adapter_provenance_payload(
    *,
    manifest: ExperimentManifest,
    snapshots: tuple[DatasetSnapshot, ...],
    quality_reports: tuple[DatasetQualityReport, ...],
) -> dict[str, Any]:
    split_reports = {
        str(report.payload.get("split_name")): report.payload
        for report in quality_reports
    }
    return {
        "dataset_source": manifest.dataset.source,
        "snapshot_id": manifest.dataset.snapshot_id,
        "adapter_name": _single_payload_value(split_reports.values(), "adapter_name"),
        "adapter_version": _single_payload_value(
            split_reports.values(), "adapter_version"
        ),
        "source_uri": manifest.dataset.source_uri,
        "source_locator": manifest.dataset.locator,
        "declared_source_content_hash": manifest.dataset.source_content_hash,
        "declared_source_schema_hash": manifest.dataset.source_schema_hash,
        "canonical_snapshot_hash": combined_dataset_fingerprint(snapshots),
        "split_hashes": {
            snapshot.split_name: snapshot.snapshot_fingerprint_hash()
            for snapshot in snapshots
        },
        "snapshot_data_hashes": {
            snapshot.split_name: snapshot.snapshot_data_hash() for snapshot in snapshots
        },
        "snapshot_query_hashes": {
            snapshot.split_name: snapshot.snapshot_query_hash()
            for snapshot in snapshots
        },
        "snapshot_fingerprint_hashes": {
            snapshot.split_name: snapshot.snapshot_fingerprint_hash()
            for snapshot in snapshots
        },
        "artifact_content_hashes": {
            snapshot.split_name: snapshot.artifact_content_hash
            for snapshot in snapshots
        },
        "source_provenance_hashes": {
            snapshot.split_name: snapshot.source_provenance_hash
            for snapshot in snapshots
        },
        "quality_report_hashes": {
            split_name: str(payload.get("content_hash"))
            for split_name, payload in sorted(split_reports.items())
        },
        "source_content_hashes": {
            split_name: payload.get("source_content_hash")
            for split_name, payload in sorted(split_reports.items())
        },
        "source_schema_hashes": {
            split_name: payload.get("source_schema_hash")
            for split_name, payload in sorted(split_reports.items())
        },
        "adapter_provenance_by_split": {
            split_name: payload.get("adapter_provenance") or {}
            for split_name, payload in sorted(split_reports.items())
        },
        "adapter_provenance_hashes": {
            split_name: payload.get("adapter_provenance_hash")
            for split_name, payload in sorted(split_reports.items())
        },
        "top_of_book": manifest.dataset.top_of_book.as_dict()
        if manifest.dataset.top_of_book
        else None,
        "top_of_book_adapter_provenance_hashes": {
            split_name: payload.get("top_of_book_adapter_provenance_hash")
            for split_name, payload in sorted(split_reports.items())
            if payload.get("top_of_book_requested")
        },
        "depth": manifest.dataset.depth.as_dict() if manifest.dataset.depth else None,
        "depth_adapter_provenance_hashes": {
            split_name: payload.get("l2_depth_adapter_provenance_hash")
            for split_name, payload in sorted(split_reports.items())
            if payload.get("l2_depth_requested")
        },
    }


def _single_payload_value(payloads: Any, key: str) -> Any:
    values = sorted(
        {str(payload.get(key)) for payload in payloads if payload.get(key) is not None}
    )
    if len(values) == 1:
        return values[0]
    return values


def _validate_dataset_adapter_provenance(
    *,
    manifest: ExperimentManifest,
    quality_reports: dict[str, DatasetQualityReport],
) -> None:
    if not requires_candidate_validation(manifest.research_classification):
        return
    reasons: list[str] = []
    for split_name, report in sorted(quality_reports.items()):
        payload = report.payload
        adapter_name = str(payload.get("adapter_name") or "")
        adapter_version = str(payload.get("adapter_version") or "")
        source = str(payload.get("dataset_source") or payload.get("source") or "")
        # Validated candidates never infer artifact evidence from mutable
        # source fields.  The compatibility source branch remains explicit for
        # non-artifact adapters that later implement full verification.
        if manifest.dataset.artifact_ref is not None:
            source_content_hash = str(payload.get("artifact_content_hash") or "")
            source_schema_hash = str(payload.get("artifact_schema_hash") or "")
        else:
            source_content_hash = str(payload.get("source_content_hash") or "")
            source_schema_hash = str(payload.get("source_schema_hash") or "")
        adapter_provenance = payload.get("adapter_provenance")
        adapter_provenance_hash = str(payload.get("adapter_provenance_hash") or "")
        canonical_hash = str(
            payload.get("canonical_snapshot_hash")
            or payload.get("dataset_content_hash")
            or ""
        )
        if not adapter_name:
            reasons.append(f"{split_name}:dataset_adapter_name_missing")
        if not adapter_version:
            reasons.append(f"{split_name}:dataset_adapter_version_missing")
        if not source:
            reasons.append(f"{split_name}:dataset_source_missing")
        if not canonical_hash.startswith("sha256:"):
            reasons.append(f"{split_name}:canonical_snapshot_hash_missing")
        verification = _verification_from_quality_payload(payload)
        if verification is None or not verification_allowed(
            classification=manifest.research_classification, result=verification
        ):
            reasons.append(f"{split_name}:dataset_verification_not_verified")
        if not source_content_hash.startswith("sha256:"):
            reasons.append(f"{split_name}:artifact_or_source_content_hash_missing")
        if not source_schema_hash.startswith("sha256:"):
            reasons.append(f"{split_name}:artifact_or_source_schema_hash_missing")
        if not isinstance(adapter_provenance, dict) or not adapter_provenance:
            reasons.append(f"{split_name}:adapter_provenance_missing")
        if not adapter_provenance_hash.startswith("sha256:"):
            reasons.append(f"{split_name}:adapter_provenance_hash_missing")
        elif adapter_provenance_hash != sha256_prefixed(adapter_provenance or {}):
            reasons.append(f"{split_name}:adapter_provenance_hash_mismatch")
        reasons.extend(
            f"{split_name}:{reason}"
            for reason in _locator_contract_reasons(manifest, "dataset")
        )
        reasons.extend(
            _top_of_book_provenance_reasons(
                manifest=manifest, split_name=split_name, payload=payload
            )
        )
        reasons.extend(
            _depth_provenance_reasons(
                manifest=manifest, split_name=split_name, payload=payload
            )
        )
    if reasons:
        raise ResearchValidationError(
            "dataset_adapter_provenance_failed:" + ",".join(reasons)
        )


def _verification_from_quality_payload(
    payload: dict[str, Any],
) -> DatasetVerificationResult | None:
    value = payload.get("verification")
    if not isinstance(value, dict):
        return None
    try:
        return DatasetVerificationResult(
            overall_status=VerificationStatus(value["overall_status"]),
            content_status=VerificationStatus(value["content_status"]),
            expected_content_hash=value.get("expected_content_hash"),
            actual_content_hash=value.get("actual_content_hash"),
            content_method=str(value.get("content_method") or ""),
            schema_status=VerificationStatus(value["schema_status"]),
            expected_schema_hash=value.get("expected_schema_hash"),
            actual_schema_hash=value.get("actual_schema_hash"),
            locator_status=VerificationStatus(value["locator_status"]),
            locator_type=value.get("locator_type"),
            scope_status=VerificationStatus(value["scope_status"]),
            declared_scope=value.get("declared_scope"),
            actual_scope=value.get("actual_scope"),
            adapter_name=str(value.get("adapter_name") or ""),
            adapter_version=str(value.get("adapter_version") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _top_of_book_provenance_reasons(
    *,
    manifest: ExperimentManifest,
    split_name: str,
    payload: dict[str, Any],
) -> list[str]:
    top = manifest.dataset.top_of_book
    if top is None:
        return []
    reasons: list[str] = []
    actual_content = str(payload.get("top_of_book_source_content_hash") or "")
    split_content = str(payload.get("top_of_book_split_content_hash") or "")
    actual_schema = str(payload.get("top_of_book_source_schema_hash") or "")
    provenance = payload.get("top_of_book_adapter_provenance")
    provenance_hash = str(payload.get("top_of_book_adapter_provenance_hash") or "")
    if not top.source_content_hash:
        reasons.append(f"{split_name}:top_of_book_declared_source_content_hash_missing")
    if not top.source_schema_hash:
        reasons.append(f"{split_name}:top_of_book_declared_source_schema_hash_missing")
    if not actual_content.startswith("sha256:"):
        reasons.append(f"{split_name}:top_of_book_source_content_hash_missing")
    if not split_content.startswith("sha256:"):
        reasons.append(f"{split_name}:top_of_book_split_content_hash_missing")
    if not actual_schema.startswith("sha256:"):
        reasons.append(f"{split_name}:top_of_book_source_schema_hash_missing")
    if top.source_content_hash and top.source_content_hash != actual_content:
        reasons.append(f"{split_name}:top_of_book_source_content_hash_mismatch")
    if top.source_schema_hash and top.source_schema_hash != actual_schema:
        reasons.append(f"{split_name}:top_of_book_source_schema_hash_mismatch")
    if not isinstance(provenance, dict) or not provenance:
        reasons.append(f"{split_name}:top_of_book_adapter_provenance_missing")
    elif provenance.get("source_artifact_content_hash") != actual_content:
        reasons.append(
            f"{split_name}:top_of_book_provenance_source_content_hash_mismatch"
        )
    elif provenance.get("source_schema_hash") != actual_schema:
        reasons.append(
            f"{split_name}:top_of_book_provenance_source_schema_hash_mismatch"
        )
    if not provenance_hash.startswith("sha256:"):
        reasons.append(f"{split_name}:top_of_book_adapter_provenance_hash_missing")
    elif provenance_hash != sha256_prefixed(provenance or {}):
        reasons.append(f"{split_name}:top_of_book_adapter_provenance_hash_mismatch")
    try:
        locator = parse_immutable_locator(top.locator)
    except LocatorValidationError:
        locator = None
    if locator is not None:
        if locator.artifact_content_hash != top.source_content_hash:
            reasons.append(
                f"{split_name}:top_of_book_locator_declared_content_hash_mismatch"
            )
        if locator.artifact_content_hash != actual_content:
            reasons.append(
                f"{split_name}:top_of_book_locator_actual_content_hash_mismatch"
            )
    reasons.extend(
        f"{split_name}:{reason}"
        for reason in _locator_contract_reasons(manifest, "top_of_book")
    )
    return reasons


def _depth_provenance_reasons(
    *,
    manifest: ExperimentManifest,
    split_name: str,
    payload: dict[str, Any],
) -> list[str]:
    if not _depth_requested_for_manifest(manifest):
        return []
    depth = manifest.dataset.depth
    reasons: list[str] = []
    actual_content = str(payload.get("l2_depth_source_content_hash") or "")
    actual_schema = str(payload.get("l2_depth_source_schema_hash") or "")
    provenance = payload.get("l2_depth_adapter_provenance")
    provenance_hash = str(payload.get("l2_depth_adapter_provenance_hash") or "")
    if depth is None:
        reasons.append(
            f"{split_name}:depth_spec_missing_for_validation_required_depth_evidence"
        )
    else:
        if not depth.source_content_hash:
            reasons.append(f"{split_name}:depth_declared_source_content_hash_missing")
        if not depth.source_schema_hash:
            reasons.append(f"{split_name}:depth_declared_source_schema_hash_missing")
        if depth.source_content_hash and depth.source_content_hash != actual_content:
            reasons.append(f"{split_name}:depth_source_content_hash_mismatch")
        if depth.source_schema_hash and depth.source_schema_hash != actual_schema:
            reasons.append(f"{split_name}:depth_source_schema_hash_mismatch")
    if not actual_content.startswith("sha256:"):
        reasons.append(f"{split_name}:depth_source_content_hash_missing")
    if not actual_schema.startswith("sha256:"):
        reasons.append(f"{split_name}:depth_source_schema_hash_missing")
    if not isinstance(provenance, dict) or not provenance:
        reasons.append(f"{split_name}:depth_adapter_provenance_missing")
    elif provenance.get("source_artifact_content_hash") != actual_content:
        reasons.append(f"{split_name}:depth_provenance_source_content_hash_mismatch")
    elif provenance.get("source_schema_hash") != actual_schema:
        reasons.append(f"{split_name}:depth_provenance_source_schema_hash_mismatch")
    if not provenance_hash.startswith("sha256:"):
        reasons.append(f"{split_name}:depth_adapter_provenance_hash_missing")
    elif provenance_hash != sha256_prefixed(provenance or {}):
        reasons.append(f"{split_name}:depth_adapter_provenance_hash_mismatch")
    if depth is not None:
        try:
            locator = parse_immutable_locator(depth.locator)
        except LocatorValidationError:
            locator = None
        if locator is not None:
            if locator.artifact_content_hash != depth.source_content_hash:
                reasons.append(
                    f"{split_name}:depth_locator_declared_content_hash_mismatch"
                )
            if locator.artifact_content_hash != actual_content:
                reasons.append(
                    f"{split_name}:depth_locator_actual_content_hash_mismatch"
                )
    reasons.extend(
        f"{split_name}:{reason}"
        for reason in _locator_contract_reasons(manifest, "depth")
    )
    return reasons


def _depth_requested_for_manifest(manifest: ExperimentManifest) -> bool:
    return (
        manifest.dataset.depth is not None
        or bool(manifest.execution_timing.depth_required)
        or manifest.execution_timing.min_execution_reality_level_for_validation
        == "l2_depth_walk_no_queue"
        or any(
            scenario.type == "depth_walk"
            for scenario in manifest.execution_model.scenarios
        )
    )


def _locator_contract_reasons(manifest: ExperimentManifest, evidence: str) -> list[str]:
    """Use the typed locator contract for both producers and consumers."""
    if not requires_candidate_validation(manifest.research_classification):
        return []
    try:
        if evidence == "dataset" and manifest.dataset.artifact_ref is not None:
            load_artifact_manifest(
                manifest.dataset.artifact_ref.artifact_manifest_uri,
                manifest.dataset.artifact_ref.artifact_manifest_hash,
            )
            return []
        spec = (
            manifest.dataset
            if evidence == "dataset"
            else manifest.dataset.top_of_book
            if evidence == "top_of_book"
            else manifest.dataset.depth
        )
        if spec is None:
            return [f"missing_immutable_{evidence}_locator"]
        parse_immutable_locator(getattr(spec, "locator", None))
        return []
    except (ArtifactManifestError, LocatorValidationError):
        return [f"invalid_immutable_{evidence}_locator"]


def validate_immutable_dataset_locator(
    *, artifact_manifest_uri: str, artifact_manifest_hash: str
) -> None:
    """Public freeze-to-candidate boundary for first-class artifact references."""
    manifest = load_artifact_manifest(artifact_manifest_uri, artifact_manifest_hash)
    # Keep producer and consumer on the same strict locator parser even though
    # the manifest parser already invokes it; this makes the boundary explicit.
    parse_immutable_locator(manifest.locator.as_dict())


def _validate_strategy_data_requirements(
    manifest: ExperimentManifest, strategy_registry: StrategyRegistry
) -> None:
    requirements = strategy_registry.resolve(manifest.strategy_name).data_requirements(
        {key: values[0] for key, values in manifest.parameter_space.items() if values}
    )
    available = _manifest_data_capabilities(manifest)
    missing = [
        capability.name
        for capability in requirements.normalized_capabilities()
        if capability.required and not bool(available.get(capability.name))
    ]
    if missing:
        reason = ",".join(missing)
        if missing in (["top_of_book"], ["orderbook_top"]):
            raise ResearchValidationError(
                "research_data_requirement_top_of_book_missing"
            )
        raise ResearchValidationError(f"research_data_capability_missing:{reason}")


def _manifest_data_capabilities(manifest: ExperimentManifest) -> dict[str, bool]:
    top_of_book_requested = manifest.dataset.top_of_book is not None
    registry = default_dataset_adapter_registry()
    registry.resolve(manifest.dataset.source)
    if top_of_book_requested and manifest.dataset.top_of_book is not None:
        registry.resolve_top_of_book(manifest.dataset.top_of_book.source)
    depth_requested = _depth_requested_for_manifest(manifest)
    if depth_requested:
        depth_source = (
            manifest.dataset.depth.source
            if manifest.dataset.depth is not None
            else "orderbook_depth_levels"
        )
        registry.resolve_depth(depth_source)
    return {
        "candles": True,
        "top_of_book": top_of_book_requested,
        "l2_depth_snapshot": depth_requested,
        "depth_walk": depth_requested,
        "trade_ticks": False,
        "funding": False,
        "cross_asset": False,
        "on_chain": False,
        "calibration_artifacts": bool(manifest.execution_model.calibration_required),
        "execution_evidence": top_of_book_requested
        or any(
            scenario.type == "depth_walk"
            for scenario in manifest.execution_model.scenarios
        ),
    }


def _combined_dataset_quality_gate(
    reports: dict[str, DatasetQualityReport],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    for split_name, report in sorted(reports.items()):
        if report.quality_gate_status != "PASS":
            for reason in report.quality_gate_reasons or ("dataset_quality_failed",):
                reasons.append(f"dataset_quality_{split_name}_{reason}")
    return ("PASS" if not reasons else "FAIL", reasons)


def _dataset_quality_warning_codes(
    reports: dict[str, DatasetQualityReport],
) -> list[str]:
    summary = _top_of_book_quality_summary(reports)
    if summary.get("gate_status") == "WARN":
        return [TOP_OF_BOOK_OPTIONAL_COVERAGE_WARNING]
    return []


def _top_of_book_quality_summary(
    reports: dict[str, DatasetQualityReport],
) -> dict[str, Any]:
    all_report_payloads = [report.payload for _, report in sorted(reports.items())]
    depth_summary = _combined_l2_depth_summary(all_report_payloads)
    requested_reports = [
        (split_name, report.payload)
        for split_name, report in sorted(reports.items())
        if bool(report.payload.get("top_of_book_requested"))
    ]
    if not requested_reports:
        return {
            "requested": False,
            "required": False,
            "gate_status": "NOT_REQUESTED",
            "joined_quote_count": 0,
            "missing_quote_count": 0,
            "expected_signal_count": 0,
            "coverage_pct": None,
            "top_of_book_candle_quote_coverage": None,
            "top_of_book_candle_quote_coverage_pct": None,
            "top_of_book_candle_quote_expected_count": 0,
            "top_of_book_candle_quote_joined_count": 0,
            "signal_execution_quote_coverage": None,
            "signal_execution_quote_coverage_pct": None,
            "signal_execution_quote_coverage_status": "not_computable_without_strategy_signal_run",
            "signal_level_depth_coverage_pct": None,
            "signal_level_depth_coverage_status": "not_computed_depth_walk_not_wired_to_research_backtest",
            "depth_available": False,
            "depth_evidence_available": False,
            **depth_summary,
            "affected_splits": [],
            "next_action": None,
            "limitations": [
                "top_of_book_not_requested",
                (
                    "orderbook_depth_complete_snapshots_stored_not_execution_model_used"
                    if depth_summary.get("l2_depth_complete_snapshots_available")
                    else "orderbook_depth_unavailable"
                ),
                "intra_candle_path_unavailable",
            ],
        }

    expected = sum(
        int(payload.get("top_of_book_expected_signal_count") or 0)
        for _, payload in requested_reports
    )
    joined = sum(
        int(payload.get("top_of_book_joined_count") or 0)
        for _, payload in requested_reports
    )
    missing = sum(
        int(payload.get("top_of_book_missing_count") or 0)
        for _, payload in requested_reports
    )
    statuses = [
        str(payload.get("top_of_book_gate_status") or "UNKNOWN")
        for _, payload in requested_reports
    ]
    gate_status = "PASS"
    if "FAIL" in statuses:
        gate_status = "FAIL"
    elif "WARN" in statuses:
        gate_status = "WARN"
    elif any(status != "PASS" for status in statuses):
        gate_status = "UNKNOWN"
    affected_splits = [
        {
            "split_name": str(split_name),
            "top_of_book_gate_status": str(
                payload.get("top_of_book_gate_status") or "UNKNOWN"
            ),
            "top_of_book_coverage_pct": payload.get("top_of_book_coverage_pct"),
            "top_of_book_missing_count": int(
                payload.get("top_of_book_missing_count") or 0
            ),
            "top_of_book_joined_count": int(
                payload.get("top_of_book_joined_count") or 0
            ),
            "top_of_book_required": bool(payload.get("top_of_book_required")),
            "top_of_book_gate_reasons": [
                str(item) for item in payload.get("top_of_book_gate_reasons") or []
            ],
        }
        for split_name, payload in requested_reports
        if str(payload.get("top_of_book_gate_status") or "UNKNOWN") != "PASS"
        or int(payload.get("top_of_book_missing_count") or 0) > 0
    ]
    coverage_pct = round((joined / expected * 100.0), 8) if expected else 0.0
    required = any(
        bool(payload.get("top_of_book_required")) for _, payload in requested_reports
    )
    join_tolerances = sorted(
        {
            int(payload.get("top_of_book_join_tolerance_ms"))
            for _, payload in requested_reports
            if payload.get("top_of_book_join_tolerance_ms") is not None
        }
    )
    sources = sorted(
        {
            str(payload.get("top_of_book_source"))
            for _, payload in requested_reports
            if payload.get("top_of_book_source")
        }
    )
    return {
        "requested": True,
        "required": required,
        "fail_closed": gate_status == "FAIL",
        "gate_status": gate_status,
        "joined_quote_count": joined,
        "missing_quote_count": missing,
        "expected_signal_count": expected,
        "coverage_pct": coverage_pct,
        "top_of_book_candle_quote_coverage": coverage_pct,
        "top_of_book_candle_quote_coverage_pct": coverage_pct,
        "top_of_book_candle_quote_expected_count": expected,
        "top_of_book_candle_quote_joined_count": joined,
        "signal_execution_quote_coverage": None,
        "signal_execution_quote_coverage_pct": None,
        "signal_execution_quote_coverage_status": "not_computable_without_strategy_signal_run",
        "signal_level_depth_coverage_pct": None,
        "signal_level_depth_coverage_status": "not_computed_depth_walk_not_wired_to_research_backtest",
        **depth_summary,
        "join_tolerance_ms": join_tolerances[0]
        if len(join_tolerances) == 1
        else join_tolerances,
        "sources": sources,
        "affected_splits": affected_splits,
        "warning_code": TOP_OF_BOOK_OPTIONAL_COVERAGE_WARNING
        if gate_status == "WARN"
        else None,
        "next_action": TOP_OF_BOOK_OPERATOR_NEXT_ACTION
        if gate_status in {"WARN", "FAIL"}
        else None,
        "limitations": [
            "top_of_book_is_best_bid_ask_only_not_full_depth",
            "queue_position_unavailable",
            "market_impact_unavailable",
            "trade_ticks_unavailable",
            "intra_candle_path_unavailable",
            "execution_reference_requires_execution_timing_policy",
        ],
    }


def _combined_l2_depth_summary(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    rows_available = any(
        bool(payload.get("l2_depth_rows_available")) for payload in payloads
    )
    complete_snapshots_available = any(
        bool(payload.get("l2_depth_complete_snapshots_available"))
        for payload in payloads
    )
    first_values = [
        int(payload["l2_depth_first_ts"])
        for payload in payloads
        if payload.get("l2_depth_first_ts") is not None
    ]
    last_values = [
        int(payload["l2_depth_last_ts"])
        for payload in payloads
        if payload.get("l2_depth_last_ts") is not None
    ]
    hashes = [
        str(payload.get("l2_depth_content_hash"))
        for payload in payloads
        if isinstance(payload.get("l2_depth_content_hash"), str)
    ]
    return {
        "depth_available": complete_snapshots_available,
        "depth_available_semantics": "stored_l2_depth_complete_snapshots_exist_not_execution_model_used",
        "depth_evidence_available": complete_snapshots_available,
        "l2_depth_evidence_available": complete_snapshots_available,
        "l2_depth_rows_available": rows_available,
        "l2_depth_complete_snapshots_available": complete_snapshots_available,
        "l2_depth_snapshot_count": sum(
            int(payload.get("l2_depth_snapshot_count") or 0) for payload in payloads
        ),
        "l2_depth_row_count": sum(
            int(payload.get("l2_depth_row_count") or 0) for payload in payloads
        ),
        "l2_depth_first_ts": min(first_values) if first_values else None,
        "l2_depth_last_ts": max(last_values) if last_values else None,
        "l2_depth_sources": sorted(
            {
                str(source)
                for payload in payloads
                for source in payload.get("l2_depth_sources") or []
            }
        ),
        "l2_depth_content_hashes": hashes,
        "depth_snapshot_selection_policy": "first_snapshot_after_or_equal_reference_ts_with_max_wait",
        "depth_liquidity_sufficiency_status": "not_computed_depth_walk_not_wired_to_research_backtest",
        "depth_walk_execution_model_available": True,
        "depth_walk_execution_model_used": False,
        "full_orderbook_depth_available": False,
        "queue_position_available": False,
        "trade_ticks_available": False,
        "market_impact_model_available": False,
        "intra_candle_path_available": False,
    }


def _selection_only_candidate_payload(value: Any) -> Any:
    """Remove confirmatory evidence from candidate-search report payloads."""
    if isinstance(value, dict):
        return {
            key: _selection_only_candidate_payload(item)
            for key, item in value.items()
            if "final_holdout" not in str(key)
        }
    if isinstance(value, (list, tuple)):
        return [_selection_only_candidate_payload(item) for item in value]
    return value


def _load_walk_forward_snapshots(
    *,
    db_path: str | Path | None,
    manifest: ExperimentManifest,
    windows: list[dict[str, DateRange]],
    run_context: DatasetRunContext | None = None,
) -> dict[str, DatasetSnapshot]:
    snapshots = {
        "train": load_dataset_split(
            db_path=db_path,
            manifest=manifest,
            split_name="train",
            run_context=run_context,
        ),
        "validation": load_dataset_split(
            db_path=db_path,
            manifest=manifest,
            split_name="validation",
            run_context=run_context,
        ),
    }
    for index, window in enumerate(windows, start=1):
        window_id = f"window_{index:03d}"
        snapshots[f"{window_id}_train"] = load_dataset_range(
            db_path=db_path,
            manifest=manifest,
            split_name=f"{window_id}_train",
            date_range=window["train"],
            run_context=run_context,
        )
        snapshots[f"{window_id}_test"] = load_dataset_range(
            db_path=db_path,
            manifest=manifest,
            split_name=f"{window_id}_test",
            date_range=window["test"],
            run_context=run_context,
        )
    return snapshots


def _repository_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"
