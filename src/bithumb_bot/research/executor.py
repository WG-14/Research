from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .execution_plan import ResearchWorkUnit
from .hashing import sha256_prefixed


@dataclass(frozen=True)
class ResearchWorkResult:
    work_unit: ResearchWorkUnit
    work_unit_hash: str
    candidate_index: int
    candidate_id: str
    scenario_index: int
    scenario_id: str
    status: str
    base_result: dict[str, Any] | None = None
    failure_reason: str | None = None
    failure_evidence: dict[str, Any] | None = None
    observability: dict[str, Any] | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if self.content_hash is not None:
            return
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "work_unit_hash": self.work_unit_hash,
                    "candidate_index": self.candidate_index,
                    "candidate_id": self.candidate_id,
                    "scenario_index": self.scenario_index,
                    "scenario_id": self.scenario_id,
                    "status": self.status,
                    "failure_reason": self.failure_reason,
                    "base_result_available": self.base_result is not None,
                    "failure_evidence_hash": (
                        sha256_prefixed(self.failure_evidence) if self.failure_evidence is not None else None
                    ),
                }
            ),
        )

    def observability_payload(self) -> dict[str, Any]:
        payload = dict(self.observability or {})
        payload.setdefault("work_unit", self.work_unit.as_dict())
        payload.setdefault("status", self.status)
        if self.failure_reason is not None:
            payload.setdefault("failure_reason", self.failure_reason)
        if self.failure_evidence is not None:
            payload.setdefault("resource_guard", self.failure_evidence)
        payload.setdefault("content_hash", self.content_hash)
        return payload


ResearchWorker = Callable[[Any], ResearchWorkResult]


def execute_research_work_units_serial(
    *,
    tasks: Iterable[Any],
    worker: ResearchWorker,
) -> list[ResearchWorkResult]:
    return [worker(task) for task in tasks]


def execute_research_work_units_parallel(
    *,
    tasks: Iterable[Any],
    worker: ResearchWorker,
    max_workers: int,
) -> list[ResearchWorkResult]:
    results: list[ResearchWorkResult] = []
    with ProcessPoolExecutor(max_workers=int(max_workers)) as pool:
        futures = [pool.submit(worker, task) for task in tasks]
        for completion_order, future in enumerate(as_completed(futures)):
            result = future.result()
            observability = dict(result.observability or {})
            observability["completion_order"] = completion_order
            results.append(
                ResearchWorkResult(
                    work_unit=result.work_unit,
                    work_unit_hash=result.work_unit_hash,
                    candidate_index=result.candidate_index,
                    candidate_id=result.candidate_id,
                    scenario_index=result.scenario_index,
                    scenario_id=result.scenario_id,
                    status=result.status,
                    base_result=result.base_result,
                    failure_reason=result.failure_reason,
                    failure_evidence=result.failure_evidence,
                    observability=observability,
                    content_hash=result.content_hash,
                )
            )
    return results


def sort_work_results_deterministically(results: Iterable[ResearchWorkResult]) -> list[ResearchWorkResult]:
    return sorted(
        results,
        key=lambda result: (
            int(result.scenario_index),
            int(result.candidate_index),
            str(result.work_unit.split_name),
        ),
    )
