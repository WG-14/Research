from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RESOURCE_PLAN_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ResourceContract:
    cpu_limit: int | None
    memory_limit_mb: int | None
    swap_limit_mb: int | None
    detected_source: str
    env_worker_cap: int | None
    total_process_budget: int | None
    fallback_reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpu_limit": self.cpu_limit,
            "memory_limit_mb": self.memory_limit_mb,
            "swap_limit_mb": self.swap_limit_mb,
            "detected_source": self.detected_source,
            "env_worker_cap": self.env_worker_cap,
            "total_process_budget": self.total_process_budget,
            "fallback_reasons": list(self.fallback_reasons),
        }


@dataclass(frozen=True)
class WorkUnitSelection:
    requested_work_unit_type: str
    effective_work_unit_type: str
    candidate_scenario_task_count: int
    candidate_scenario_split_task_count: int
    selection_reason: str
    rejected_alternatives: list[dict[str, Any]]
    split_names: tuple[str, ...] = ()
    include_walk_forward: bool = False
    final_holdout_present: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESOURCE_PLAN_SCHEMA_VERSION,
            "requested_work_unit_type": self.requested_work_unit_type,
            "effective_work_unit_type": self.effective_work_unit_type,
            "candidate_scenario_task_count": self.candidate_scenario_task_count,
            "candidate_scenario_split_task_count": self.candidate_scenario_split_task_count,
            "selection_reason": self.selection_reason,
            "rejected_alternatives": list(self.rejected_alternatives),
            "split_names": list(self.split_names),
            "include_walk_forward": self.include_walk_forward,
            "final_holdout_present": self.final_holdout_present,
        }


@dataclass(frozen=True)
class ResearchResourcePlan:
    execution_mode: str
    requested_execution_mode: str
    effective_execution_mode: str
    execution_mode_selection_reason: str
    requested_max_workers: int
    effective_max_workers: int
    max_in_flight_tasks: int
    work_unit_type: str
    memory_budget_mb: int | None
    resource_contract: ResourceContract
    work_unit_selection: WorkUnitSelection
    selection_reasons: tuple[str, ...]
    fallback_reasons: tuple[str, ...]
    execution_policy_source: str
    execution_mode_source: str
    max_workers_source: str
    work_unit_source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESOURCE_PLAN_SCHEMA_VERSION,
            "execution_mode": self.execution_mode,
            "requested_execution_mode": self.requested_execution_mode,
            "effective_execution_mode": self.effective_execution_mode,
            "execution_mode_selection_reason": self.execution_mode_selection_reason,
            "detected_cpu_limit": self.resource_contract.cpu_limit,
            "detected_memory_limit_mb": self.resource_contract.memory_limit_mb,
            "detected_swap_limit_mb": self.resource_contract.swap_limit_mb,
            "detected_source": self.resource_contract.detected_source,
            "env_worker_cap": self.resource_contract.env_worker_cap,
            "total_process_budget": self.resource_contract.total_process_budget,
            "requested_max_workers": self.requested_max_workers,
            "effective_max_workers": self.effective_max_workers,
            "max_in_flight_tasks": self.max_in_flight_tasks,
            "work_unit_type": self.work_unit_type,
            "memory_budget_mb": self.memory_budget_mb,
            "selection_reasons": list(self.selection_reasons),
            "fallback_reasons": list(self.fallback_reasons),
            "execution_policy_source": self.execution_policy_source,
            "execution_mode_source": self.execution_mode_source,
            "max_workers_source": self.max_workers_source,
            "work_unit_source": self.work_unit_source,
        }


