"""Research-only strategy declarations.

This module deliberately has no operational strategy, replay, or order-entry
dependency.  It is the contract consumed by ``bithumb-research``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .backtest_types import BacktestRun, BacktestRunContext
from .dataset_snapshot import DatasetSnapshot
from .decision_event import ResearchDecisionEvent
from .execution_model import ExecutionModel
from .experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy
from .hashing import sha256_prefixed
from .strategy_spec import StrategySpec


ResearchEventBuilder = Callable[..., Iterable[ResearchDecisionEvent]]
ResearchParameterMaterializer = Callable[..., dict[str, Any]]
ResearchStrategyRunner = Callable[
    [
        DatasetSnapshot,
        dict[str, Any],
        float,
        float,
        float | None,
        ExecutionModel | None,
        ExecutionTimingPolicy | None,
        PortfolioPolicy | None,
        BacktestRunContext | None,
    ],
    BacktestRun,
]
DiagnosticCountBuilder = Callable[[dict[str, object]], dict[str, Any]]
ResearchDataRequirementBuilder = Callable[[object | None], "ResearchStrategyDataRequirements"]
ResearchDecisionBuilder = Callable[..., Any]
ResearchPayloadAdapter = Callable[[dict[str, object], Any], dict[str, object]]
ExitPolicyMaterializer = Callable[[str, dict[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class ExitPolicyMaterialization:
    exit_policy: dict[str, Any]
    exit_policy_hash: str
    exit_policy_contract_hash: str
    exit_policy_config: dict[str, Any]
    exit_policy_config_hash: str
    exit_policy_source: str
    exit_policy_materialization_mode: str

    def as_dict(self) -> dict[str, object]:
        return {
            "exit_policy": dict(self.exit_policy),
            "exit_policy_hash": self.exit_policy_hash,
            "exit_policy_contract_hash": self.exit_policy_contract_hash,
            "exit_policy_config": dict(self.exit_policy_config),
            "exit_policy_config_hash": self.exit_policy_config_hash,
            "exit_policy_source": self.exit_policy_source,
            "exit_policy_materialization_mode": self.exit_policy_materialization_mode,
        }


def normalize_exit_policy_materialization(
    result: Any,
    *,
    strategy_name: str,
    materializer: Callable[..., Any] | None,
    default_source: str,
    default_mode: str,
) -> ExitPolicyMaterialization:
    if isinstance(result, ExitPolicyMaterialization):
        return result
    if not isinstance(result, dict) or not isinstance(result.get("exit_policy"), dict):
        raise ValueError(f"research_exit_policy_materializer_invalid:{strategy_name}")
    policy = dict(result["exit_policy"])
    config = dict(result.get("exit_policy_config") or policy)
    source = str(result.get("exit_policy_source") or default_source)
    contract_payload = {
        "schema_version": 1,
        "strategy_name": strategy_name,
        "materializer_module": materializer.__module__ if materializer else None,
        "materializer_qualname": materializer.__qualname__ if materializer else None,
        "exit_policy_source": source,
    }
    return ExitPolicyMaterialization(
        exit_policy=policy,
        exit_policy_hash=str(result.get("exit_policy_hash") or sha256_prefixed(policy)),
        exit_policy_contract_hash=str(result.get("exit_policy_contract_hash") or sha256_prefixed(contract_payload)),
        exit_policy_config=config,
        exit_policy_config_hash=str(result.get("exit_policy_config_hash") or sha256_prefixed(config)),
        exit_policy_source=source,
        exit_policy_materialization_mode=str(result.get("exit_policy_materialization_mode") or default_mode),
    )


def generic_diagnostics_count_builder(payload: dict[str, object]) -> dict[str, Any]:
    raw_signal = str(payload.get("raw_signal") or "").upper()
    final_signal = str(payload.get("final_signal") or "").upper()
    entry_signal = str(payload.get("entry_signal") or "").upper()
    defaults = {
        "raw_signal_count": 0,
        "final_signal_count": 0,
        "entry_signal_count": 0,
        "exit_signal_count": 0,
    }
    counts: dict[str, int] = {}
    if raw_signal in {"BUY", "SELL"}:
        counts["raw_signal_count"] = 1
    if final_signal in {"BUY", "SELL"}:
        counts["final_signal_count"] = 1
    if entry_signal == "BUY":
        counts["entry_signal_count"] = 1
    if str(payload.get("exit_signal") or "").upper() == "SELL":
        counts["exit_signal_count"] = 1
    return {
        "strategy_diagnostics_namespace": payload.get("strategy_diagnostics_namespace"),
        "strategy_diagnostic_count_defaults": defaults,
        "strategy_diagnostic_counts": counts,
    }


@dataclass(frozen=True, slots=True)
class ResearchDataRequirement:
    name: str
    required: bool = True
    min_coverage_pct: float | None = None
    source: str | None = None
    notes: str | None = None
    lookback_rows: int | None = None
    min_rows: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "min_coverage_pct": self.min_coverage_pct,
            "source": self.source,
            "notes": self.notes,
            "lookback_rows": self.lookback_rows,
            "min_rows": self.min_rows,
        }


@dataclass(frozen=True, slots=True)
class ResearchStrategyDataRequirements:
    required_data: tuple[str, ...]
    optional_data: tuple[str, ...] = ()
    capabilities: tuple[ResearchDataRequirement, ...] = ()

    def normalized_capabilities(self) -> tuple[ResearchDataRequirement, ...]:
        values: dict[str, ResearchDataRequirement] = {}
        for name in self.required_data:
            normalized = str(name).strip().lower()
            if normalized:
                values[normalized] = ResearchDataRequirement(name=normalized)
        for name in self.optional_data:
            normalized = str(name).strip().lower()
            if normalized and normalized not in values:
                values[normalized] = ResearchDataRequirement(name=normalized, required=False)
        for capability in self.capabilities:
            normalized = str(capability.name).strip().lower()
            if normalized:
                values[normalized] = ResearchDataRequirement(
                    name=normalized,
                    required=bool(capability.required),
                    min_coverage_pct=capability.min_coverage_pct,
                    source=capability.source,
                    notes=capability.notes,
                    lookback_rows=capability.lookback_rows,
                    min_rows=capability.min_rows,
                )
        return tuple(values[name] for name in sorted(values))

    def capability_contract_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "required_data": list(self.required_data),
            "optional_data": list(self.optional_data),
            "capabilities": [item.as_dict() for item in self.normalized_capabilities()],
        }


@dataclass(frozen=True, slots=True)
class ResearchStrategyPlugin:
    name: str
    version: str
    spec: StrategySpec
    required_data: tuple[str, ...]
    optional_data: tuple[str, ...]
    runner: ResearchStrategyRunner
    event_builder: ResearchEventBuilder
    decision_contract_version: str
    diagnostics_namespace: str
    parameter_materializer: ResearchParameterMaterializer | None = None
    diagnostics_builder: DiagnosticCountBuilder | None = None
    data_requirements_builder: ResearchDataRequirementBuilder | None = None
    decision_builder: ResearchDecisionBuilder | None = None
    payload_adapter: ResearchPayloadAdapter | None = None
    exit_policy_materializer: ExitPolicyMaterializer | None = None

    def __post_init__(self) -> None:
        name = str(self.name or "").strip().lower()
        if not name:
            raise ValueError("research_strategy_name_missing")
        if not str(self.version or "").strip():
            raise ValueError(f"research_strategy_version_missing:{name}")
        if not str(self.decision_contract_version or "").strip():
            raise ValueError(f"research_strategy_decision_contract_version_missing:{name}")
        if self.runner is None:
            raise ValueError(f"research_strategy_runner_missing:{name}")
        if self.event_builder is None:
            raise ValueError(f"research_strategy_event_builder_missing:{name}")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "required_data", tuple(sorted({str(x).strip().lower() for x in self.required_data if str(x).strip()})))
        object.__setattr__(self, "optional_data", tuple(sorted({str(x).strip().lower() for x in self.optional_data if str(x).strip()})))
        if self.diagnostics_builder is None:
            object.__setattr__(self, "diagnostics_builder", generic_diagnostics_count_builder)

    def data_requirements(self, strategy_spec: object | None = None) -> ResearchStrategyDataRequirements:
        if self.data_requirements_builder is not None:
            return self.data_requirements_builder(strategy_spec)
        return ResearchStrategyDataRequirements(self.required_data, self.optional_data)

    def contract_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "name": self.name,
            "version": self.version,
            "strategy_spec_hash": self.spec.spec_hash(),
            "required_data": list(self.required_data),
            "optional_data": list(self.optional_data),
            "research_data_requirements": self.data_requirements().capability_contract_payload(),
            "decision_contract_version": self.decision_contract_version,
            "diagnostics_namespace": self.diagnostics_namespace,
            "runner_module": self.runner.__module__,
            "event_builder_module": self.event_builder.__module__,
            "parameter_materializer_module": (
                self.parameter_materializer.__module__ if self.parameter_materializer is not None else None
            ),
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.contract_payload())
