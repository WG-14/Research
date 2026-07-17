from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from .research_classification import requires_candidate_validation


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class RandomEntryBenchmarkContract:
    iterations: int
    seed_policy: str
    entry_index_policy: str

    def as_dict(self) -> dict[str, object]:
        return {
            "iterations": self.iterations,
            "seed_policy": self.seed_policy,
            "entry_index_policy": self.entry_index_policy,
        }


@dataclass(frozen=True)
class SameHoldingPeriodBenchmarkContract:
    holding_period_source: str
    entry_policy: str
    min_candidate_closed_trades: int

    def as_dict(self) -> dict[str, object]:
        return {
            "holding_period_source": self.holding_period_source,
            "entry_policy": self.entry_policy,
            "min_candidate_closed_trades": self.min_candidate_closed_trades,
        }


@dataclass(frozen=True)
class StrategyBenchmarkReference:
    strategy_name: str
    strategy_version: str
    parameter_values: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version,
            "parameter_values": dict(self.parameter_values),
        }


@dataclass(frozen=True)
class ApprovedStrategyBenchmarkReference:
    strategy: StrategyBenchmarkReference
    approval_artifact_path: str
    approval_artifact_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            **self.strategy.as_dict(),
            "approval_artifact_path": self.approval_artifact_path,
            "approval_artifact_hash": self.approval_artifact_hash,
        }


@dataclass(frozen=True)
class BenchmarkSuiteContract:
    schema_version: int
    required_for_validation: bool
    random_entry: RandomEntryBenchmarkContract
    same_holding_period: SameHoldingPeriodBenchmarkContract
    simpler_strategy: StrategyBenchmarkReference
    approved_strategy: ApprovedStrategyBenchmarkReference

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "required_for_validation": self.required_for_validation,
            "random_entry": self.random_entry.as_dict(),
            "same_holding_period": self.same_holding_period.as_dict(),
            "simpler_strategy": self.simpler_strategy.as_dict(),
            "approved_strategy": self.approved_strategy.as_dict(),
        }


def parse_benchmark_suite_contract(
    value: Any,
    *,
    research_classification: str,
    registry: Any,
    candidate_strategy_name: str | None = None,
) -> BenchmarkSuiteContract | None:
    validation_required = requires_candidate_validation(research_classification)
    if value is None:
        if validation_required:
            raise ValueError("benchmark_suite required for validation-bound manifests")
        return None
    if not isinstance(value, dict):
        raise ValueError("benchmark_suite must be an object")
    allowed = {
        "schema_version",
        "required_for_validation",
        "random_entry",
        "same_holding_period",
        "simpler_strategy",
        "approved_strategy",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"benchmark_suite unsupported fields: {','.join(unknown)}")
    schema_version = _positive_int(
        value.get("schema_version"), "benchmark_suite.schema_version"
    )
    if schema_version != 1:
        raise ValueError("benchmark_suite.schema_version must be 1")
    required = bool(value.get("required_for_validation", validation_required))
    if validation_required and not required:
        raise ValueError(
            "benchmark_suite.required_for_validation must be true for validation-bound manifests"
        )
    contract = BenchmarkSuiteContract(
        schema_version=schema_version,
        required_for_validation=required,
        random_entry=_parse_random_entry(value.get("random_entry")),
        same_holding_period=_parse_same_holding_period(
            value.get("same_holding_period")
        ),
        simpler_strategy=_parse_strategy_reference(
            value.get("simpler_strategy"),
            field="benchmark_suite.simpler_strategy",
            registry=registry,
        ),
        approved_strategy=_parse_approved_strategy(
            value.get("approved_strategy"), registry=registry
        ),
    )
    if (
        candidate_strategy_name
        and contract.simpler_strategy.strategy_name == candidate_strategy_name
    ):
        raise ValueError(
            "benchmark_suite.simpler_strategy must differ from the candidate strategy"
        )
    return contract


def _parse_random_entry(value: Any) -> RandomEntryBenchmarkContract:
    if not isinstance(value, dict):
        raise ValueError("benchmark_suite.random_entry must be an object")
    allowed = {"iterations", "seed_policy", "entry_index_policy"}
    _reject_unknown(value, allowed, "benchmark_suite.random_entry")
    seed_policy = str(value.get("seed_policy") or "").strip()
    if seed_policy != "derived_from_manifest_split_benchmark_contract_hash":
        raise ValueError(
            "benchmark_suite.random_entry.seed_policy must be "
            "derived_from_manifest_split_benchmark_contract_hash"
        )
    entry_policy = str(value.get("entry_index_policy") or "").strip()
    if entry_policy != "uniform_causal_entry_holding_to_split_end":
        raise ValueError(
            "benchmark_suite.random_entry.entry_index_policy must be "
            "uniform_causal_entry_holding_to_split_end"
        )
    iterations = _positive_int(
        value.get("iterations"), "benchmark_suite.random_entry.iterations"
    )
    if iterations > 10_000:
        raise ValueError("benchmark_suite.random_entry.iterations must be <= 10000")
    return RandomEntryBenchmarkContract(
        iterations=iterations,
        seed_policy=seed_policy,
        entry_index_policy=entry_policy,
    )