def detect_resource_contract(
    *,
    cgroup_root: str | Path = "/sys/fs/cgroup",
    proc_root: str | Path = "/proc",
) -> ResourceContract:
    fallback_reasons: list[str] = []
    cpu_limit: int | None = None
    memory_limit_mb: int | None = None
    swap_limit_mb: int | None = None
    detected_sources: list[str] = []

    cgroup = Path(cgroup_root)
    try:
        cpu_limit = _read_cgroup_cpu_limit(cgroup)
        if cpu_limit is not None:
            detected_sources.append("cgroup_cpu")
        else:
            fallback_reasons.append("cgroup_cpu_limit_unavailable")
    except OSError:
        fallback_reasons.append("cgroup_cpu_limit_unavailable")
    try:
        memory_limit_mb = _read_cgroup_memory_limit_mb(cgroup / "memory.max")
        if memory_limit_mb is not None:
            detected_sources.append("cgroup_memory")
        else:
            fallback_reasons.append("cgroup_memory_limit_unavailable")
    except OSError:
        fallback_reasons.append("cgroup_memory_limit_unavailable")
    try:
        swap_limit_mb = _read_cgroup_memory_limit_mb(cgroup / "memory.swap.max")
    except OSError:
        swap_limit_mb = None

    proc = Path(proc_root)
    if cpu_limit is None:
        proc_cpu = _read_proc_cpu_count(proc / "cpuinfo")
        if proc_cpu is not None:
            cpu_limit = proc_cpu
            detected_sources.append("proc_cpuinfo")
        else:
            cpu_limit = os.cpu_count() or 1
            detected_sources.append("os_cpu_count")
            fallback_reasons.append("proc_cpuinfo_unavailable")
    if memory_limit_mb is None:
        proc_mem = _read_proc_mem_total_mb(proc / "meminfo")
        if proc_mem is not None:
            memory_limit_mb = proc_mem
            detected_sources.append("proc_meminfo")
        else:
            fallback_reasons.append("proc_meminfo_unavailable")

    env_cap = _positive_env_int("RESEARCH_RESEARCH_MAX_WORKERS")
    batch_child_cap = _positive_env_int("RESEARCH_BATCH_CHILD_WORKER_BUDGET")
    if batch_child_cap is not None:
        env_cap = (
            min(env_cap, batch_child_cap) if env_cap is not None else batch_child_cap
        )
    return ResourceContract(
        cpu_limit=max(1, int(cpu_limit)) if cpu_limit is not None else None,
        memory_limit_mb=memory_limit_mb,
        swap_limit_mb=swap_limit_mb,
        detected_source="+".join(detected_sources) if detected_sources else "unknown",
        env_worker_cap=env_cap,
        total_process_budget=_positive_env_int("RESEARCH_TOTAL_PROCESS_BUDGET"),
        fallback_reasons=tuple(sorted(set(fallback_reasons))),
    )


