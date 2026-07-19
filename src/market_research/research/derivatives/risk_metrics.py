"""Typed, hash-bound derivative risk metrics for offline research.

The artifact in this module is a projection of an already completed derivative
simulation.  It does not accept account state, broker data, implicit currency
conversion, or caller supplied metric values.  A metric that cannot be derived
from the supplied immutable evidence is represented as unavailable instead of
being silently replaced by zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Sequence

from market_research.research.hashing import sha256_prefixed

from .common import (
    DERIVATIVE_RESEARCH_SCHEMA_VERSION,
    DerivativeExperimentRun,
    DerivativeResearchError,
    decimal_text,
    exact_decimal,
    parse_timestamp,
    require_hash,
    require_stable_id,
)
from .futures import FuturesStressExecution, FuturesStressInputs
from .options import (
    MultiLegState,
    OptionRobustnessDimension,
    OptionRobustnessExecution,
    OptionRobustnessInput,
)
from .portfolio import PortfolioExposureSnapshot
from .simulation_evidence import (
    DerivativeSimulationEvidence,
    SimulationProductKind,
)


RISK_METRICS_SCHEMA_VERSION = DERIVATIVE_RESEARCH_SCHEMA_VERSION
_ZERO = Decimal("0")
_SECONDS_PER_DAY = Decimal("86400")


class RiskProductKind(StrEnum):
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    MULTI_LEG = "MULTI_LEG"


class RiskMetricId(StrEnum):
    S5_R01 = "S5-R01"
    S5_R02 = "S5-R02"
    S5_R03 = "S5-R03"
    S5_R04 = "S5-R04"
    S5_R05 = "S5-R05"
    S5_R06 = "S5-R06"
    S5_R07 = "S5-R07"
    S5_R08 = "S5-R08"
    S5_R09 = "S5-R09"
    S5_R10 = "S5-R10"
    S5_R11 = "S5-R11"
    S5_R12 = "S5-R12"
    S5_R13 = "S5-R13"
    S5_R14 = "S5-R14"
    S5_R15 = "S5-R15"
    S5_R16 = "S5-R16"
    S5_R17 = "S5-R17"
    S5_R18 = "S5-R18"
    S5_R19 = "S5-R19"
    S5_R20 = "S5-R20"


class RiskMetricStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE_SAMPLE = "UNAVAILABLE_SAMPLE"
    UNAVAILABLE_ZERO_DENOMINATOR = "UNAVAILABLE_ZERO_DENOMINATOR"
    UNAVAILABLE_INPUT = "UNAVAILABLE_INPUT"
    UNBOUNDED = "UNBOUNDED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True, slots=True)
class RiskMetricValue:
    name: str
    value: Decimal
    unit: str

    def __post_init__(self) -> None:
        require_stable_id(self.name, "risk_metric_value.name")
        require_stable_id(self.unit, "risk_metric_value.unit")
        object.__setattr__(
            self,
            "value",
            exact_decimal(self.value, "risk_metric_value.value"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "value": decimal_text(self.value),
            "unit": self.unit,
        }

    @classmethod
    def from_dict(cls, value: object) -> "RiskMetricValue":
        payload = _object(value, "risk_metric_value")
        _exact_fields(payload, {"name", "value", "unit"}, "risk_metric_value")
        return cls(
            name=_text(payload["name"], "risk_metric_value.name"),
            value=exact_decimal(payload["value"], "risk_metric_value.value"),
            unit=_text(payload["unit"], "risk_metric_value.unit"),
        )


@dataclass(frozen=True, slots=True)
class DerivativeRiskMetric:
    metric_id: RiskMetricId
    status: RiskMetricStatus
    values: tuple[RiskMetricValue, ...]
    reason: str | None
    source_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.metric_id, RiskMetricId):
            raise DerivativeResearchError("risk_metric_id_invalid")
        if not isinstance(self.status, RiskMetricStatus):
            raise DerivativeResearchError("risk_metric_status_invalid")
        if not isinstance(self.values, tuple) or any(
            not isinstance(item, RiskMetricValue) for item in self.values
        ):
            raise DerivativeResearchError("risk_metric_values_tuple_required")
        names = [item.name for item in self.values]
        if len(names) != len(set(names)):
            raise DerivativeResearchError("risk_metric_value_name_duplicate")
        if not isinstance(self.source_hashes, tuple) or not self.source_hashes:
            raise DerivativeResearchError("risk_metric_sources_required")
        if tuple(sorted(set(self.source_hashes))) != self.source_hashes:
            raise DerivativeResearchError("risk_metric_sources_not_unique_sorted")
        for source_hash in self.source_hashes:
            require_hash(source_hash, "risk_metric.source_hash")
        if self.status is RiskMetricStatus.AVAILABLE:
            if not self.values or self.reason is not None:
                raise DerivativeResearchError("risk_metric_available_fields_invalid")
        else:
            if self.values or self.reason is None:
                raise DerivativeResearchError("risk_metric_unavailable_fields_invalid")
            require_stable_id(self.reason, "risk_metric.reason")

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id.value,
            "status": self.status.value,
            "values": [item.as_dict() for item in self.values],
            "reason": self.reason,
            "source_hashes": list(self.source_hashes),
        }

    @classmethod
    def from_dict(cls, value: object) -> "DerivativeRiskMetric":
        payload = _object(value, "risk_metric")
        _exact_fields(
            payload,
            {"metric_id", "status", "values", "reason", "source_hashes"},
            "risk_metric",
        )
        try:
            metric_id = RiskMetricId(_text(payload["metric_id"], "risk_metric.id"))
            status = RiskMetricStatus(_text(payload["status"], "risk_metric.status"))
        except ValueError as exc:
            raise DerivativeResearchError("risk_metric_enum_invalid") from exc
        raw_reason = payload["reason"]
        if raw_reason is not None and not isinstance(raw_reason, str):
            raise DerivativeResearchError("risk_metric_reason_must_be_text")
        return cls(
            metric_id=metric_id,
            status=status,
            values=tuple(
                RiskMetricValue.from_dict(item)
                for item in _sequence(payload["values"], "risk_metric.values")
            ),
            reason=raw_reason,
            source_hashes=tuple(
                _text(item, "risk_metric.source_hash")
                for item in _sequence(
                    payload["source_hashes"], "risk_metric.source_hashes"
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class DerivativeRiskEvidence:
    """Complete S5-R01..R20 catalog for one typed simulation result."""

    risk_id: str
    version: str
    product_kind: RiskProductKind
    simulation_result_hash: str
    experiment_run_hash: str
    dataset_snapshot_hash: str
    evaluated_at: str
    metrics: tuple[DerivativeRiskMetric, ...]
    source_hashes: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = RISK_METRICS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RISK_METRICS_SCHEMA_VERSION:
            raise DerivativeResearchError("risk_evidence_schema_unsupported")
        require_stable_id(self.risk_id, "risk_evidence.risk_id")
        require_stable_id(self.version, "risk_evidence.version")
        if not isinstance(self.product_kind, RiskProductKind):
            raise DerivativeResearchError("risk_evidence_product_kind_invalid")
        parse_timestamp(self.evaluated_at, "risk_evidence.evaluated_at")
        required_sources = {
            self.simulation_result_hash,
            self.experiment_run_hash,
            self.dataset_snapshot_hash,
        }
        for value in required_sources:
            require_hash(value, "risk_evidence.binding_hash")
        if tuple(sorted(set(self.source_hashes))) != self.source_hashes:
            raise DerivativeResearchError("risk_evidence_sources_not_unique_sorted")
        for value in self.source_hashes:
            require_hash(value, "risk_evidence.source_hash")
        if not required_sources.issubset(set(self.source_hashes)):
            raise DerivativeResearchError("risk_evidence_source_binding_incomplete")
        expected_ids = tuple(RiskMetricId)
        if (
            not isinstance(self.metrics, tuple)
            or tuple(item.metric_id for item in self.metrics) != expected_ids
        ):
            raise DerivativeResearchError(
                "risk_evidence_complete_metric_catalog_required"
            )
        if any(
            not set(item.source_hashes).issubset(set(self.source_hashes))
            for item in self.metrics
        ):
            raise DerivativeResearchError("risk_metric_source_not_in_evidence")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="derivative_risk_evidence"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_risk_evidence",
            "risk_id": self.risk_id,
            "version": self.version,
            "product_kind": self.product_kind.value,
            "simulation_result_hash": self.simulation_result_hash,
            "experiment_run_hash": self.experiment_run_hash,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "evaluated_at": self.evaluated_at,
            "metrics": [item.as_dict() for item in self.metrics],
            "source_hashes": list(self.source_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "DerivativeRiskEvidence":
        payload = _object(value, "risk_evidence")
        _exact_fields(
            payload,
            {
                "schema_version",
                "artifact_type",
                "risk_id",
                "version",
                "product_kind",
                "simulation_result_hash",
                "experiment_run_hash",
                "dataset_snapshot_hash",
                "evaluated_at",
                "metrics",
                "source_hashes",
                "content_hash",
            },
            "risk_evidence",
        )
        if payload["artifact_type"] != "derivative_risk_evidence":
            raise DerivativeResearchError("risk_evidence_artifact_type_invalid")
        try:
            kind = RiskProductKind(
                _text(payload["product_kind"], "risk_evidence.product_kind")
            )
        except ValueError as exc:
            raise DerivativeResearchError("risk_evidence_product_kind_invalid") from exc
        result = cls(
            risk_id=_text(payload["risk_id"], "risk_evidence.risk_id"),
            version=_text(payload["version"], "risk_evidence.version"),
            product_kind=kind,
            simulation_result_hash=_text(
                payload["simulation_result_hash"], "risk_evidence.simulation_hash"
            ),
            experiment_run_hash=_text(
                payload["experiment_run_hash"], "risk_evidence.run_hash"
            ),
            dataset_snapshot_hash=_text(
                payload["dataset_snapshot_hash"], "risk_evidence.dataset_hash"
            ),
            evaluated_at=_text(payload["evaluated_at"], "risk_evidence.evaluated_at"),
            metrics=tuple(
                DerivativeRiskMetric.from_dict(item)
                for item in _sequence(payload["metrics"], "risk_evidence.metrics")
            ),
            source_hashes=tuple(
                _text(item, "risk_evidence.source_hash")
                for item in _sequence(
                    payload["source_hashes"], "risk_evidence.source_hashes"
                )
            ),
            schema_version=_integer(
                payload["schema_version"], "risk_evidence.schema_version"
            ),
        )
        serialized_hash = _text(payload["content_hash"], "risk_evidence.content_hash")
        if serialized_hash != result.content_hash:
            raise DerivativeResearchError("risk_evidence_content_hash_mismatch")
        return result


def build_futures_risk_evidence(
    *,
    risk_id: str,
    version: str,
    simulation_result: DerivativeSimulationEvidence,
    experiment_run: DerivativeExperimentRun,
    evaluated_at: str,
    stress_pairs: Sequence[tuple[FuturesStressInputs, FuturesStressExecution]] = (),
    portfolio_snapshot: PortfolioExposureSnapshot | None = None,
) -> DerivativeRiskEvidence:
    """Derive the futures S5-R catalog from the complete simulation stream."""

    sources, payload = _validated_base(
        simulation_result=simulation_result,
        experiment_run=experiment_run,
        evaluated_at=evaluated_at,
        expected_kind=SimulationProductKind.FUTURE,
    )
    simulator_bundle = _object(payload["simulator"], "risk.futures.simulator_bundle")
    margin_policy = _object(
        simulator_bundle["margin_policy"], "risk.futures.margin_policy"
    )
    initial_per_contract = _decimal(
        margin_policy["initial_margin_per_contract"], "risk.initial_margin"
    )
    collateral_fraction = _decimal(
        margin_policy["collateral_fraction"], "risk.collateral_fraction"
    )
    contracts = {
        _text(item["contract_id"], "risk.futures.contract_id"): item
        for item in _objects(simulator_bundle["contracts"], "risk.futures.contracts")
    }
    steps = _objects(payload["steps"], "risk.futures.steps")
    final_ledger = _object(steps[-1]["ledger"], "risk.futures.final_ledger")
    total_pnl = _decimal(final_ledger["cash_balance"], "risk.cash_balance") - _decimal(
        final_ledger["initial_cash"], "risk.initial_cash"
    )

    pnl_by_contract: dict[str, Decimal] = {}
    roll_yield = _ZERO
    roll_cost = _ZERO
    roll_observation_count = 0
    maximum_margin = _ZERO
    utilization_values: list[Decimal] = []
    utilization_unbounded = False
    for step in steps:
        ledger = _object(step["ledger"], "risk.futures.ledger")
        positions = _objects(ledger["positions"], "risk.futures.positions")
        requirement = sum(
            (
                Decimal(abs(_integer(item["quantity"], "risk.position.quantity")))
                * initial_per_contract
                for item in positions
            ),
            _ZERO,
        )
        maximum_margin = max(maximum_margin, requirement)
        collateral = (
            _decimal(ledger["cash_balance"], "risk.ledger.cash") * collateral_fraction
        )
        if requirement > 0 and collateral <= 0:
            utilization_unbounded = True
        elif collateral > 0:
            utilization_values.append(requirement / collateral)
        for fill in _objects(step["fills"], "risk.futures.fills"):
            contract_id = _text(fill["contract_id"], "risk.fill.contract_id")
            pnl_by_contract[contract_id] = pnl_by_contract.get(contract_id, _ZERO) + (
                _decimal(fill["realized_trade_pnl"], "risk.fill.realized_pnl")
                - _decimal(fill["commission"], "risk.fill.commission")
                - _decimal(fill["slippage_cost"], "risk.fill.slippage")
            )
        for settlement in _objects(
            step["settlement_events"], "risk.futures.settlements"
        ):
            contract_id = _text(
                settlement["contract_id"], "risk.settlement.contract_id"
            )
            pnl_by_contract[contract_id] = pnl_by_contract.get(
                contract_id, _ZERO
            ) + _decimal(settlement["variation_margin"], "risk.variation_margin")
        raw_roll = step["roll_execution"]
        if raw_roll is not None:
            roll = _object(raw_roll, "risk.futures.roll")
            roll_observation_count += 1
            roll_yield += _decimal(roll["roll_yield"], "risk.roll_yield")
            roll_cost += _decimal(roll["close_cost"], "risk.roll_close_cost")
            roll_cost += _decimal(roll["open_cost"], "risk.roll_open_cost")

    base_sources = _metric_sources(simulation_result, experiment_run)
    metrics: dict[RiskMetricId, DerivativeRiskMetric] = {}
    metrics[RiskMetricId.S5_R01] = (
        _available(
            RiskMetricId.S5_R01,
            tuple(
                RiskMetricValue(
                    name=f"contract.{contract_id}",
                    value=value,
                    unit="native_currency",
                )
                for contract_id, value in sorted(pnl_by_contract.items())
            ),
            base_sources,
        )
        if pnl_by_contract
        else _unavailable(
            RiskMetricId.S5_R01,
            RiskMetricStatus.UNAVAILABLE_SAMPLE,
            "no_contract_pnl_events",
            base_sources,
        )
    )
    metrics[RiskMetricId.S5_R02] = (
        _available(
            RiskMetricId.S5_R02,
            (
                RiskMetricValue(
                    "return_on_max_margin", total_pnl / maximum_margin, "ratio"
                ),
            ),
            base_sources,
        )
        if maximum_margin > 0
        else _unavailable(
            RiskMetricId.S5_R02,
            RiskMetricStatus.UNAVAILABLE_ZERO_DENOMINATOR,
            "maximum_margin_is_zero",
            base_sources,
        )
    )
    capital_seconds = _portfolio_capital_seconds(
        portfolio_snapshot, evaluated_at, simulation_result.dataset_snapshot_hash
    )
    if portfolio_snapshot is not None:
        sources.update(_collect_hashes(portfolio_snapshot.as_dict()))
    metrics[RiskMetricId.S5_R03] = (
        _available(
            RiskMetricId.S5_R03,
            (
                RiskMetricValue(
                    "return_per_capital_day",
                    total_pnl / (capital_seconds / _SECONDS_PER_DAY),
                    "native_currency_per_capital_day",
                ),
            ),
            _sources_with(base_sources, portfolio_snapshot),
        )
        if capital_seconds is not None and capital_seconds > 0
        else _unavailable(
            RiskMetricId.S5_R03,
            RiskMetricStatus.UNAVAILABLE_INPUT
            if capital_seconds is None
            else RiskMetricStatus.UNAVAILABLE_ZERO_DENOMINATOR,
            "capital_usage_evidence_missing"
            if capital_seconds is None
            else "capital_usage_time_is_zero",
            _sources_with(base_sources, portfolio_snapshot),
        )
    )
    metrics[RiskMetricId.S5_R04] = (
        _available(
            RiskMetricId.S5_R04,
            (RiskMetricValue("total_roll_yield", roll_yield, "native_currency"),),
            base_sources,
        )
        if roll_observation_count
        else _unavailable(
            RiskMetricId.S5_R04,
            RiskMetricStatus.UNAVAILABLE_SAMPLE,
            "roll_execution_sample_missing",
            base_sources,
        )
    )
    metrics[RiskMetricId.S5_R05] = (
        _available(
            RiskMetricId.S5_R05,
            (RiskMetricValue("total_roll_cost", roll_cost, "native_currency"),),
            base_sources,
        )
        if roll_observation_count
        else _unavailable(
            RiskMetricId.S5_R05,
            RiskMetricStatus.UNAVAILABLE_SAMPLE,
            "roll_execution_sample_missing",
            base_sources,
        )
    )
    if utilization_unbounded:
        metrics[RiskMetricId.S5_R06] = _unavailable(
            RiskMetricId.S5_R06,
            RiskMetricStatus.UNBOUNDED,
            "nonpositive_collateral_with_margin",
            base_sources,
        )
    elif utilization_values:
        metrics[RiskMetricId.S5_R06] = _available(
            RiskMetricId.S5_R06,
            (
                RiskMetricValue(
                    "maximum_margin_utilization", max(utilization_values), "ratio"
                ),
            ),
            base_sources,
        )
    else:
        metrics[RiskMetricId.S5_R06] = _unavailable(
            RiskMetricId.S5_R06,
            RiskMetricStatus.UNAVAILABLE_ZERO_DENOMINATOR,
            "no_positive_collateral_observation",
            base_sources,
        )

    final_positions = _objects(final_ledger["positions"], "risk.final_positions")
    expiry_basis: dict[str, Decimal] = {}
    term_exposure: dict[str, Decimal] = {}
    for position in final_positions:
        contract_id = _text(position["contract_id"], "risk.position.contract_id")
        contract = contracts.get(contract_id)
        if contract is None:
            raise DerivativeResearchError("risk_futures_contract_binding_missing")
        expiry = _text(contract["expiration_date"], "risk.contract.expiration")
        quantity = Decimal(_integer(position["quantity"], "risk.position.quantity"))
        expiry_basis[expiry] = (
            expiry_basis.get(expiry, _ZERO) + abs(quantity) * initial_per_contract
        )
        term_exposure[expiry] = term_exposure.get(expiry, _ZERO) + quantity
    total_expiry_basis = sum(expiry_basis.values(), _ZERO)
    metrics[RiskMetricId.S5_R07] = (
        _available(
            RiskMetricId.S5_R07,
            (
                RiskMetricValue(
                    "maximum_expiry_share",
                    max(expiry_basis.values()) / total_expiry_basis,
                    "ratio",
                ),
                RiskMetricValue(
                    "expiry_herfindahl",
                    sum(
                        (
                            (value / total_expiry_basis) ** 2
                            for value in expiry_basis.values()
                        ),
                        _ZERO,
                    ),
                    "ratio",
                ),
            ),
            base_sources,
        )
        if total_expiry_basis > 0
        else _unavailable(
            RiskMetricId.S5_R07,
            RiskMetricStatus.UNAVAILABLE_ZERO_DENOMINATOR,
            "expiry_margin_basis_is_zero",
            base_sources,
        )
    )
    metrics[RiskMetricId.S5_R08] = (
        _available(
            RiskMetricId.S5_R08,
            tuple(
                RiskMetricValue(f"expiry.{expiry}", quantity, "signed_contracts")
                for expiry, quantity in sorted(term_exposure.items())
            ),
            base_sources,
        )
        if term_exposure
        else _unavailable(
            RiskMetricId.S5_R08,
            RiskMetricStatus.UNAVAILABLE_SAMPLE,
            "no_open_term_structure_exposure",
            base_sources,
        )
    )

    price_limit_losses: list[Decimal] = []
    stress_hashes: set[str] = set()
    simulation_ledger_hashes = {
        _text(
            _object(step["ledger"], "risk.ledger")["content_hash"], "risk.ledger.hash"
        )
        for step in steps
    }
    for inputs, execution in stress_pairs:
        _validate_futures_stress_pair(
            inputs,
            execution,
            evaluated_at,
            simulation_result.execution_model_hash,
            simulation_ledger_hashes,
        )
        stress_hashes.update(_collect_hashes(inputs.as_dict()))
        stress_hashes.update(_collect_hashes(execution.as_dict()))
        if "PRICE_LIMIT_NO_EXIT" in execution.result.diagnostics:
            price_limit_losses.append(max(_ZERO, -execution.result.equity_delta))
    sources.update(stress_hashes)
    r09_sources = tuple(sorted(set(base_sources) | stress_hashes))
    metrics[RiskMetricId.S5_R09] = (
        _available(
            RiskMetricId.S5_R09,
            (
                RiskMetricValue(
                    "worst_price_limit_loss", max(price_limit_losses), "native_currency"
                ),
            ),
            r09_sources,
        )
        if price_limit_losses
        else _unavailable(
            RiskMetricId.S5_R09,
            RiskMetricStatus.UNAVAILABLE_SAMPLE,
            "price_limit_stress_sample_missing",
            r09_sources,
        )
    )
    for metric_id in tuple(RiskMetricId)[9:]:
        metrics[metric_id] = _not_applicable(metric_id, base_sources)
    return _evidence(
        risk_id=risk_id,
        version=version,
        product_kind=RiskProductKind.FUTURE,
        simulation_result=simulation_result,
        experiment_run=experiment_run,
        evaluated_at=evaluated_at,
        metrics=metrics,
        sources=sources,
    )


def build_option_risk_evidence(
    *,
    risk_id: str,
    version: str,
    simulation_result: DerivativeSimulationEvidence,
    experiment_run: DerivativeExperimentRun,
    evaluated_at: str,
    robustness_input: OptionRobustnessInput | None = None,
    robustness_executions: Sequence[OptionRobustnessExecution] = (),
    portfolio_snapshot: PortfolioExposureSnapshot | None = None,
    minimum_rate_sample: int = 2,
) -> DerivativeRiskEvidence:
    """Derive option risk metrics without inventing distributional evidence."""

    if isinstance(minimum_rate_sample, bool) or minimum_rate_sample <= 0:
        raise DerivativeResearchError("risk_minimum_rate_sample_invalid")
    if simulation_result.product_kind not in {
        SimulationProductKind.OPTION,
        SimulationProductKind.MULTI_LEG,
    }:
        raise DerivativeResearchError("risk_option_simulation_kind_required")
    sources, payload = _validated_base(
        simulation_result=simulation_result,
        experiment_run=experiment_run,
        evaluated_at=evaluated_at,
        expected_kind=simulation_result.product_kind,
    )
    base_sources = _metric_sources(simulation_result, experiment_run)
    robust_by_dimension: dict[OptionRobustnessDimension, OptionRobustnessExecution] = {}
    robust_sources: set[str] = set()
    if robustness_input is not None:
        _validate_option_robustness(
            robustness_input,
            robustness_executions,
            evaluated_at,
            simulation_result,
        )
        robust_sources.update(_collect_hashes(robustness_input.identity_payload()))
        robust_sources.add(robustness_input.content_hash)
        for execution in robustness_executions:
            robust_by_dimension[execution.dimension] = execution
            robust_sources.update(_collect_hashes(execution.identity_payload()))
            robust_sources.add(execution.content_hash)
        sources.update(robust_sources)
    elif robustness_executions:
        raise DerivativeResearchError("risk_option_robustness_input_required")
    if portfolio_snapshot is not None:
        _portfolio_capital_seconds(
            portfolio_snapshot, evaluated_at, simulation_result.dataset_snapshot_hash
        )
        sources.update(_collect_hashes(portfolio_snapshot.as_dict()))

    positions = _objects(payload["positions"], "risk.options.positions")
    marks = {
        _text(item["position_id"], "risk.option_mark.position_id"): item
        for item in _objects(payload["marks"], "risk.options.marks")
    }
    chain = _object(payload["product_chain"], "risk.options.chain")
    contracts = {
        _text(item["content_hash"], "risk.option_contract.hash"): item
        for item in _objects(chain["contracts"], "risk.option_contracts")
    }
    valuation_inputs = {
        _text(item["content_hash"], "risk.valuation.hash"): item
        for item in _objects(payload["valuation_inputs"], "risk.valuations")
    }
    greeks_by_input = {
        _text(item["valuation_input_hash"], "risk.greeks.input_hash"): item
        for item in _objects(payload["greeks"], "risk.greeks")
    }

    total_pnl = _ZERO
    premium_basis = _ZERO
    all_liquid = True
    net_greeks = {name: _ZERO for name in ("delta", "gamma", "vega", "theta_per_year")}
    expiry_basis: dict[str, Decimal] = {}
    early_eligible = 0
    net_call_slope = _ZERO
    for position in positions:
        position_id = _text(position["position_id"], "risk.position.id")
        contract_hash = _text(position["contract_hash"], "risk.position.contract_hash")
        contract = contracts.get(contract_hash)
        if contract is None:
            raise DerivativeResearchError("risk_option_contract_binding_missing")
        quantity = _decimal(position["quantity"], "risk.position.quantity")
        multiplier = _decimal(contract["multiplier"], "risk.contract.multiplier")
        entry_price = _decimal(position["entry_price"], "risk.position.entry_price")
        premium = abs(entry_price * quantity * multiplier)
        premium_basis += premium
        expiry = _text(contract["expiration_at"], "risk.contract.expiration")
        expiry_basis[expiry] = expiry_basis.get(expiry, _ZERO) + premium
        side_sign = Decimal("1") if position["side"] == "LONG" else Decimal("-1")
        if contract["option_type"] == "CALL":
            net_call_slope += side_sign * quantity * multiplier
        if contract["exercise_style"] != "EUROPEAN":
            early_eligible += 1
        mark = marks.get(position_id)
        if mark is None:
            raise DerivativeResearchError("risk_option_mark_binding_missing")
        raw_liquidation = mark["liquidation_pnl"]
        if raw_liquidation is None:
            all_liquid = False
        else:
            total_pnl += _decimal(raw_liquidation, "risk.mark.liquidation_pnl")
        input_hash = _text(mark["theoretical_input_hash"], "risk.mark.input_hash")
        if input_hash not in valuation_inputs:
            raise DerivativeResearchError("risk_option_valuation_binding_missing")
        greek = greeks_by_input.get(input_hash)
        if greek is None:
            raise DerivativeResearchError("risk_option_greeks_binding_missing")
        for greek_name in net_greeks:
            net_greeks[greek_name] += (
                side_sign
                * quantity
                * multiplier
                * _decimal(greek[greek_name], f"risk.greeks.{greek_name}")
            )

    metrics: dict[RiskMetricId, DerivativeRiskMetric] = {
        metric_id: _not_applicable(metric_id, base_sources)
        for metric_id in tuple(RiskMetricId)[:9]
    }
    if not all_liquid:
        metrics[RiskMetricId.S5_R10] = _unavailable(
            RiskMetricId.S5_R10,
            RiskMetricStatus.UNAVAILABLE_INPUT,
            "liquidation_value_missing",
            base_sources,
        )
    elif premium_basis == 0:
        metrics[RiskMetricId.S5_R10] = _unavailable(
            RiskMetricId.S5_R10,
            RiskMetricStatus.UNAVAILABLE_ZERO_DENOMINATOR,
            "premium_basis_is_zero",
            base_sources,
        )
    else:
        metrics[RiskMetricId.S5_R10] = _available(
            RiskMetricId.S5_R10,
            (RiskMetricValue("return_on_premium", total_pnl / premium_basis, "ratio"),),
            base_sources,
        )

    payoff_execution = robust_by_dimension.get(
        OptionRobustnessDimension.PAYOFF_TAIL_RISK
    )
    rare_execution = robust_by_dimension.get(OptionRobustnessDimension.SHORT_RARE_LOSS)
    option_robust_sources = tuple(sorted(set(base_sources) | robust_sources))
    if net_call_slope < 0:
        metrics[RiskMetricId.S5_R11] = _unavailable(
            RiskMetricId.S5_R11,
            RiskMetricStatus.UNBOUNDED,
            "short_call_tail_is_unbounded",
            base_sources,
        )
    elif payoff_execution is not None:
        metrics[RiskMetricId.S5_R11] = _available(
            RiskMetricId.S5_R11,
            (
                RiskMetricValue(
                    "maximum_observed_payoff_loss",
                    max(_ZERO, -payoff_execution.worst_value),
                    "native_currency",
                ),
            ),
            option_robust_sources,
        )
    else:
        metrics[RiskMetricId.S5_R11] = _unavailable(
            RiskMetricId.S5_R11,
            RiskMetricStatus.UNAVAILABLE_INPUT,
            "complete_payoff_tail_evidence_missing",
            base_sources,
        )
    expected_loss = _execution_metric(rare_execution, "rare_loss.weighted")
    metrics[RiskMetricId.S5_R12] = (
        _available(
            RiskMetricId.S5_R12,
            (
                RiskMetricValue(
                    "probability_weighted_expected_loss",
                    expected_loss,
                    "native_currency",
                ),
            ),
            option_robust_sources,
        )
        if expected_loss is not None
        else _unavailable(
            RiskMetricId.S5_R12,
            RiskMetricStatus.UNAVAILABLE_SAMPLE,
            "loss_probability_sample_missing",
            option_robust_sources,
        )
    )
    tail_loss = (
        None if payoff_execution is None else max(_ZERO, -payoff_execution.worst_value)
    )
    metrics[RiskMetricId.S5_R13] = (
        _available(
            RiskMetricId.S5_R13,
            (RiskMetricValue("payoff_tail_loss", tail_loss, "native_currency"),),
            option_robust_sources,
        )
        if tail_loss is not None
        else _unavailable(
            RiskMetricId.S5_R13,
            RiskMetricStatus.UNAVAILABLE_SAMPLE,
            "tail_scenario_sample_missing",
            option_robust_sources,
        )
    )
    metrics[RiskMetricId.S5_R14] = _available(
        RiskMetricId.S5_R14,
        (
            RiskMetricValue("net_delta", net_greeks["delta"], "underlying_units"),
            RiskMetricValue("net_gamma", net_greeks["gamma"], "gamma_units"),
            RiskMetricValue("net_vega", net_greeks["vega"], "vega_units"),
            RiskMetricValue(
                "net_theta", net_greeks["theta_per_year"], "native_currency_per_year"
            ),
        ),
        base_sources,
    )
    iv_execution = robust_by_dimension.get(OptionRobustnessDimension.IV_MODEL)
    skew_execution = robust_by_dimension.get(OptionRobustnessDimension.SKEW_SHIFT)
    metrics[RiskMetricId.S5_R15] = _sensitivity_metric(
        RiskMetricId.S5_R15,
        iv_execution,
        "iv_model_adverse_change",
        option_robust_sources,
    )
    metrics[RiskMetricId.S5_R16] = _sensitivity_metric(
        RiskMetricId.S5_R16,
        skew_execution,
        "skew_adverse_change",
        option_robust_sources,
    )
    metrics[RiskMetricId.S5_R17] = (
        _available(
            RiskMetricId.S5_R17,
            (
                RiskMetricValue(
                    "maximum_expiry_premium_share",
                    max(expiry_basis.values()) / premium_basis,
                    "ratio",
                ),
                RiskMetricValue(
                    "expiry_premium_herfindahl",
                    sum(
                        (
                            (value / premium_basis) ** 2
                            for value in expiry_basis.values()
                        ),
                        _ZERO,
                    ),
                    "ratio",
                ),
            ),
            base_sources,
        )
        if premium_basis > 0
        else _unavailable(
            RiskMetricId.S5_R17,
            RiskMetricStatus.UNAVAILABLE_ZERO_DENOMINATOR,
            "expiry_premium_basis_is_zero",
            base_sources,
        )
    )
    quotes = _objects(chain["quotes"], "risk.option_quotes")
    metrics[RiskMetricId.S5_R18] = (
        _available(
            RiskMetricId.S5_R18,
            (
                RiskMetricValue(
                    "zero_bid_occurrence_rate",
                    Decimal(sum(item["state"] == "ZERO_BID" for item in quotes))
                    / Decimal(len(quotes)),
                    "ratio",
                ),
            ),
            base_sources,
        )
        if len(quotes) >= minimum_rate_sample
        else _unavailable(
            RiskMetricId.S5_R18,
            RiskMetricStatus.UNAVAILABLE_SAMPLE,
            "quote_sample_below_minimum",
            base_sources,
        )
    )
    lifecycle = _objects(payload["lifecycle_events"], "risk.option_lifecycle")
    early_events = sum(
        item["event_type"] in {"EXERCISE", "ASSIGNMENT"} for item in lifecycle
    )
    metrics[RiskMetricId.S5_R19] = (
        _available(
            RiskMetricId.S5_R19,
            (
                RiskMetricValue(
                    "early_exercise_event_rate",
                    Decimal(early_events) / Decimal(early_eligible),
                    "ratio",
                ),
            ),
            base_sources,
        )
        if early_eligible > 0
        else _unavailable(
            RiskMetricId.S5_R19,
            RiskMetricStatus.UNAVAILABLE_ZERO_DENOMINATOR,
            "no_early_exercise_eligible_positions",
            base_sources,
        )
    )
    multileg_states: list[str] = []
    if simulation_result.product_kind is SimulationProductKind.MULTI_LEG:
        multileg = _object(payload["multi_leg_execution"], "risk.multileg")
        multileg_states.append(_text(multileg["state"], "risk.multileg.state"))
    if robustness_input is not None:
        multileg_states.extend(
            item.state.value for item in robustness_input.multileg_results
        )
    metrics[RiskMetricId.S5_R20] = (
        _available(
            RiskMetricId.S5_R20,
            (
                RiskMetricValue(
                    "multileg_fill_failure_rate",
                    Decimal(
                        sum(
                            state != MultiLegState.FILLED.value
                            for state in multileg_states
                        )
                    )
                    / Decimal(len(multileg_states)),
                    "ratio",
                ),
            ),
            option_robust_sources,
        )
        if len(multileg_states) >= minimum_rate_sample
        else _unavailable(
            RiskMetricId.S5_R20,
            RiskMetricStatus.UNAVAILABLE_SAMPLE
            if simulation_result.product_kind is SimulationProductKind.MULTI_LEG
            else RiskMetricStatus.NOT_APPLICABLE,
            "multileg_sample_below_minimum"
            if simulation_result.product_kind is SimulationProductKind.MULTI_LEG
            else "metric_not_applicable_to_single_option",
            option_robust_sources,
        )
    )
    kind = RiskProductKind(simulation_result.product_kind.value)
    return _evidence(
        risk_id=risk_id,
        version=version,
        product_kind=kind,
        simulation_result=simulation_result,
        experiment_run=experiment_run,
        evaluated_at=evaluated_at,
        metrics=metrics,
        sources=sources,
    )


def _validated_base(
    *,
    simulation_result: DerivativeSimulationEvidence,
    experiment_run: DerivativeExperimentRun,
    evaluated_at: str,
    expected_kind: SimulationProductKind,
) -> tuple[set[str], Mapping[str, object]]:
    if not isinstance(simulation_result, DerivativeSimulationEvidence):
        raise DerivativeResearchError("risk_simulation_evidence_required")
    if simulation_result.product_kind is not expected_kind:
        raise DerivativeResearchError("risk_simulation_product_mismatch")
    if not isinstance(experiment_run, DerivativeExperimentRun):
        raise DerivativeResearchError("risk_experiment_run_required")
    restored = DerivativeSimulationEvidence.from_dict(simulation_result.as_dict())
    if restored != simulation_result:
        raise DerivativeResearchError("risk_simulation_deep_validation_failed")
    expected_run_hash = sha256_prefixed(
        experiment_run.identity_payload(), label="derivative_experiment_run"
    )
    if expected_run_hash != experiment_run.content_hash:
        raise DerivativeResearchError("risk_experiment_run_content_hash_mismatch")
    simulation_result.validate_run(experiment_run)
    evaluated = parse_timestamp(evaluated_at, "risk.evaluated_at")
    if evaluated < parse_timestamp(experiment_run.finished_at, "risk.run.finished_at"):
        raise DerivativeResearchError("risk_evaluated_before_run_finished")
    sources = _collect_hashes(simulation_result.as_dict())
    sources.update(_collect_hashes(experiment_run.as_dict()))
    sources.update(
        {
            simulation_result.content_hash,
            experiment_run.content_hash,
            simulation_result.dataset_snapshot_hash,
        }
    )
    return sources, simulation_result.simulation_payload


def _validate_futures_stress_pair(
    inputs: FuturesStressInputs,
    execution: FuturesStressExecution,
    evaluated_at: str,
    simulator_hash: str,
    ledger_hashes: set[str],
) -> None:
    if not isinstance(inputs, FuturesStressInputs) or not isinstance(
        execution, FuturesStressExecution
    ):
        raise DerivativeResearchError("risk_futures_stress_pair_invalid")
    if (
        sha256_prefixed(inputs.identity_payload(), label="futures_stress_inputs")
        != inputs.content_hash
    ):
        raise DerivativeResearchError("risk_futures_stress_input_tampered")
    if (
        sha256_prefixed(
            execution.result.identity_payload(), label="futures_stress_result"
        )
        != execution.result.content_hash
    ):
        raise DerivativeResearchError("risk_futures_stress_result_tampered")
    if (
        sha256_prefixed(execution.identity_payload(), label="futures_stress_execution")
        != execution.content_hash
    ):
        raise DerivativeResearchError("risk_futures_stress_execution_tampered")
    if execution.input_hash != inputs.content_hash:
        raise DerivativeResearchError("risk_futures_stress_input_mismatch")
    if execution.simulator_hash != simulator_hash:
        raise DerivativeResearchError("risk_futures_stress_simulator_mismatch")
    if execution.ledger_hash not in ledger_hashes:
        raise DerivativeResearchError("risk_futures_stress_ledger_not_in_simulation")
    if parse_timestamp(inputs.as_of, "risk.stress.as_of") > parse_timestamp(
        evaluated_at, "risk.evaluated_at"
    ):
        raise DerivativeResearchError("risk_futures_stress_from_future")


def _validate_option_robustness(
    inputs: OptionRobustnessInput,
    executions: Sequence[OptionRobustnessExecution],
    evaluated_at: str,
    simulation_result: DerivativeSimulationEvidence,
) -> None:
    if not isinstance(inputs, OptionRobustnessInput):
        raise DerivativeResearchError("risk_option_robustness_input_invalid")
    if (
        sha256_prefixed(inputs.identity_payload(), label="option_robustness_input")
        != inputs.content_hash
    ):
        raise DerivativeResearchError("risk_option_robustness_input_tampered")
    if parse_timestamp(
        inputs.chain_snapshot.knowledge_time, "risk.robustness.knowledge_time"
    ) > parse_timestamp(evaluated_at, "risk.evaluated_at"):
        raise DerivativeResearchError("risk_option_robustness_from_future")
    if inputs.chain_snapshot.content_hash != simulation_result.product_chain_hash:
        raise DerivativeResearchError("risk_option_robustness_chain_mismatch")
    payload = simulation_result.simulation_payload
    simulation_positions = {
        _text(item["position_id"], "risk.robustness.position_id"): _text(
            item["content_hash"], "risk.robustness.position_hash"
        )
        for item in _objects(payload["positions"], "risk.robustness.positions")
    }
    input_positions = {
        item.position_id: item.content_hash
        for item in inputs.positions
        if item.position_id in inputs.priced_position_ids
    }
    if simulation_positions != input_positions:
        raise DerivativeResearchError("risk_option_robustness_position_mismatch")
    exact_groups = (
        (
            "valuation",
            {
                _text(item["content_hash"], "risk.robustness.valuation_hash")
                for item in _objects(
                    payload["valuation_inputs"], "risk.robustness.valuations"
                )
            },
            {item.content_hash for item in inputs.valuation_inputs},
        ),
        (
            "implied_volatility",
            {
                _text(item["content_hash"], "risk.robustness.iv_hash")
                for item in _objects(
                    payload["implied_volatilities"], "risk.robustness.iv_results"
                )
            },
            {item.content_hash for item in inputs.base_iv_results},
        ),
        (
            "greeks",
            {
                _text(item["content_hash"], "risk.robustness.greeks_hash")
                for item in _objects(payload["greeks"], "risk.robustness.greeks")
            },
            {item.content_hash for item in inputs.greeks},
        ),
        (
            "marks",
            {
                _text(item["content_hash"], "risk.robustness.mark_hash")
                for item in _objects(payload["marks"], "risk.robustness.marks")
            },
            {item.content_hash for item in inputs.marks},
        ),
    )
    for label, observed, expected in exact_groups:
        if observed != expected:
            raise DerivativeResearchError(f"risk_option_robustness_{label}_mismatch")
    simulation_fill_hashes = {
        _text(item["content_hash"], "risk.robustness.fill_hash")
        for item in _objects(payload["fills"], "risk.robustness.fills")
    }
    expected_fill_hashes = {
        item.source_fill_hash
        for item in inputs.positions
        if item.position_id in inputs.priced_position_ids
    }
    if simulation_fill_hashes != expected_fill_hashes:
        raise DerivativeResearchError("risk_option_robustness_fill_mismatch")
    seen: set[OptionRobustnessDimension] = set()
    for execution in executions:
        if not isinstance(execution, OptionRobustnessExecution):
            raise DerivativeResearchError("risk_option_robustness_execution_invalid")
        if execution.dimension in seen:
            raise DerivativeResearchError("risk_option_robustness_dimension_duplicate")
        seen.add(execution.dimension)
        if execution.input_hash != inputs.content_hash:
            raise DerivativeResearchError("risk_option_robustness_input_mismatch")
        if (
            sha256_prefixed(
                execution.identity_payload(), label="option_robustness_execution"
            )
            != execution.content_hash
        ):
            raise DerivativeResearchError("risk_option_robustness_execution_tampered")


def _portfolio_capital_seconds(
    snapshot: PortfolioExposureSnapshot | None,
    evaluated_at: str,
    dataset_hash: str,
) -> Decimal | None:
    if snapshot is None:
        return None
    if not isinstance(snapshot, PortfolioExposureSnapshot):
        raise DerivativeResearchError("risk_portfolio_snapshot_invalid")
    if (
        sha256_prefixed(
            snapshot.identity_payload(), label="portfolio_exposure_snapshot"
        )
        != snapshot.content_hash
    ):
        raise DerivativeResearchError("risk_portfolio_snapshot_tampered")
    if parse_timestamp(snapshot.as_of, "risk.portfolio.as_of") > parse_timestamp(
        evaluated_at, "risk.evaluated_at"
    ):
        raise DerivativeResearchError("risk_portfolio_snapshot_from_future")
    currencies = {item.currency for item in snapshot.positions}
    if len(currencies) > 1:
        raise DerivativeResearchError("risk_implicit_fx_conversion_forbidden")
    if any(
        item.evidence_hashes.dataset_hash != dataset_hash for item in snapshot.positions
    ):
        raise DerivativeResearchError("risk_portfolio_dataset_mismatch")
    return snapshot.total_capital_use_seconds


def _evidence(
    *,
    risk_id: str,
    version: str,
    product_kind: RiskProductKind,
    simulation_result: DerivativeSimulationEvidence,
    experiment_run: DerivativeExperimentRun,
    evaluated_at: str,
    metrics: Mapping[RiskMetricId, DerivativeRiskMetric],
    sources: set[str],
) -> DerivativeRiskEvidence:
    return DerivativeRiskEvidence(
        risk_id=risk_id,
        version=version,
        product_kind=product_kind,
        simulation_result_hash=simulation_result.content_hash,
        experiment_run_hash=experiment_run.content_hash,
        dataset_snapshot_hash=simulation_result.dataset_snapshot_hash,
        evaluated_at=evaluated_at,
        metrics=tuple(metrics[item] for item in RiskMetricId),
        source_hashes=tuple(sorted(sources)),
    )


def _metric_sources(
    simulation_result: DerivativeSimulationEvidence,
    experiment_run: DerivativeExperimentRun,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                simulation_result.content_hash,
                experiment_run.content_hash,
                simulation_result.dataset_snapshot_hash,
            }
        )
    )


def _sources_with(
    sources: tuple[str, ...], artifact: PortfolioExposureSnapshot | None
) -> tuple[str, ...]:
    if artifact is None:
        return sources
    return tuple(sorted({*sources, artifact.content_hash}))


def _available(
    metric_id: RiskMetricId,
    values: tuple[RiskMetricValue, ...],
    sources: tuple[str, ...],
) -> DerivativeRiskMetric:
    return DerivativeRiskMetric(
        metric_id=metric_id,
        status=RiskMetricStatus.AVAILABLE,
        values=values,
        reason=None,
        source_hashes=sources,
    )


def _unavailable(
    metric_id: RiskMetricId,
    status: RiskMetricStatus,
    reason: str,
    sources: tuple[str, ...],
) -> DerivativeRiskMetric:
    return DerivativeRiskMetric(
        metric_id=metric_id,
        status=status,
        values=(),
        reason=reason,
        source_hashes=sources,
    )


def _not_applicable(
    metric_id: RiskMetricId, sources: tuple[str, ...]
) -> DerivativeRiskMetric:
    return _unavailable(
        metric_id,
        RiskMetricStatus.NOT_APPLICABLE,
        "metric_not_applicable_to_product",
        sources,
    )


def _sensitivity_metric(
    metric_id: RiskMetricId,
    execution: OptionRobustnessExecution | None,
    name: str,
    sources: tuple[str, ...],
) -> DerivativeRiskMetric:
    if execution is None:
        return _unavailable(
            metric_id,
            RiskMetricStatus.UNAVAILABLE_SAMPLE,
            "sensitivity_comparison_missing",
            sources,
        )
    return _available(
        metric_id,
        (RiskMetricValue(name, abs(execution.adverse_change), "native_currency"),),
        sources,
    )


def _execution_metric(
    execution: OptionRobustnessExecution | None, metric_id: str
) -> Decimal | None:
    if execution is None:
        return None
    return next(
        (item.value for item in execution.metrics if item.metric_id == metric_id),
        None,
    )


def _collect_hashes(value: object) -> set[str]:
    hashes: set[str] = set()
    if isinstance(value, Mapping):
        for nested in value.values():
            hashes.update(_collect_hashes(nested))
    elif isinstance(value, (list, tuple)):
        for nested in value:
            hashes.update(_collect_hashes(nested))
    elif isinstance(value, str):
        try:
            hashes.add(require_hash(value, "risk.source_hash"))
        except DerivativeResearchError:
            pass
    return hashes


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise DerivativeResearchError(f"{label}_must_be_object")
    return value


def _objects(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    return tuple(_object(item, f"{label}[]") for item in _sequence(value, label))


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise DerivativeResearchError(f"{label}_must_be_array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DerivativeResearchError(f"{label}_must_be_text")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DerivativeResearchError(f"{label}_must_be_integer")
    return value


def _decimal(value: object, label: str) -> Decimal:
    return exact_decimal(value, label)


def _exact_fields(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise DerivativeResearchError(f"{label}_fields_invalid")