def _parse_same_holding_period(value: Any) -> SameHoldingPeriodBenchmarkContract:
    if not isinstance(value, dict):
        raise ValueError("benchmark_suite.same_holding_period must be an object")
    allowed = {"holding_period_source", "entry_policy", "min_candidate_closed_trades"}
    _reject_unknown(value, allowed, "benchmark_suite.same_holding_period")
    source = str(value.get("holding_period_source") or "").strip()
    if source != "candidate_median_closed_trade_holding_bars":
        raise ValueError(
            "benchmark_suite.same_holding_period.holding_period_source must be "
            "candidate_median_closed_trade_holding_bars"
        )
    entry_policy = str(value.get("entry_policy") or "").strip()
    if entry_policy != "non_overlapping_unconditional_entries":
        raise ValueError(
            "benchmark_suite.same_holding_period.entry_policy must be "
            "non_overlapping_unconditional_entries"
        )
    return SameHoldingPeriodBenchmarkContract(
        holding_period_source=source,
        entry_policy=entry_policy,
        min_candidate_closed_trades=_positive_int(
            value.get("min_candidate_closed_trades"),
            "benchmark_suite.same_holding_period.min_candidate_closed_trades",
        ),
    )


def _parse_strategy_reference(
    value: Any, *, field: str, registry: Any
) -> StrategyBenchmarkReference:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    allowed = {"strategy_name", "strategy_version", "parameter_values"}
    _reject_unknown(value, allowed, field)
    name = str(value.get("strategy_name") or "").strip()
    version = str(value.get("strategy_version") or "").strip()
    parameters = value.get("parameter_values")
    if not name or not version or not isinstance(parameters, dict):
        raise ValueError(
            f"{field} requires strategy_name, strategy_version, and parameter_values"
        )
    plugin = registry.resolve(name)
    if plugin.version != version:
        raise ValueError(f"{field}.strategy_version does not match registered strategy")
    accepted = set(plugin.spec.accepted_parameter_names)
    unknown_parameters = sorted(set(parameters) - accepted)
    if unknown_parameters:
        raise ValueError(
            f"{field}.parameter_values unsupported fields: {','.join(unknown_parameters)}"
        )
    missing = sorted(
        set(plugin.spec.required_parameter_names)
        - set(parameters)
        - set(plugin.spec.default_parameters)
    )
    if missing:
        raise ValueError(
            f"{field}.parameter_values missing required fields: {','.join(missing)}"
        )
    return StrategyBenchmarkReference(name, version, dict(parameters))


def _parse_approved_strategy(
    value: Any, *, registry: Any
) -> ApprovedStrategyBenchmarkReference:
    if not isinstance(value, dict):
        raise ValueError("benchmark_suite.approved_strategy must be an object")
    allowed = {
        "strategy_name",
        "strategy_version",
        "parameter_values",
        "approval_artifact_path",
        "approval_artifact_hash",
    }
    _reject_unknown(value, allowed, "benchmark_suite.approved_strategy")
    strategy = _parse_strategy_reference(
        {
            key: value.get(key)
            for key in ("strategy_name", "strategy_version", "parameter_values")
        },
        field="benchmark_suite.approved_strategy",
        registry=registry,
    )
    path = str(value.get("approval_artifact_path") or "").strip()
    resolved_path = Path(path).expanduser()
    if not path or not resolved_path.is_absolute():
        raise ValueError(
            "benchmark_suite.approved_strategy.approval_artifact_path must be absolute"
        )
    repository_root = Path(__file__).resolve().parents[3]
    try:
        resolved_path.resolve(strict=False).relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise ValueError(
            "benchmark_suite.approved_strategy.approval_artifact_path must be repository-external"
        )
    content_hash = str(value.get("approval_artifact_hash") or "").strip()
    if _SHA256_RE.fullmatch(content_hash) is None:
        raise ValueError(
            "benchmark_suite.approved_strategy.approval_artifact_hash must be sha256:<64 hex>"
        )
    return ApprovedStrategyBenchmarkReference(strategy, path, content_hash)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0 or parsed != value:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _reject_unknown(value: dict[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{field} unsupported fields: {','.join(unknown)}")