def plan_research_resources(
    *,
    manifest: Any,
    candidate_count: int,
    scenario_count: int,
    split_count: int,
    split_names: list[str] | tuple[str, ...] | None = None,
    include_walk_forward: bool = False,
    resource_contract: ResourceContract | None = None,
) -> ResearchResourcePlan:
    contract = resource_contract or detect_resource_contract()
    execution = manifest.research_run.execution
    mode_explicit = _manifest_execution_mode_explicit(manifest)
    work_unit_explicit = _manifest_execution_field_explicit(manifest, "work_unit")
    requested_execution_mode = str(execution.mode or "serial").strip().lower()
    effective_execution_mode, mode_reason, mode_fallbacks = _select_execution_mode(
        requested_execution_mode=requested_execution_mode,
        execution_mode_explicit=mode_explicit,
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        split_count=split_count,
        auto_parallel_block_reasons=_auto_parallel_block_reasons(
            manifest=manifest,
            split_names=split_names,
            include_walk_forward=include_walk_forward,
        ),
        resource_contract=contract,
    )
    auto_worker_cap = contract.cpu_limit or os.cpu_count() or 1
    max_workers_explicit = _manifest_execution_field_explicit(manifest, "max_workers")
    requested_workers = max(
        1, int(execution.max_workers if max_workers_explicit else auto_worker_cap)
    )
    caps: list[tuple[str, int]] = [
        ("manifest_requested_max_workers", requested_workers)
    ]
    if contract.cpu_limit is not None:
        caps.append(("detected_cpu_limit", max(1, int(contract.cpu_limit))))
    if contract.env_worker_cap is not None:
        caps.append(("env_worker_cap", max(1, int(contract.env_worker_cap))))
    if contract.total_process_budget is not None:
        caps.append(
            ("total_process_budget", max(1, int(contract.total_process_budget)))
        )
    if effective_execution_mode != "parallel":
        caps.append(("serial_execution_mode", 1))
    effective_workers = min(value for _, value in caps)
    selection_reasons = [f"{name}:{value}" for name, value in caps]
    selection_reasons.append(f"execution_mode_selection:{mode_reason}")
    if effective_workers < requested_workers:
        selection_reasons.append("effective_workers_capped_below_manifest_request")
    max_workers_source = "user_explicit" if max_workers_explicit else "resource_planner"
    cap_names = [name for name, value in caps if value == effective_workers]
    if effective_workers < requested_workers:
        if "env_worker_cap" in cap_names or "total_process_budget" in cap_names:
            max_workers_source = "env_cap"

    memory_budget = getattr(
        manifest.research_run.resource_limits, "max_total_memory_mb", None
    )
    if memory_budget is None:
        memory_budget = contract.memory_limit_mb
    memory_budget_mb = int(float(memory_budget)) if memory_budget is not None else None
    work_unit_selection = select_work_unit_granularity(
        requested_work_unit_type=str(execution.work_unit or "candidate_scenario"),
        candidate_count=candidate_count,
        scenario_count=scenario_count,
        split_count=split_count,
        split_names=split_names,
        include_walk_forward=include_walk_forward,
        effective_max_workers=effective_workers,
    )
    work_unit_source = "user_explicit" if work_unit_explicit else "parser_default"
    if (
        work_unit_selection.effective_work_unit_type
        != work_unit_selection.requested_work_unit_type
    ):
        work_unit_source = "resource_planner"
    execution_mode_source = "user_explicit" if mode_explicit else "resource_planner"
    return ResearchResourcePlan(
        execution_mode=effective_execution_mode,
        requested_execution_mode=requested_execution_mode if mode_explicit else "auto",
        effective_execution_mode=effective_execution_mode,
        execution_mode_selection_reason=mode_reason,
        requested_max_workers=requested_workers,
        effective_max_workers=effective_workers,
        max_in_flight_tasks=max(1, effective_workers * 2),
        work_unit_type=work_unit_selection.effective_work_unit_type,
        memory_budget_mb=memory_budget_mb,
        resource_contract=contract,
        work_unit_selection=work_unit_selection,
        selection_reasons=tuple(selection_reasons),
        fallback_reasons=tuple(
            sorted(set(contract.fallback_reasons + tuple(mode_fallbacks)))
        ),
        execution_policy_source=execution_mode_source,
        execution_mode_source=execution_mode_source,
        max_workers_source=max_workers_source,
        work_unit_source=work_unit_source,
    )


def select_work_unit_granularity(
    *,
    requested_work_unit_type: str,
    candidate_count: int,
    scenario_count: int,
    split_count: int,
    split_names: list[str] | tuple[str, ...] | None = None,
    include_walk_forward: bool = False,
    effective_max_workers: int,
) -> WorkUnitSelection:
    requested = str(requested_work_unit_type or "candidate_scenario").strip().lower()
    split_tuple = tuple(str(name) for name in (split_names or ()))
    final_holdout_present = "final_holdout" in split_tuple
    candidate_scenario_tasks = max(0, int(candidate_count)) * max(
        0, int(scenario_count)
    )
    split_tasks = candidate_scenario_tasks * max(1, int(split_count))
    rejected: list[dict[str, Any]] = []
    split_supported, split_reject_reasons = _candidate_scenario_split_capability(
        split_names=split_tuple,
        include_walk_forward=include_walk_forward,
    )
    if requested == "candidate_scenario_split":
        if not split_supported:
            for reason in split_reject_reasons:
                rejected.append(
                    {"work_unit_type": "candidate_scenario_split", "reason": reason}
                )
            return WorkUnitSelection(
                requested_work_unit_type=requested,
                effective_work_unit_type="candidate_scenario",
                candidate_scenario_task_count=candidate_scenario_tasks,
                candidate_scenario_split_task_count=split_tasks,
                selection_reason="candidate_scenario_split_not_supported",
                rejected_alternatives=rejected,
                split_names=split_tuple,
                include_walk_forward=include_walk_forward,
                final_holdout_present=final_holdout_present,
            )
        rejected.append(
            {
                "work_unit_type": "candidate_scenario",
                "reason": "manifest_requested_split_work_unit",
            }
        )
        return WorkUnitSelection(
            requested_work_unit_type=requested,
            effective_work_unit_type="candidate_scenario_split",
            candidate_scenario_task_count=candidate_scenario_tasks,
            candidate_scenario_split_task_count=split_tasks,
            selection_reason="manifest_requested_candidate_scenario_split",
            rejected_alternatives=rejected,
            split_names=split_tuple,
            include_walk_forward=include_walk_forward,
            final_holdout_present=final_holdout_present,
        )
    if not split_supported:
        for reason in split_reject_reasons:
            rejected.append(
                {"work_unit_type": "candidate_scenario_split", "reason": reason}
            )
        return WorkUnitSelection(
            requested_work_unit_type=requested,
            effective_work_unit_type="candidate_scenario",
            candidate_scenario_task_count=candidate_scenario_tasks,
            candidate_scenario_split_task_count=split_tasks,
            selection_reason="candidate_scenario_selected",
            rejected_alternatives=rejected,
            split_names=split_tuple,
            include_walk_forward=include_walk_forward,
            final_holdout_present=final_holdout_present,
        )
    if (
        candidate_scenario_tasks < int(effective_max_workers) <= split_tasks
        and int(split_count) > 1
    ):
        rejected.append(
            {
                "work_unit_type": "candidate_scenario",
                "reason": "candidate_scenario_tasks_below_effective_workers",
            }
        )
        return WorkUnitSelection(
            requested_work_unit_type=requested,
            effective_work_unit_type="candidate_scenario_split",
            candidate_scenario_task_count=candidate_scenario_tasks,
            candidate_scenario_split_task_count=split_tasks,
            selection_reason="split_tasks_fill_effective_workers",
            rejected_alternatives=rejected,
            split_names=split_tuple,
            include_walk_forward=include_walk_forward,
            final_holdout_present=final_holdout_present,
        )
    reason = "candidate_scenario_tasks_match_or_exceed_effective_workers"
    if split_tasks <= candidate_scenario_tasks:
        reason = "split_parallelism_not_available"
    elif int(effective_max_workers) > split_tasks:
        reason = "split_tasks_still_below_effective_workers"
    rejected.append({"work_unit_type": "candidate_scenario_split", "reason": reason})
    return WorkUnitSelection(
        requested_work_unit_type=requested,
        effective_work_unit_type="candidate_scenario",
        candidate_scenario_task_count=candidate_scenario_tasks,
        candidate_scenario_split_task_count=split_tasks,
        selection_reason="candidate_scenario_selected",
        rejected_alternatives=rejected,
        split_names=split_tuple,
        include_walk_forward=include_walk_forward,
        final_holdout_present=final_holdout_present,
    )


def _manifest_execution_mode_explicit(manifest: Any) -> bool:
    return _manifest_execution_field_explicit(manifest, "mode")


def _manifest_execution_field_explicit(manifest: Any, field_name: str) -> bool:
    provenance = getattr(manifest, "manifest_input_provenance", None)
    research_run_provenance = getattr(provenance, "research_run", None)
    execution_provenance = getattr(research_run_provenance, "execution", None)
    provenance_field = {
        "mode": "mode_declared",
        "max_workers": "max_workers_declared",
        "work_unit": "work_unit_declared",
        "process_start_method": "process_start_method_declared",
    }.get(field_name)
    if execution_provenance is not None and provenance_field is not None:
        return bool(getattr(execution_provenance, provenance_field, False))
    raw = getattr(manifest, "raw", None)
    if not isinstance(raw, dict):
        return False
    research_run = raw.get("research_run")
    if not isinstance(research_run, dict):
        return False
    execution = research_run.get("execution")
    return isinstance(execution, dict) and field_name in execution


def _select_execution_mode(
    *,
    requested_execution_mode: str,
    execution_mode_explicit: bool,
    candidate_count: int,
    scenario_count: int,
    split_count: int,
    resource_contract: ResourceContract,
    auto_parallel_block_reasons: tuple[str, ...] = (),
) -> tuple[str, str, tuple[str, ...]]:
    requested = str(requested_execution_mode or "serial").strip().lower()
    if execution_mode_explicit:
        return requested, f"manifest_explicit_{requested}", ()
    if auto_parallel_block_reasons:
        return "serial", "auto_serial_fallback", auto_parallel_block_reasons
    task_count = (
        max(0, int(candidate_count))
        * max(0, int(scenario_count))
        * max(1, int(split_count))
    )
    cpu_limit = resource_contract.cpu_limit
    capped_cpu_limit = cpu_limit
    for cap in (
        resource_contract.env_worker_cap,
        resource_contract.total_process_budget,
    ):
        if cap is not None:
            capped_cpu_limit = (
                min(int(capped_cpu_limit), int(cap))
                if capped_cpu_limit is not None
                else int(cap)
            )
    if capped_cpu_limit is not None and int(capped_cpu_limit) >= 2 and task_count >= 2:
        return "parallel", "auto_parallel_detected_resources_and_workload", ()
    fallback_reasons: list[str] = []
    if capped_cpu_limit is None:
        fallback_reasons.append("auto_execution_mode_cpu_limit_unavailable")
    elif int(capped_cpu_limit) < 2:
        fallback_reasons.append("auto_execution_mode_insufficient_cpu")
    if task_count < 2:
        fallback_reasons.append("auto_execution_mode_insufficient_task_count")
    return "serial", "auto_serial_fallback", tuple(fallback_reasons)


def _auto_parallel_block_reasons(
    *,
    manifest: Any,
    split_names: list[str] | tuple[str, ...] | None,
    include_walk_forward: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    split_tuple = tuple(str(name) for name in (split_names or ()))
    if include_walk_forward or any(name.startswith("window_") for name in split_tuple):
        reasons.append("auto_parallel_walk_forward_not_in_scope")
    if "final_holdout" in split_tuple:
        reasons.append("auto_parallel_final_holdout_not_in_scope")
    audit_trail = getattr(getattr(manifest, "research_run", None), "audit_trail", None)
    if bool(getattr(audit_trail, "complete_external", False)):
        reasons.append("auto_parallel_complete_external_audit_not_supported")
    artifact_policy = getattr(
        getattr(manifest, "research_run", None), "artifact_policy", None
    )
    if bool(getattr(artifact_policy, "full_decisions_external_jsonl", False)):
        reasons.append("auto_parallel_full_decisions_external_not_supported")
    return tuple(reasons)


def _candidate_scenario_split_capability(
    *,
    split_names: tuple[str, ...],
    include_walk_forward: bool,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if include_walk_forward or any(name.startswith("window_") for name in split_names):
        reasons.append("candidate_scenario_split_walk_forward_not_supported")
    if "final_holdout" in split_names:
        reasons.append("candidate_scenario_split_final_holdout_not_supported")
    if split_names and split_names != ("train", "validation"):
        missing = [name for name in ("train", "validation") if name not in split_names]
        if missing:
            reasons.append("candidate_scenario_split_missing_required_splits")
        extra = [
            name
            for name in split_names
            if name not in {"train", "validation", "final_holdout"}
        ]
        if extra and not any(name.startswith("window_") for name in extra):
            reasons.append("candidate_scenario_split_unknown_split_not_supported")
    return not reasons, tuple(reasons)


def _read_cgroup_cpu_limit(cgroup_root: Path) -> int | None:
    cpu_max = cgroup_root / "cpu.max"
    if cpu_max.exists():
        parts = cpu_max.read_text(encoding="utf-8").strip().split()
        if len(parts) >= 2 and parts[0] != "max":
            quota = int(parts[0])
            period = int(parts[1])
            if quota > 0 and period > 0:
                return max(1, int(math.ceil(quota / period)))
    quota_path = cgroup_root / "cpu.cfs_quota_us"
    period_path = cgroup_root / "cpu.cfs_period_us"
    if quota_path.exists() and period_path.exists():
        quota = int(quota_path.read_text(encoding="utf-8").strip())
        period = int(period_path.read_text(encoding="utf-8").strip())
        if quota > 0 and period > 0:
            return max(1, int(math.ceil(quota / period)))
    return None


def _read_cgroup_memory_limit_mb(path: Path) -> int | None:
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    if raw in {"", "max"}:
        return None
    value = int(raw)
    if value <= 0:
        return None
    return max(1, value // (1024 * 1024))


def _read_proc_cpu_count(path: Path) -> int | None:
    if not path.exists():
        return None
    count = sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("processor")
    )
    return count or None


def _read_proc_mem_total_mb(path: Path) -> int | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2:
                return max(1, int(parts[1]) // 1024)
    return None


def _positive_env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    try:
        value = int(str(raw))
    except ValueError:
        return None
    return value if value > 0 else None
