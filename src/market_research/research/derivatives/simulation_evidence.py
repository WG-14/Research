"""Hash-bound evidence for offline derivative simulation results.

The futures and options modules expose immutable product-domain objects, but a
``DerivativeExperimentRun`` can only carry hashes.  This module supplies the
missing typed result artifact: it embeds the actual canonical simulator
objects, verifies their internal links, derives the event-stream hash, and can
then validate the run's two result bindings.

There is deliberately no network, account, broker, or order-submission API in
this module.  ``OptionOrderIntentEvidence`` is a research intent recorded next
to an already simulated fill; it is not an executable order contract.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Sequence, TypeVar

from market_research.research.hashing import sha256_prefixed

from .common import (
    DERIVATIVE_RESEARCH_SCHEMA_VERSION,
    DerivativeDatasetSnapshot,
    DerivativeExperimentRun,
    DerivativeExperimentSpec,
    DerivativeResearchError,
    InstrumentKind,
    decimal_text,
    exact_decimal,
    parse_timestamp,
    require_hash,
    require_stable_id,
)
from .futures import (
    ContractChainSnapshot,
    FuturesLifecycleEvent,
    FuturesOrderIntent,
    FuturesSimulator,
    SimulationStep,
)
from .options import (
    BlackScholesModel,
    FillStatus,
    IVFailure,
    ImpliedVolatilityResult,
    MultiLegExecutionPolicy,
    MultiLegExecutionResult,
    MultiLegOrder,
    OptionChainSnapshot,
    OptionFill,
    OptionGreeks,
    OptionLifecycleEvent,
    OptionMark,
    OptionPosition,
    OptionType,
    TransactionSide,
    ValuationInputSnapshot,
    solve_black_scholes_implied_volatility,
)


SIMULATION_EVIDENCE_SCHEMA_VERSION = DERIVATIVE_RESEARCH_SCHEMA_VERSION
_EnumT = TypeVar("_EnumT", bound=StrEnum)


class SimulationEvidenceError(DerivativeResearchError):
    """A simulation result is incomplete, inconsistent, or has been altered."""


class SimulationProductKind(StrEnum):
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    MULTI_LEG = "MULTI_LEG"


class OptionExecutionMode(StrEnum):
    SINGLE = "SINGLE"
    SIMULTANEOUS = "SIMULTANEOUS"
    SEQUENTIAL = "SEQUENTIAL"


@dataclass(frozen=True, slots=True)
class OptionExecutionPolicy:
    """Exact, versioned parameters used by the deterministic option fill path."""

    policy_id: str
    policy_version: str
    fill_model_version: str
    mode: OptionExecutionMode
    fee_per_contract: Decimal
    slippage_ticks: int
    allow_partial: bool
    allow_illiquid: bool
    maximum_leg_time_skew_seconds: int | None = None
    cost_model_hash: str = field(init=False)
    fill_model_hash: str = field(init=False)
    content_hash: str = field(init=False)
    schema_version: int = SIMULATION_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.policy_id, "option_execution_policy.policy_id")
        require_stable_id(self.policy_version, "option_execution_policy.policy_version")
        require_stable_id(
            self.fill_model_version,
            "option_execution_policy.fill_model_version",
        )
        if not isinstance(self.mode, OptionExecutionMode):
            raise SimulationEvidenceError("option_execution_policy_mode_invalid")
        fee = exact_decimal(
            self.fee_per_contract, "option_execution_policy.fee_per_contract"
        )
        if fee < 0:
            raise SimulationEvidenceError("option_execution_policy_fee_negative")
        if (
            isinstance(self.slippage_ticks, bool)
            or not isinstance(self.slippage_ticks, int)
            or self.slippage_ticks < 0
        ):
            raise SimulationEvidenceError("option_execution_policy_slippage_invalid")
        skew = self.maximum_leg_time_skew_seconds
        if self.mode is OptionExecutionMode.SINGLE:
            if skew is not None:
                raise SimulationEvidenceError("single_option_policy_leg_skew_forbidden")
        elif isinstance(skew, bool) or not isinstance(skew, int) or skew < 0:
            raise SimulationEvidenceError("multileg_option_policy_leg_skew_required")
        object.__setattr__(self, "fee_per_contract", fee)
        object.__setattr__(
            self,
            "cost_model_hash",
            sha256_prefixed(self.cost_model_payload(), label="option_cost_model"),
        )
        object.__setattr__(
            self,
            "fill_model_hash",
            sha256_prefixed(self.fill_model_payload(), label="option_fill_model"),
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="option_execution_policy"),
        )

    def cost_model_payload(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "fee_model": "FLAT_PER_FILLED_CONTRACT",
            "fee_per_contract": decimal_text(self.fee_per_contract),
        }

    def fill_model_payload(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "fill_model_version": self.fill_model_version,
            "method": "CROSS_RECORDED_TWO_SIDED_QUOTE",
            "mode": self.mode.value,
            "slippage_ticks": self.slippage_ticks,
            "allow_partial": self.allow_partial,
            "allow_illiquid": self.allow_illiquid,
            "maximum_leg_time_skew_seconds": self.maximum_leg_time_skew_seconds,
        }

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "fill_model_version": self.fill_model_version,
            "mode": self.mode.value,
            "fee_per_contract": decimal_text(self.fee_per_contract),
            "slippage_ticks": self.slippage_ticks,
            "allow_partial": self.allow_partial,
            "allow_illiquid": self.allow_illiquid,
            "maximum_leg_time_skew_seconds": self.maximum_leg_time_skew_seconds,
            "cost_model_hash": self.cost_model_hash,
            "fill_model_hash": self.fill_model_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class OptionOrderIntentEvidence:
    """A non-operational research intent bound to one recorded option quote."""

    order_id: str
    contract_id: str
    side: TransactionSide
    quantity: Decimal
    requested_at: str
    quote_hash: str
    execution_policy_hash: str
    content_hash: str = field(init=False)
    schema_version: int = SIMULATION_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.order_id, "option_order_intent.order_id")
        require_stable_id(self.contract_id, "option_order_intent.contract_id")
        if not isinstance(self.side, TransactionSide):
            raise SimulationEvidenceError("option_order_intent_side_invalid")
        quantity = exact_decimal(
            self.quantity, "option_order_intent.quantity", positive=True
        )
        parse_timestamp(self.requested_at, "option_order_intent.requested_at")
        require_hash(self.quote_hash, "option_order_intent.quote_hash")
        require_hash(
            self.execution_policy_hash,
            "option_order_intent.execution_policy_hash",
        )
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="option_order_intent_evidence"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "order_id": self.order_id,
            "contract_id": self.contract_id,
            "side": self.side.value,
            "quantity": decimal_text(self.quantity),
            "requested_at": self.requested_at,
            "quote_hash": self.quote_hash,
            "execution_policy_hash": self.execution_policy_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def futures_fill_model_hash(simulator: FuturesSimulator) -> str:
    """Return the exact fill-model binding expected by an experiment spec."""

    if not isinstance(simulator, FuturesSimulator):
        raise SimulationEvidenceError("futures_simulator_required")
    return sha256_prefixed(
        {
            "simulator_id": simulator.simulator_id,
            "simulator_version": simulator.simulator_version,
            "method": "LISTED_CONTRACT_QUOTE_TICK_ROUNDED",
            "cost_policy_hash": simulator.cost_policy.content_hash,
        },
        label="futures_fill_model",
    )


@dataclass(frozen=True, slots=True)
class DerivativeSimulationEvidence:
    """One discriminated, self-contained derivative simulation result artifact."""

    simulation_id: str
    product_kind: SimulationProductKind
    simulation_payload_json: str = field(repr=False)
    dataset_snapshot_hash: str = field(init=False)
    product_chain_hash: str = field(init=False)
    experiment_spec_hash: str = field(init=False)
    execution_model_hash: str = field(init=False)
    event_stream_hash: str = field(init=False)
    content_hash: str = field(init=False)
    schema_version: int = SIMULATION_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.simulation_id, "simulation_evidence.simulation_id")
        if not isinstance(self.product_kind, SimulationProductKind):
            raise SimulationEvidenceError("simulation_product_kind_invalid")
        payload = _decode_canonical_object(
            self.simulation_payload_json, "simulation_evidence.simulation_payload"
        )
        bindings = _validate_simulation_payload(self.product_kind, payload)
        for name, value in bindings.items():
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="derivative_simulation_evidence"
            ),
        )

    @property
    def simulation_payload(self) -> dict[str, object]:
        return _decode_canonical_object(
            self.simulation_payload_json, "simulation_evidence.simulation_payload"
        )

    @classmethod
    def from_futures(
        cls,
        *,
        simulation_id: str,
        dataset: DerivativeDatasetSnapshot,
        experiment_spec: DerivativeExperimentSpec,
        chain: ContractChainSnapshot,
        simulator: FuturesSimulator,
        orders: Sequence[FuturesOrderIntent],
        steps: Sequence[SimulationStep],
        lifecycle_events: Sequence[FuturesLifecycleEvent] = (),
    ) -> "DerivativeSimulationEvidence":
        _require_actual_types(
            dataset,
            experiment_spec,
            chain,
            simulator,
            orders,
            steps,
            lifecycle_events,
        )
        _require_members(orders, FuturesOrderIntent, "futures_orders")
        _require_members(steps, SimulationStep, "futures_steps")
        _require_members(
            lifecycle_events, FuturesLifecycleEvent, "futures_lifecycle_events"
        )
        payload = {
            "dataset_snapshot": dataset.as_dict(),
            "experiment_spec": experiment_spec.as_dict(),
            "product_chain": chain.as_dict(),
            "simulator": _futures_simulator_payload(simulator),
            "orders": [item.as_dict() for item in orders],
            "steps": [item.as_dict() for item in steps],
            "lifecycle_events": [item.as_dict() for item in lifecycle_events],
        }
        return cls(
            simulation_id=simulation_id,
            product_kind=SimulationProductKind.FUTURE,
            simulation_payload_json=_canonical_json(payload),
        )

    @classmethod
    def from_option(
        cls,
        *,
        simulation_id: str,
        dataset: DerivativeDatasetSnapshot,
        experiment_spec: DerivativeExperimentSpec,
        chain: OptionChainSnapshot,
        execution_policy: OptionExecutionPolicy,
        valuation_model: BlackScholesModel,
        orders: Sequence[OptionOrderIntentEvidence],
        fills: Sequence[OptionFill],
        positions: Sequence[OptionPosition],
        valuation_inputs: Sequence[ValuationInputSnapshot],
        implied_volatilities: Sequence[ImpliedVolatilityResult],
        greeks: Sequence[OptionGreeks],
        marks: Sequence[OptionMark],
        lifecycle_events: Sequence[OptionLifecycleEvent] = (),
        lifecycle_datasets: Sequence[DerivativeDatasetSnapshot] = (),
        lifecycle_observation_dataset_hashes: Sequence[str] = (),
    ) -> "DerivativeSimulationEvidence":
        if execution_policy.mode is not OptionExecutionMode.SINGLE:
            raise SimulationEvidenceError("single_option_execution_mode_required")
        payload = _option_payload(
            dataset=dataset,
            experiment_spec=experiment_spec,
            chain=chain,
            execution_policy=execution_policy,
            valuation_model=valuation_model,
            orders=orders,
            fills=fills,
            positions=positions,
            valuation_inputs=valuation_inputs,
            implied_volatilities=implied_volatilities,
            greeks=greeks,
            marks=marks,
            lifecycle_events=lifecycle_events,
            lifecycle_datasets=lifecycle_datasets,
            lifecycle_observation_dataset_hashes=(lifecycle_observation_dataset_hashes),
        )
        return cls(
            simulation_id=simulation_id,
            product_kind=SimulationProductKind.OPTION,
            simulation_payload_json=_canonical_json(payload),
        )

    @classmethod
    def from_multi_leg(
        cls,
        *,
        simulation_id: str,
        dataset: DerivativeDatasetSnapshot,
        experiment_spec: DerivativeExperimentSpec,
        chain: OptionChainSnapshot,
        execution_policy: OptionExecutionPolicy,
        valuation_model: BlackScholesModel,
        order: MultiLegOrder,
        execution_result: MultiLegExecutionResult,
        positions: Sequence[OptionPosition],
        valuation_inputs: Sequence[ValuationInputSnapshot],
        implied_volatilities: Sequence[ImpliedVolatilityResult],
        greeks: Sequence[OptionGreeks],
        marks: Sequence[OptionMark],
        lifecycle_events: Sequence[OptionLifecycleEvent] = (),
        lifecycle_datasets: Sequence[DerivativeDatasetSnapshot] = (),
        lifecycle_observation_dataset_hashes: Sequence[str] = (),
        participation_rates: Sequence[tuple[str, Decimal]] = (),
    ) -> "DerivativeSimulationEvidence":
        if execution_policy.mode is OptionExecutionMode.SINGLE:
            raise SimulationEvidenceError("multileg_option_execution_mode_required")
        if not isinstance(order, MultiLegOrder):
            raise SimulationEvidenceError("option_multileg_order_required")
        if not isinstance(execution_result, MultiLegExecutionResult):
            raise SimulationEvidenceError("option_multileg_execution_required")
        payload = _option_payload(
            dataset=dataset,
            experiment_spec=experiment_spec,
            chain=chain,
            execution_policy=execution_policy,
            valuation_model=valuation_model,
            orders=(),
            fills=execution_result.attempted_fills,
            positions=positions,
            valuation_inputs=valuation_inputs,
            implied_volatilities=implied_volatilities,
            greeks=greeks,
            marks=marks,
            lifecycle_events=lifecycle_events,
            lifecycle_datasets=lifecycle_datasets,
            lifecycle_observation_dataset_hashes=(lifecycle_observation_dataset_hashes),
        )
        payload["multi_leg_order"] = {
            **order.identity_payload(),
            "content_hash": order.content_hash,
        }
        payload["multi_leg_execution"] = {
            **execution_result.identity_payload(),
            "content_hash": execution_result.content_hash,
        }
        participation_leg_ids = [leg_id for leg_id, _rate in participation_rates]
        if len(participation_leg_ids) != len(set(participation_leg_ids)):
            raise SimulationEvidenceError("multileg_participation_leg_duplicate")
        known_leg_ids = {item.leg_id for item in order.legs}
        if not set(participation_leg_ids).issubset(known_leg_ids):
            raise SimulationEvidenceError("multileg_participation_leg_unknown")
        normalized_participation: dict[str, str] = {}
        for leg_id, raw_rate in participation_rates:
            rate = exact_decimal(raw_rate, "multileg_participation_rate", positive=True)
            if rate > 1:
                raise SimulationEvidenceError("multileg_participation_rate_invalid")
            normalized_participation[leg_id] = decimal_text(rate)
        payload["multi_leg_participation_rates"] = normalized_participation
        return cls(
            simulation_id=simulation_id,
            product_kind=SimulationProductKind.MULTI_LEG,
            simulation_payload_json=_canonical_json(payload),
        )

    @classmethod
    def from_dict(cls, value: object) -> "DerivativeSimulationEvidence":
        payload = _object(value, "simulation_evidence")
        _exact_fields(
            payload,
            {
                "schema_version",
                "artifact_type",
                "simulation_id",
                "product_kind",
                "dataset_snapshot_hash",
                "product_chain_hash",
                "experiment_spec_hash",
                "execution_model_hash",
                "event_stream_hash",
                "simulation_payload",
                "content_hash",
            },
            "simulation_evidence",
        )
        if payload["artifact_type"] != "derivative_simulation_evidence":
            raise SimulationEvidenceError("simulation_artifact_type_invalid")
        schema_version = _integer(
            payload["schema_version"], "simulation_evidence.schema_version"
        )
        kind = _enum_value(
            SimulationProductKind,
            payload["product_kind"],
            "simulation_evidence.product_kind",
        )
        result = cls(
            simulation_id=_text(
                payload["simulation_id"], "simulation_evidence.simulation_id"
            ),
            product_kind=kind,
            simulation_payload_json=_canonical_json(
                _object(
                    payload["simulation_payload"],
                    "simulation_evidence.simulation_payload",
                )
            ),
            schema_version=schema_version,
        )
        for name in (
            "dataset_snapshot_hash",
            "product_chain_hash",
            "experiment_spec_hash",
            "execution_model_hash",
            "event_stream_hash",
            "content_hash",
        ):
            serialized = _text(payload[name], f"simulation_evidence.{name}")
            if serialized != getattr(result, name):
                raise SimulationEvidenceError(f"simulation_{name}_mismatch")
        return result

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_simulation_evidence",
            "simulation_id": self.simulation_id,
            "product_kind": self.product_kind.value,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "product_chain_hash": self.product_chain_hash,
            "experiment_spec_hash": self.experiment_spec_hash,
            "execution_model_hash": self.execution_model_hash,
            "event_stream_hash": self.event_stream_hash,
            "simulation_payload": self.simulation_payload,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def validate_run(self, run: DerivativeExperimentRun) -> None:
        """Require a successful run to bind this exact result and event stream."""

        if not isinstance(run, DerivativeExperimentRun):
            raise SimulationEvidenceError("derivative_experiment_run_required")
        if run.status != "SUCCEEDED":
            raise SimulationEvidenceError("simulation_run_not_succeeded")
        if run.experiment_spec_hash != self.experiment_spec_hash:
            raise SimulationEvidenceError("simulation_run_experiment_mismatch")
        if run.dataset_snapshot_hash != self.dataset_snapshot_hash:
            raise SimulationEvidenceError("simulation_run_dataset_mismatch")
        observation_datasets = (
            ()
            if self.product_kind is SimulationProductKind.FUTURE
            else _objects(
                self.simulation_payload.get("lifecycle_dataset_snapshots"),
                "simulation.lifecycle_dataset_snapshots",
            )
        )
        expected_observation_hashes = tuple(
            _text(item.get("content_hash"), "observation_dataset.content_hash")
            for item in observation_datasets
        )
        if run.observation_dataset_snapshot_hashes != expected_observation_hashes:
            raise SimulationEvidenceError("simulation_run_observation_dataset_mismatch")
        run_finished = parse_timestamp(run.finished_at, "derivative_run.finished_at")
        if any(
            parse_timestamp(
                _text(item.get("knowledge_time"), "observation_dataset.knowledge_time"),
                "observation_dataset.knowledge_time",
            )
            > run_finished
            for item in observation_datasets
        ):
            raise SimulationEvidenceError(
                "simulation_observation_dataset_after_run_finished"
            )
        if run.event_stream_hash != self.event_stream_hash:
            raise SimulationEvidenceError("simulation_run_event_stream_mismatch")
        if run.result_artifact_hash != self.content_hash:
            raise SimulationEvidenceError("simulation_run_result_artifact_mismatch")


def _option_payload(
    *,
    dataset: DerivativeDatasetSnapshot,
    experiment_spec: DerivativeExperimentSpec,
    chain: OptionChainSnapshot,
    execution_policy: OptionExecutionPolicy,
    valuation_model: BlackScholesModel,
    orders: Sequence[OptionOrderIntentEvidence],
    fills: Sequence[OptionFill],
    positions: Sequence[OptionPosition],
    valuation_inputs: Sequence[ValuationInputSnapshot],
    implied_volatilities: Sequence[ImpliedVolatilityResult],
    greeks: Sequence[OptionGreeks],
    marks: Sequence[OptionMark],
    lifecycle_events: Sequence[OptionLifecycleEvent],
    lifecycle_datasets: Sequence[DerivativeDatasetSnapshot],
    lifecycle_observation_dataset_hashes: Sequence[str],
) -> dict[str, object]:
    _require_actual_types(
        dataset,
        experiment_spec,
        chain,
        execution_policy,
        orders,
        fills,
        positions,
        valuation_inputs,
        implied_volatilities,
        greeks,
        marks,
        lifecycle_events,
    )
    if not isinstance(valuation_model, BlackScholesModel):
        raise SimulationEvidenceError("option_valuation_model_required")
    if any(
        not isinstance(item, DerivativeDatasetSnapshot) for item in lifecycle_datasets
    ):
        raise SimulationEvidenceError("option_lifecycle_dataset_invalid")
    if len(lifecycle_events) != len(lifecycle_observation_dataset_hashes):
        raise SimulationEvidenceError("option_lifecycle_dataset_binding_required")
    dataset_hashes = tuple(item.content_hash for item in lifecycle_datasets)
    if len(dataset_hashes) != len(set(dataset_hashes)):
        raise SimulationEvidenceError("option_lifecycle_dataset_duplicate")
    for value in lifecycle_observation_dataset_hashes:
        require_hash(value, "option_lifecycle_observation_dataset_hash")
    if set(lifecycle_observation_dataset_hashes) != set(dataset_hashes):
        raise SimulationEvidenceError("option_lifecycle_dataset_binding_required")
    if experiment_spec.valuation_model_hash != valuation_model.content_hash:
        raise SimulationEvidenceError("option_spec_valuation_model_mismatch")
    _require_members(orders, OptionOrderIntentEvidence, "option_orders")
    _require_members(fills, OptionFill, "option_fills")
    _require_members(positions, OptionPosition, "option_positions")
    _require_members(
        valuation_inputs, ValuationInputSnapshot, "option_valuation_inputs"
    )
    _require_members(
        implied_volatilities,
        ImpliedVolatilityResult,
        "option_implied_volatilities",
    )
    _require_members(greeks, OptionGreeks, "option_greeks")
    _require_members(marks, OptionMark, "option_marks")
    _require_members(lifecycle_events, OptionLifecycleEvent, "option_lifecycle_events")
    return {
        "dataset_snapshot": dataset.as_dict(),
        "experiment_spec": experiment_spec.as_dict(),
        "product_chain": chain.as_dict(),
        "execution_policy": execution_policy.as_dict(),
        "valuation_model": valuation_model.as_dict(),
        "orders": [item.as_dict() for item in orders],
        "fills": [item.as_dict() for item in fills],
        "positions": [
            {**item.identity_payload(), "content_hash": item.content_hash}
            for item in positions
        ],
        "valuation_inputs": [item.as_dict() for item in valuation_inputs],
        "implied_volatilities": [item.as_dict() for item in implied_volatilities],
        "greeks": [item.as_dict() for item in greeks],
        "marks": [
            {**item.identity_payload(), "content_hash": item.content_hash}
            for item in marks
        ],
        "lifecycle_events": [
            {**item.identity_payload(), "content_hash": item.content_hash}
            for item in lifecycle_events
        ],
        "lifecycle_dataset_snapshots": [item.as_dict() for item in lifecycle_datasets],
        "lifecycle_observation_bindings": [
            {
                "event_hash": event.content_hash,
                "dataset_snapshot_hash": dataset_hash,
            }
            for event, dataset_hash in zip(
                lifecycle_events,
                lifecycle_observation_dataset_hashes,
                strict=True,
            )
        ],
    }


def _futures_simulator_payload(simulator: FuturesSimulator) -> dict[str, object]:
    return {
        "simulator": simulator.as_dict(),
        "contracts": [item.as_dict() for item in simulator.contracts],
        "settlement_policy": simulator.settlement_policy.as_dict(),
        "margin_policy": simulator.margin_policy.as_dict(),
        "expiry_policy": simulator.expiry_policy.as_dict(),
        "cost_policy": simulator.cost_policy.as_dict(),
        "fill_model_hash": futures_fill_model_hash(simulator),
    }


def _require_actual_types(
    dataset: object,
    experiment_spec: object,
    chain: object,
    execution_model: object,
    orders: Sequence[object],
    events: Sequence[object],
    *groups: Sequence[object],
) -> None:
    if not isinstance(dataset, DerivativeDatasetSnapshot):
        raise SimulationEvidenceError("derivative_dataset_snapshot_required")
    if not isinstance(experiment_spec, DerivativeExperimentSpec):
        raise SimulationEvidenceError("derivative_experiment_spec_required")
    if not isinstance(chain, (ContractChainSnapshot, OptionChainSnapshot)):
        raise SimulationEvidenceError("derivative_product_chain_required")
    if not isinstance(execution_model, (FuturesSimulator, OptionExecutionPolicy)):
        raise SimulationEvidenceError("derivative_execution_model_required")
    for group in (orders, events, *groups):
        if not isinstance(group, Sequence):
            raise SimulationEvidenceError("simulation_domain_sequence_required")


def _require_members(
    values: Sequence[object], expected_type: type[object], label: str
) -> None:
    if any(not isinstance(value, expected_type) for value in values):
        raise SimulationEvidenceError(f"{label}_contain_invalid_type")


def _validate_simulation_payload(
    kind: SimulationProductKind, payload: Mapping[str, object]
) -> dict[str, str]:
    if kind is SimulationProductKind.FUTURE:
        return _validate_futures_payload(payload)
    return _validate_option_payload(
        payload, multileg=kind is SimulationProductKind.MULTI_LEG
    )


def _validate_common(
    payload: Mapping[str, object], *, instrument: InstrumentKind
) -> tuple[Mapping[str, object], Mapping[str, object], Mapping[str, object]]:
    dataset = _object(payload.get("dataset_snapshot"), "simulation.dataset_snapshot")
    spec = _object(payload.get("experiment_spec"), "simulation.experiment_spec")
    chain = _object(payload.get("product_chain"), "simulation.product_chain")
    _verify_content_hash(dataset, "derivative_dataset_snapshot", "dataset")
    _verify_content_hash(spec, "derivative_experiment_spec", "experiment_spec")
    instrument_kind = _text(dataset.get("instrument_kind"), "dataset.instrument_kind")
    if instrument_kind != instrument.value:
        raise SimulationEvidenceError("simulation_dataset_instrument_mismatch")
    dataset_hash = _text(dataset["content_hash"], "dataset.content_hash")
    spec_hash = _text(spec["content_hash"], "experiment_spec.content_hash")
    chain_hash = _text(chain.get("content_hash"), "product_chain.content_hash")
    require_hash(chain_hash, "simulation.product_chain.content_hash")
    if spec.get("dataset_snapshot_hash") != dataset_hash:
        raise SimulationEvidenceError("simulation_spec_dataset_mismatch")
    chain_hashes = _texts(
        dataset.get("chain_snapshot_hashes"), "dataset.chain_snapshot_hashes"
    )
    if chain_hash not in chain_hashes:
        raise SimulationEvidenceError("simulation_dataset_chain_mismatch")
    raw_manifest_hashes = set(
        _texts(dataset.get("raw_manifest_hashes"), "dataset.raw_manifest_hashes")
    )
    chain_source_hashes = set(
        _texts(
            chain.get("source_manifest_hashes"),
            "product_chain.source_manifest_hashes",
        )
    )
    if not chain_source_hashes or not chain_source_hashes.issubset(raw_manifest_hashes):
        raise SimulationEvidenceError("simulation_chain_source_unbound")
    chain_time_field = (
        "observed_at" if instrument is InstrumentKind.FUTURE else "knowledge_time"
    )
    chain_time = parse_timestamp(
        _text(chain.get(chain_time_field), f"product_chain.{chain_time_field}"),
        f"product_chain.{chain_time_field}",
    )
    if not (
        parse_timestamp(
            _text(dataset.get("period_start"), "dataset.period_start"),
            "dataset.period_start",
        )
        <= chain_time
        <= parse_timestamp(
            _text(dataset.get("period_end"), "dataset.period_end"),
            "dataset.period_end",
        )
    ):
        raise SimulationEvidenceError("simulation_chain_outside_dataset_period")
    dataset_universe = set(_texts(dataset.get("universe_ids"), "dataset.universe_ids"))
    if not dataset_universe:
        raise SimulationEvidenceError("simulation_dataset_universe_empty")
    filter_contract = _object(dataset.get("filter_contract"), "dataset.filter_contract")
    filter_label = (
        "futures_dataset_filter_contract"
        if instrument is InstrumentKind.FUTURE
        else "option_dataset_filter_contract"
    )
    _verify_content_hash(filter_contract, filter_label, "dataset.filter_contract")
    if filter_contract.get("content_hash") not in _texts(
        dataset.get("policy_hashes"), "dataset.policy_hashes"
    ):
        raise SimulationEvidenceError("simulation_dataset_filter_hash_unbound")
    require_hash(dataset_hash, "simulation.dataset_snapshot_hash")
    require_hash(spec_hash, "simulation.experiment_spec_hash")
    return dataset, spec, chain


def _validate_futures_payload(payload: Mapping[str, object]) -> dict[str, str]:
    _exact_fields(
        payload,
        {
            "dataset_snapshot",
            "experiment_spec",
            "product_chain",
            "simulator",
            "orders",
            "steps",
            "lifecycle_events",
        },
        "futures_simulation",
    )
    dataset, spec, chain = _validate_common(payload, instrument=InstrumentKind.FUTURE)
    _verify_content_hash(chain, "futures_contract_chain", "futures_chain")
    simulator_bundle = _object(payload["simulator"], "futures_simulator_bundle")
    _exact_fields(
        simulator_bundle,
        {
            "simulator",
            "contracts",
            "settlement_policy",
            "margin_policy",
            "expiry_policy",
            "cost_policy",
            "fill_model_hash",
        },
        "futures_simulator_bundle",
    )
    simulator = _object(simulator_bundle["simulator"], "futures_simulator")
    policies = (
        ("settlement_policy", "futures_settlement_policy"),
        ("margin_policy", "futures_margin_policy"),
        ("expiry_policy", "futures_expiry_policy"),
        ("cost_policy", "futures_cost_policy"),
    )
    for key, label in policies:
        _verify_content_hash(
            _object(simulator_bundle[key], f"futures_simulator.{key}"),
            label,
            f"futures_simulator.{key}",
        )
    contract_payloads = _objects(
        simulator_bundle["contracts"], "futures_simulator.contracts"
    )
    if not contract_payloads:
        raise SimulationEvidenceError("futures_simulator_contracts_empty")
    for contract in contract_payloads:
        _verify_content_hash(contract, "futures_contract", "futures_contract")
    _verify_content_hash(simulator, "futures_simulator", "futures_simulator")
    contract_hashes = [item["content_hash"] for item in contract_payloads]
    if simulator.get("contract_hashes") != contract_hashes:
        raise SimulationEvidenceError("futures_simulator_contract_binding_mismatch")
    for key, simulator_key in (
        ("settlement_policy", "settlement_policy_hash"),
        ("margin_policy", "margin_policy_hash"),
        ("expiry_policy", "expiry_policy_hash"),
        ("cost_policy", "cost_policy_hash"),
    ):
        policy = _object(simulator_bundle[key], f"futures_simulator.{key}")
        if simulator.get(simulator_key) != policy.get("content_hash"):
            raise SimulationEvidenceError("futures_simulator_policy_binding_mismatch")
    simulator_hash = _text(simulator["content_hash"], "futures_simulator.content_hash")
    cost_hash = _text(
        _object(simulator_bundle["cost_policy"], "futures_cost_policy")["content_hash"],
        "futures_cost_policy.content_hash",
    )
    fill_hash = _text(
        simulator_bundle["fill_model_hash"], "futures_simulator.fill_model_hash"
    )
    expected_fill_hash = sha256_prefixed(
        {
            "simulator_id": simulator.get("simulator_id"),
            "simulator_version": simulator.get("simulator_version"),
            "method": "LISTED_CONTRACT_QUOTE_TICK_ROUNDED",
            "cost_policy_hash": cost_hash,
        },
        label="futures_fill_model",
    )
    if fill_hash != expected_fill_hash:
        raise SimulationEvidenceError("futures_fill_model_hash_mismatch")
    if spec.get("simulation_policy_hash") != simulator_hash:
        raise SimulationEvidenceError("futures_spec_simulator_mismatch")
    if spec.get("cost_model_hash") != cost_hash:
        raise SimulationEvidenceError("futures_spec_cost_model_mismatch")
    if spec.get("fill_model_hash") != fill_hash:
        raise SimulationEvidenceError("futures_spec_fill_model_mismatch")

    chain_contracts = _objects(chain.get("contracts"), "futures_chain.contracts")
    chain_quotes = _objects(chain.get("quotes"), "futures_chain.quotes")
    chain_lifecycle = _objects(
        chain.get("lifecycle_events"), "futures_chain.lifecycle_events"
    )
    chain_source_hashes = set(
        _texts(
            chain.get("source_manifest_hashes"),
            "futures_chain.source_manifest_hashes",
        )
    )
    dataset_source_hashes = set(
        _texts(dataset.get("raw_manifest_hashes"), "dataset.raw_manifest_hashes")
    )
    chain_observed_at = parse_timestamp(
        _text(chain.get("observed_at"), "futures_chain.observed_at"),
        "futures_chain.observed_at",
    )
    dataset_knowledge_time = parse_timestamp(
        _text(dataset.get("knowledge_time"), "dataset.knowledge_time"),
        "dataset.knowledge_time",
    )
    dataset_period_start = parse_timestamp(
        _text(dataset.get("period_start"), "dataset.period_start"),
        "dataset.period_start",
    )
    dataset_period_end = parse_timestamp(
        _text(dataset.get("period_end"), "dataset.period_end"),
        "dataset.period_end",
    )
    for contract in chain_contracts:
        _verify_content_hash(contract, "futures_contract", "futures_chain.contract")
    for quote in chain_quotes:
        _verify_content_hash(
            quote, "futures_contract_quote", "futures_chain.contract_quote"
        )
        source_hash = _text(
            quote.get("source_hash"), "futures_chain.contract_quote.source_hash"
        )
        if source_hash not in chain_source_hashes:
            raise SimulationEvidenceError("futures_chain_quote_source_unbound")
        if source_hash not in dataset_source_hashes:
            raise SimulationEvidenceError("futures_chain_quote_source_not_in_dataset")
    chain_lifecycle_hashes: set[str] = set()
    for event in chain_lifecycle:
        _verify_content_hash(
            event, "futures_lifecycle_event", "futures_chain.lifecycle_event"
        )
        event_hash = _text(
            event.get("content_hash"), "futures_chain.lifecycle_event.content_hash"
        )
        chain_lifecycle_hashes.add(event_hash)
        source_hash = _text(
            event.get("source_hash"), "futures_chain.lifecycle_event.source_hash"
        )
        if source_hash not in chain_source_hashes:
            raise SimulationEvidenceError("futures_chain_lifecycle_source_unbound")
        if source_hash not in dataset_source_hashes:
            raise SimulationEvidenceError(
                "futures_chain_lifecycle_source_not_in_dataset"
            )
        availability = _object(
            event.get("availability"), "futures_chain.lifecycle_event.availability"
        )
        event_at = parse_timestamp(
            _text(event.get("event_at"), "futures_chain.lifecycle_event.event_at"),
            "futures_chain.lifecycle_event.event_at",
        )
        availability_times = tuple(
            parse_timestamp(
                _text(
                    availability.get(field_name),
                    f"futures_chain.lifecycle_event.availability.{field_name}",
                ),
                f"futures_chain.lifecycle_event.availability.{field_name}",
            )
            for field_name in (
                "event_at",
                "published_at",
                "provider_received_at",
                "system_received_at",
                "processed_at",
            )
        )
        if event_at != availability_times[0] or availability_times != tuple(
            sorted(availability_times)
        ):
            raise SimulationEvidenceError(
                "futures_chain_lifecycle_availability_invalid"
            )
        if availability_times[-1] > chain_observed_at:
            raise SimulationEvidenceError(
                "futures_chain_lifecycle_not_known_at_snapshot"
            )
        if not dataset_period_start <= event_at <= dataset_period_end:
            raise SimulationEvidenceError(
                "futures_chain_lifecycle_outside_dataset_period"
            )
        if availability_times[-1] > dataset_knowledge_time:
            raise SimulationEvidenceError(
                "futures_chain_lifecycle_unknown_at_dataset_knowledge_time"
            )
    chain_contract_hashes = {
        _text(item.get("content_hash"), "futures_chain.contract_hash")
        for item in chain_contracts
    }
    if not set(contract_hashes).issubset(chain_contract_hashes):
        raise SimulationEvidenceError("futures_simulator_contract_not_in_chain")
    universe = set(_texts(dataset["universe_ids"], "dataset.universe_ids"))
    used_contract_ids = {
        _text(item.get("contract_id"), "futures_contract.contract_id")
        for item in contract_payloads
    }
    if not used_contract_ids.issubset(universe):
        raise SimulationEvidenceError("futures_contract_not_in_dataset_universe")
    quote_hashes = {
        _text(item.get("content_hash"), "futures_chain.quote_hash")
        for item in chain_quotes
    }

    orders = _objects(payload["orders"], "futures_simulation.orders")
    steps = _objects(payload["steps"], "futures_simulation.steps")
    lifecycle = _objects(
        payload["lifecycle_events"], "futures_simulation.lifecycle_events"
    )
    if not orders or not steps:
        raise SimulationEvidenceError("futures_orders_and_steps_required")
    order_hashes: set[str] = set()
    event_items: list[dict[str, str]] = []
    for order in orders:
        _verify_content_hash(order, "futures_order_intent", "futures_order")
        order_hash = _text(order["content_hash"], "futures_order.content_hash")
        if order_hash in order_hashes:
            raise SimulationEvidenceError("futures_order_hash_duplicate")
        order_hashes.add(order_hash)
        if order.get("contract_id") not in used_contract_ids:
            raise SimulationEvidenceError("futures_order_contract_not_in_simulator")
        event_items.append({"kind": "ORDER", "content_hash": order_hash})

    known_event_hashes: set[str] = set()
    prior_ledger_events: tuple[str, ...] = ()
    ledger_id: str | None = None
    initial_cash: str | None = None
    for step in steps:
        _verify_futures_step(step)
        step_hash = _text(step["content_hash"], "futures_step.content_hash")
        fills = _objects(step.get("fills"), "futures_step.fills")
        settlements = _objects(
            step.get("settlement_events"), "futures_step.settlement_events"
        )
        for fill in fills:
            if fill.get("intent_hash") not in order_hashes:
                raise SimulationEvidenceError("futures_fill_order_binding_missing")
            if fill.get("quote_hash") not in quote_hashes:
                raise SimulationEvidenceError("futures_fill_quote_not_in_chain")
            known_event_hashes.add(
                _text(fill["content_hash"], "futures_fill.content_hash")
            )
        known_event_hashes.update(
            _text(item["content_hash"], "futures_settlement.content_hash")
            for item in settlements
        )
        if any(item.get("quote_hash") not in quote_hashes for item in settlements):
            raise SimulationEvidenceError("futures_settlement_quote_not_in_chain")
        for optional_key in ("margin_call", "roll_execution"):
            raw = step.get(optional_key)
            if raw is not None:
                optional = _object(raw, f"futures_step.{optional_key}")
                known_event_hashes.add(
                    _text(optional["content_hash"], f"futures_step.{optional_key}.hash")
                )
        ledger = _object(step.get("ledger"), "futures_step.ledger")
        if ledger.get("failed") is not False:
            raise SimulationEvidenceError("futures_failed_ledger_not_publishable")
        current_ledger_id = _text(ledger.get("ledger_id"), "futures_ledger.ledger_id")
        current_initial_cash = _text(
            ledger.get("initial_cash"), "futures_ledger.initial_cash"
        )
        if ledger_id is None:
            ledger_id = current_ledger_id
            initial_cash = current_initial_cash
        elif current_ledger_id != ledger_id or current_initial_cash != initial_cash:
            raise SimulationEvidenceError("futures_ledger_identity_rewritten")
        ledger_events = _texts(
            ledger.get("event_hashes"), "futures_ledger.event_hashes"
        )
        if len(set(ledger_events)) != len(ledger_events):
            raise SimulationEvidenceError("futures_ledger_event_duplicate")
        if (
            prior_ledger_events
            and tuple(ledger_events[: len(prior_ledger_events)]) != prior_ledger_events
        ):
            raise SimulationEvidenceError("futures_ledger_event_history_rewritten")
        if not set(ledger_events).issubset(known_event_hashes):
            raise SimulationEvidenceError("futures_ledger_event_not_in_steps")
        if set(ledger_events) != known_event_hashes:
            raise SimulationEvidenceError("futures_ledger_event_history_incomplete")
        prior_ledger_events = ledger_events
        event_items.append({"kind": "STEP", "content_hash": step_hash})
    if not prior_ledger_events:
        raise SimulationEvidenceError("futures_final_ledger_event_history_empty")
    for event in lifecycle:
        _verify_content_hash(event, "futures_lifecycle_event", "futures_lifecycle")
        event_hash = _text(event["content_hash"], "futures_lifecycle.content_hash")
        if event_hash not in chain_lifecycle_hashes:
            raise SimulationEvidenceError("futures_lifecycle_not_in_chain")
        event_items.append({"kind": "LIFECYCLE", "content_hash": event_hash})

    return {
        "dataset_snapshot_hash": _text(dataset["content_hash"], "dataset.content_hash"),
        "product_chain_hash": _text(chain["content_hash"], "chain.content_hash"),
        "experiment_spec_hash": _text(spec["content_hash"], "spec.content_hash"),
        "execution_model_hash": simulator_hash,
        "event_stream_hash": sha256_prefixed(
            event_items, label="derivative_simulation_event_stream"
        ),
    }


def _verify_futures_step(step: Mapping[str, object]) -> None:
    fills = _objects(step.get("fills"), "futures_step.fills")
    settlements = _objects(
        step.get("settlement_events"), "futures_step.settlement_events"
    )
    for fill in fills:
        _verify_content_hash(fill, "futures_fill", "futures_fill")
    for settlement in settlements:
        _verify_content_hash(
            settlement, "futures_settlement_event", "futures_settlement"
        )
    optional = (
        ("margin_call", "futures_margin_call"),
        ("roll_execution", "futures_roll_execution"),
    )
    for key, label in optional:
        raw = step.get(key)
        if raw is not None:
            _verify_content_hash(_object(raw, f"futures_step.{key}"), label, key)
    ledger = _object(step.get("ledger"), "futures_step.ledger")
    for position in _objects(ledger.get("positions"), "futures_ledger.positions"):
        _verify_content_hash(position, "futures_position", "futures_position")
    _verify_content_hash(ledger, "futures_ledger", "futures_ledger")
    _verify_content_hash(step, "futures_simulation_step", "futures_step")


def _validate_option_lifecycle_dataset(
    dataset: Mapping[str, object],
) -> None:
    _verify_content_hash(
        dataset,
        "derivative_dataset_snapshot",
        "option_lifecycle_dataset",
    )
    if dataset.get("instrument_kind") != InstrumentKind.OPTION.value:
        raise SimulationEvidenceError("option_lifecycle_dataset_instrument_mismatch")
    period_start = parse_timestamp(
        _text(dataset.get("period_start"), "option_lifecycle_dataset.period_start"),
        "option_lifecycle_dataset.period_start",
    )
    period_end = parse_timestamp(
        _text(dataset.get("period_end"), "option_lifecycle_dataset.period_end"),
        "option_lifecycle_dataset.period_end",
    )
    knowledge_time = parse_timestamp(
        _text(dataset.get("knowledge_time"), "option_lifecycle_dataset.knowledge_time"),
        "option_lifecycle_dataset.knowledge_time",
    )
    if period_start >= period_end or period_end > knowledge_time:
        raise SimulationEvidenceError("option_lifecycle_dataset_time_range_invalid")
    if not _texts(
        dataset.get("raw_manifest_hashes"),
        "option_lifecycle_dataset.raw_manifest_hashes",
    ):
        raise SimulationEvidenceError("option_lifecycle_dataset_sources_required")
    if not _texts(dataset.get("universe_ids"), "option_lifecycle_dataset.universe_ids"):
        raise SimulationEvidenceError("option_lifecycle_dataset_universe_required")
    filter_contract = _object(
        dataset.get("filter_contract"), "option_lifecycle_dataset.filter_contract"
    )
    _verify_content_hash(
        filter_contract,
        "option_dataset_filter_contract",
        "option_lifecycle_dataset.filter_contract",
    )
    if filter_contract.get("content_hash") not in _texts(
        dataset.get("policy_hashes"), "option_lifecycle_dataset.policy_hashes"
    ):
        raise SimulationEvidenceError("option_lifecycle_dataset_filter_hash_unbound")


def _validate_option_payload(
    payload: Mapping[str, object], *, multileg: bool
) -> dict[str, str]:
    expected = {
        "dataset_snapshot",
        "experiment_spec",
        "product_chain",
        "execution_policy",
        "valuation_model",
        "orders",
        "fills",
        "positions",
        "valuation_inputs",
        "implied_volatilities",
        "greeks",
        "marks",
        "lifecycle_events",
        "lifecycle_dataset_snapshots",
        "lifecycle_observation_bindings",
    }
    if multileg:
        expected.update(
            {
                "multi_leg_order",
                "multi_leg_execution",
                "multi_leg_participation_rates",
            }
        )
    _exact_fields(payload, expected, "option_simulation")
    dataset, spec, chain = _validate_common(payload, instrument=InstrumentKind.OPTION)
    _verify_content_hash(chain, "option_chain_snapshot", "option_chain")
    policy = _object(payload["execution_policy"], "option_execution_policy")
    _validate_option_policy(policy)
    policy_hash = _text(policy["content_hash"], "option_execution_policy.content_hash")
    valuation_model = _object(payload["valuation_model"], "option_valuation_model")
    _verify_content_hash(
        valuation_model, "option_valuation_model", "option_valuation_model"
    )
    valuation_model_hash = _text(
        valuation_model["content_hash"], "option_valuation_model.content_hash"
    )
    if spec.get("simulation_policy_hash") != policy_hash:
        raise SimulationEvidenceError("option_spec_execution_policy_mismatch")
    if spec.get("cost_model_hash") != policy.get("cost_model_hash"):
        raise SimulationEvidenceError("option_spec_cost_model_mismatch")
    if spec.get("fill_model_hash") != policy.get("fill_model_hash"):
        raise SimulationEvidenceError("option_spec_fill_model_mismatch")
    if spec.get("valuation_model_hash") != valuation_model_hash:
        raise SimulationEvidenceError("option_spec_valuation_model_mismatch")
    mode = _text(policy.get("mode"), "option_execution_policy.mode")
    if multileg == (mode == OptionExecutionMode.SINGLE.value):
        raise SimulationEvidenceError("option_execution_mode_product_mismatch")

    contracts = _objects(chain.get("contracts"), "option_chain.contracts")
    quotes = _objects(chain.get("quotes"), "option_chain.quotes")
    contract_by_id: dict[str, Mapping[str, object]] = {}
    contract_hash_by_id: dict[str, str] = {}
    quote_by_contract: dict[str, Mapping[str, object]] = {}
    for contract in contracts:
        _verify_content_hash(contract, "option_contract", "option_contract")
        contract_id = _text(contract.get("contract_id"), "option_contract.contract_id")
        contract_by_id[contract_id] = contract
        contract_hash_by_id[contract_id] = _text(
            contract["content_hash"], "option_contract.content_hash"
        )
    for quote in quotes:
        _verify_content_hash(quote, "option_quote", "option_quote")
        quote_by_contract[
            _text(quote.get("contract_id"), "option_quote.contract_id")
        ] = quote
    universe = set(_texts(dataset["universe_ids"], "dataset.universe_ids"))
    if not set(contract_by_id).issubset(universe):
        raise SimulationEvidenceError("option_contract_not_in_dataset_universe")

    orders = _objects(payload["orders"], "option_simulation.orders")
    fills = _objects(payload["fills"], "option_simulation.fills")
    positions = _objects(payload["positions"], "option_simulation.positions")
    valuation_inputs = _objects(
        payload["valuation_inputs"], "option_simulation.valuation_inputs"
    )
    iv_results = _objects(
        payload["implied_volatilities"], "option_simulation.implied_volatilities"
    )
    greeks = _objects(payload["greeks"], "option_simulation.greeks")
    marks = _objects(payload["marks"], "option_simulation.marks")
    lifecycle = _objects(
        payload["lifecycle_events"], "option_simulation.lifecycle_events"
    )
    lifecycle_datasets = _objects(
        payload["lifecycle_dataset_snapshots"],
        "option_simulation.lifecycle_dataset_snapshots",
    )
    lifecycle_dataset_by_hash: dict[str, Mapping[str, object]] = {}
    for lifecycle_dataset in lifecycle_datasets:
        _validate_option_lifecycle_dataset(lifecycle_dataset)
        lifecycle_dataset_hash = _text(
            lifecycle_dataset.get("content_hash"),
            "option_lifecycle_dataset.content_hash",
        )
        if (
            lifecycle_dataset_hash == dataset.get("content_hash")
            or lifecycle_dataset_hash in lifecycle_dataset_by_hash
        ):
            raise SimulationEvidenceError(
                "option_lifecycle_dataset_not_separate_unique"
            )
        lifecycle_dataset_by_hash[lifecycle_dataset_hash] = lifecycle_dataset
    lifecycle_bindings = _objects(
        payload["lifecycle_observation_bindings"],
        "option_simulation.lifecycle_observation_bindings",
    )
    if len(lifecycle_bindings) != len(lifecycle):
        raise SimulationEvidenceError("option_lifecycle_dataset_binding_required")
    lifecycle_dataset_hash_by_event: dict[str, str] = {}
    for index, binding in enumerate(lifecycle_bindings):
        _exact_fields(
            binding,
            {"event_hash", "dataset_snapshot_hash"},
            "option_lifecycle_observation_binding",
        )
        event_hash = _text(
            binding.get("event_hash"), "option_lifecycle_binding.event_hash"
        )
        dataset_hash = _text(
            binding.get("dataset_snapshot_hash"),
            "option_lifecycle_binding.dataset_snapshot_hash",
        )
        if (
            event_hash
            != _text(
                lifecycle[index].get("content_hash"),
                "option_lifecycle_event.content_hash",
            )
            or event_hash in lifecycle_dataset_hash_by_event
            or dataset_hash not in lifecycle_dataset_by_hash
        ):
            raise SimulationEvidenceError("option_lifecycle_dataset_binding_mismatch")
        lifecycle_dataset_hash_by_event[event_hash] = dataset_hash
    if set(lifecycle_dataset_hash_by_event.values()) != set(lifecycle_dataset_by_hash):
        raise SimulationEvidenceError("option_lifecycle_dataset_unreferenced")
    if not fills or not positions or not valuation_inputs or not greeks or not marks:
        raise SimulationEvidenceError("option_simulation_evidence_groups_required")

    event_items: list[dict[str, str]] = []
    order_by_id: dict[str, Mapping[str, object]] = {}
    if not multileg:
        if not orders:
            raise SimulationEvidenceError("single_option_orders_required")
        for order in orders:
            _verify_content_hash(
                order, "option_order_intent_evidence", "option_order_intent"
            )
            order_id = _text(order.get("order_id"), "option_order_intent.order_id")
            if order_id in order_by_id:
                raise SimulationEvidenceError("option_order_id_duplicate")
            order_by_id[order_id] = order
            if order.get("execution_policy_hash") != policy_hash:
                raise SimulationEvidenceError("option_order_policy_mismatch")
            event_items.append(
                {
                    "kind": "ORDER",
                    "content_hash": _text(order["content_hash"], "option_order.hash"),
                }
            )

    fill_by_hash: dict[str, Mapping[str, object]] = {}
    for fill in fills:
        _verify_content_hash(fill, "option_fill", "option_fill")
        fill_hash = _text(fill["content_hash"], "option_fill.content_hash")
        if fill_hash in fill_by_hash:
            raise SimulationEvidenceError("option_fill_hash_duplicate")
        fill_by_hash[fill_hash] = fill
        _validate_option_fill(fill, policy, contract_by_id, quote_by_contract)
        if not multileg:
            fill_id = _text(fill.get("fill_id"), "option_fill.fill_id")
            bound_order = order_by_id.get(fill_id)
            if bound_order is None:
                raise SimulationEvidenceError("option_fill_order_binding_missing")
            contract_hash = _text(
                fill.get("contract_hash"), "option_fill.contract_hash"
            )
            fill_contract_id = next(
                (
                    key
                    for key, value in contract_hash_by_id.items()
                    if value == contract_hash
                ),
                None,
            )
            if (
                fill_contract_id != bound_order.get("contract_id")
                or fill.get("side") != bound_order.get("side")
                or fill.get("requested_quantity") != bound_order.get("quantity")
                or fill.get("quote_hash") != bound_order.get("quote_hash")
            ):
                raise SimulationEvidenceError("option_fill_order_fields_mismatch")
            if parse_timestamp(
                _text(fill.get("filled_at"), "option_fill.filled_at"),
                "option_fill.filled_at",
            ) < parse_timestamp(
                _text(bound_order.get("requested_at"), "option_order.requested_at"),
                "option_order.requested_at",
            ):
                raise SimulationEvidenceError("option_fill_before_order")
        event_items.append({"kind": "FILL", "content_hash": fill_hash})

    position_by_id: dict[str, Mapping[str, object]] = {}
    for position in positions:
        _verify_content_hash(position, "option_position", "option_position")
        position_id = _text(position.get("position_id"), "option_position.position_id")
        if position_id in position_by_id:
            raise SimulationEvidenceError("option_position_id_duplicate")
        position_by_id[position_id] = position
        if position.get("source_fill_hash") not in fill_by_hash:
            raise SimulationEvidenceError("option_position_fill_binding_missing")
        event_items.append(
            {
                "kind": "POSITION",
                "content_hash": _text(position["content_hash"], "option_position.hash"),
            }
        )

    input_by_hash: dict[str, Mapping[str, object]] = {}
    dataset_sources = set(
        _texts(dataset.get("raw_manifest_hashes"), "dataset.raw_manifest_hashes")
    )
    dataset_start = parse_timestamp(
        _text(dataset.get("period_start"), "dataset.period_start"),
        "dataset.period_start",
    )
    dataset_end = parse_timestamp(
        _text(dataset.get("period_end"), "dataset.period_end"),
        "dataset.period_end",
    )
    for valuation in valuation_inputs:
        _verify_content_hash(
            valuation, "option_valuation_input", "option_valuation_input"
        )
        valuation_hash = _text(
            valuation["content_hash"], "option_valuation_input.content_hash"
        )
        if valuation_hash in input_by_hash:
            raise SimulationEvidenceError("option_valuation_input_duplicate")
        input_by_hash[valuation_hash] = valuation
        embedded_contract = _object(
            valuation.get("contract"), "option_valuation_input.contract"
        )
        embedded_quote = _object(valuation.get("quote"), "option_valuation_input.quote")
        contract_id = _text(
            embedded_contract.get("contract_id"), "option_valuation_input.contract_id"
        )
        if embedded_contract.get("content_hash") != contract_hash_by_id.get(
            contract_id
        ):
            raise SimulationEvidenceError("option_valuation_contract_not_in_chain")
        if embedded_quote.get("content_hash") != quote_by_contract.get(
            contract_id, {}
        ).get("content_hash"):
            raise SimulationEvidenceError("option_valuation_quote_not_in_chain")
        valuation_sources = set(
            _texts(
                valuation.get("source_manifest_hashes"),
                "option_valuation_input.source_manifest_hashes",
            )
        )
        if not valuation_sources or not valuation_sources.issubset(dataset_sources):
            raise SimulationEvidenceError("option_valuation_source_not_in_dataset")
        valuation_at = parse_timestamp(
            _text(
                valuation.get("valuation_at"),
                "option_valuation_input.valuation_at",
            ),
            "option_valuation_input.valuation_at",
        )
        quote_as_of = parse_timestamp(
            _text(embedded_quote.get("as_of"), "option_quote.as_of"),
            "option_quote.as_of",
        )
        quote_availability = _object(
            embedded_quote.get("availability"), "option_quote.availability"
        )
        quote_event_at = parse_timestamp(
            _text(quote_availability.get("event_at"), "option_quote.event_at"),
            "option_quote.event_at",
        )
        if any(
            instant < dataset_start or instant > dataset_end
            for instant in (valuation_at, quote_as_of, quote_event_at)
        ):
            raise SimulationEvidenceError("option_valuation_time_outside_dataset")

    greek_by_input: dict[str, Mapping[str, object]] = {}
    iv_by_input: dict[str, Mapping[str, object]] = {}
    model_version = _text(
        valuation_model.get("model_version"), "option_valuation_model.model_version"
    )
    for iv_result in iv_results:
        _verify_content_hash(
            iv_result, "option_implied_volatility", "option_implied_volatility"
        )
        input_hash = _text(
            iv_result.get("valuation_input_hash"), "option_iv.valuation_input_hash"
        )
        if input_hash not in input_by_hash:
            raise SimulationEvidenceError("option_iv_input_binding_missing")
        if input_hash in iv_by_input:
            raise SimulationEvidenceError("option_iv_input_duplicate")
        valuation_contract = _object(
            input_by_hash[input_hash].get("contract"), "option_valuation.contract"
        )
        if iv_result.get("contract_id") != valuation_contract.get("contract_id"):
            raise SimulationEvidenceError("option_iv_contract_binding_mismatch")
        if iv_result.get("model_version") != model_version:
            raise SimulationEvidenceError("option_iv_model_version_mismatch")
        if (
            iv_result.get("success") is not True
            or iv_result.get("failure") != "NONE"
            or iv_result.get("volatility") is None
        ):
            raise SimulationEvidenceError("option_iv_success_required")
        volatility = exact_decimal(iv_result.get("volatility"), "option_iv.volatility")
        minimum = exact_decimal(
            valuation_model.get("minimum_volatility"),
            "option_valuation_model.minimum_volatility",
        )
        maximum = exact_decimal(
            valuation_model.get("maximum_volatility"),
            "option_valuation_model.maximum_volatility",
        )
        if volatility <= 0 or volatility < minimum or volatility > maximum:
            raise SimulationEvidenceError("option_iv_volatility_outside_model_range")
        _validate_black_scholes_implied_volatility(
            valuation=input_by_hash[input_hash],
            iv_result=iv_result,
            valuation_model=valuation_model,
            execution_policy=policy,
        )
        iv_by_input[input_hash] = iv_result
    if set(iv_by_input) != set(input_by_hash):
        raise SimulationEvidenceError("option_iv_input_coverage_mismatch")
    for greek in greeks:
        _verify_content_hash(greek, "option_greeks", "option_greeks")
        input_hash = _text(
            greek.get("valuation_input_hash"), "option_greeks.input_hash"
        )
        if input_hash not in input_by_hash or input_hash in greek_by_input:
            raise SimulationEvidenceError("option_greeks_input_binding_invalid")
        iv_result = iv_by_input[input_hash]
        valuation_contract = _object(
            input_by_hash[input_hash].get("contract"), "option_valuation.contract"
        )
        if greek.get("contract_id") != valuation_contract.get("contract_id"):
            raise SimulationEvidenceError("option_greeks_contract_binding_mismatch")
        if greek.get("model_version") != model_version:
            raise SimulationEvidenceError("option_greeks_model_version_mismatch")
        if greek.get("volatility") != iv_result.get("volatility"):
            raise SimulationEvidenceError("option_greeks_iv_binding_mismatch")
        _validate_black_scholes_greeks(
            valuation=input_by_hash[input_hash],
            greek=greek,
        )
        greek_by_input[input_hash] = greek
        event_items.append(
            {
                "kind": "VALUATION",
                "content_hash": _text(greek["content_hash"], "option_greeks.hash"),
            }
        )
    if set(greek_by_input) != set(input_by_hash):
        raise SimulationEvidenceError("option_greeks_input_coverage_mismatch")

    marked_positions: set[str] = set()
    for mark in marks:
        _verify_content_hash(mark, "option_mark", "option_mark")
        position_id = _text(mark.get("position_id"), "option_mark.position_id")
        marked_position = position_by_id.get(position_id)
        input_hash = _text(
            mark.get("theoretical_input_hash"), "option_mark.theoretical_input_hash"
        )
        mark_greek = greek_by_input.get(input_hash)
        if marked_position is None or mark_greek is None:
            raise SimulationEvidenceError("option_mark_upstream_binding_missing")
        if mark.get("theoretical_price") != mark_greek.get("price"):
            raise SimulationEvidenceError("option_mark_greeks_price_mismatch")
        valuation = input_by_hash[input_hash]
        valuation_contract = _object(valuation["contract"], "option_valuation.contract")
        if marked_position.get("contract_hash") != valuation_contract.get(
            "content_hash"
        ):
            raise SimulationEvidenceError("option_mark_position_contract_mismatch")
        if mark.get("quote_hash") != _object(
            valuation["quote"], "option_valuation.quote"
        ).get("content_hash"):
            raise SimulationEvidenceError("option_mark_quote_binding_mismatch")
        marked_positions.add(position_id)
        event_items.append(
            {
                "kind": "MARK",
                "content_hash": _text(mark["content_hash"], "option_mark.hash"),
            }
        )
    if marked_positions != set(position_by_id):
        raise SimulationEvidenceError("option_position_mark_coverage_mismatch")

    for event in lifecycle:
        _exact_fields(
            event,
            {
                "event_id",
                "event_type",
                "contract_id",
                "position_id",
                "occurred_at",
                "settlement_input",
                "settlement_spot",
                "exercise_fraction",
                "exercised_quantity",
                "intrinsic_value_per_unit",
                "cash_delta",
                "deliverable_quantity_delta",
                "deliverable_asset_id",
                "source_position_hash",
                "early_exercise_decision",
                "content_hash",
            },
            "option_lifecycle_event",
        )
        _verify_content_hash(event, "option_lifecycle_event", "option_lifecycle_event")
        event_hash = _text(
            event.get("content_hash"), "option_lifecycle_event.content_hash"
        )
        lifecycle_dataset = lifecycle_dataset_by_hash[
            lifecycle_dataset_hash_by_event[event_hash]
        ]
        lifecycle_position = position_by_id.get(
            _text(event.get("position_id"), "option_lifecycle.position_id")
        )
        if lifecycle_position is None or event.get(
            "source_position_hash"
        ) != lifecycle_position.get("content_hash"):
            raise SimulationEvidenceError("option_lifecycle_position_mismatch")
        contract_id = _text(event.get("contract_id"), "option_lifecycle.contract_id")
        lifecycle_contract = contract_by_id.get(contract_id)
        if lifecycle_contract is None or lifecycle_position.get(
            "contract_hash"
        ) != lifecycle_contract.get("content_hash"):
            raise SimulationEvidenceError("option_lifecycle_contract_mismatch")
        settlement_input = _object(
            event.get("settlement_input"), "option_lifecycle.settlement_input"
        )
        _exact_fields(
            settlement_input,
            {
                "settlement_input_id",
                "contract_id",
                "settlement_at",
                "availability",
                "spot_price",
                "source_manifest_hash",
                "content_hash",
            },
            "option_settlement_input",
        )
        _verify_content_hash(
            settlement_input,
            "option_settlement_input",
            "option_settlement_input",
        )
        if settlement_input.get("contract_id") != contract_id:
            raise SimulationEvidenceError("option_settlement_input_contract_mismatch")
        if event.get("settlement_spot") != settlement_input.get("spot_price"):
            raise SimulationEvidenceError("option_settlement_input_spot_mismatch")
        if settlement_input.get("source_manifest_hash") not in _texts(
            lifecycle_dataset.get("raw_manifest_hashes"),
            "option_lifecycle_dataset.raw_manifest_hashes",
        ):
            raise SimulationEvidenceError(
                "option_settlement_input_source_unbound_to_observation_dataset"
            )
        if contract_id not in _texts(
            lifecycle_dataset.get("universe_ids"),
            "option_lifecycle_dataset.universe_ids",
        ):
            raise SimulationEvidenceError(
                "option_settlement_contract_not_in_observation_dataset"
            )
        occurred_at = _text(event.get("occurred_at"), "option_lifecycle.occurred_at")
        if not (
            parse_timestamp(
                _text(spec.get("frozen_at"), "experiment_spec.frozen_at"),
                "experiment_spec.frozen_at",
            )
            <= parse_timestamp(occurred_at, "option_lifecycle.occurred_at")
            <= parse_timestamp(
                _text(
                    lifecycle_dataset.get("knowledge_time"),
                    "option_lifecycle_dataset.knowledge_time",
                ),
                "option_lifecycle_dataset.knowledge_time",
            )
        ):
            raise SimulationEvidenceError("option_lifecycle_dataset_chronology_invalid")
        settlement_at = _text(
            settlement_input.get("settlement_at"),
            "option_settlement_input.settlement_at",
        )
        availability = _object(
            settlement_input.get("availability"),
            "option_settlement_input.availability",
        )
        _exact_fields(
            availability,
            {
                "event_at",
                "published_at",
                "provider_received_at",
                "system_received_at",
                "processed_at",
            },
            "option_settlement_input.availability",
        )
        if availability.get("event_at") != settlement_at:
            raise SimulationEvidenceError("option_settlement_input_time_mismatch")
        availability_times = tuple(
            parse_timestamp(
                _text(availability.get(key), f"option_settlement_input.{key}"),
                f"option_settlement_input.{key}",
            )
            for key in (
                "event_at",
                "published_at",
                "provider_received_at",
                "system_received_at",
                "processed_at",
            )
        )
        if tuple(sorted(availability_times)) != availability_times:
            raise SimulationEvidenceError("option_settlement_input_clock_order_invalid")
        observation_start = parse_timestamp(
            _text(
                lifecycle_dataset.get("period_start"),
                "option_lifecycle_dataset.period_start",
            ),
            "option_lifecycle_dataset.period_start",
        )
        observation_end = parse_timestamp(
            _text(
                lifecycle_dataset.get("period_end"),
                "option_lifecycle_dataset.period_end",
            ),
            "option_lifecycle_dataset.period_end",
        )
        settlement_instant = parse_timestamp(
            settlement_at, "option_settlement_input.settlement_at"
        )
        if any(
            instant < observation_start or instant > observation_end
            for instant in (*availability_times, settlement_instant)
        ):
            raise SimulationEvidenceError(
                "option_settlement_input_outside_observation_dataset_period"
            )
        if availability_times[-1] > parse_timestamp(
            _text(
                lifecycle_dataset.get("knowledge_time"),
                "option_lifecycle_dataset.knowledge_time",
            ),
            "option_lifecycle_dataset.knowledge_time",
        ):
            raise SimulationEvidenceError(
                "option_settlement_input_after_observation_dataset_knowledge"
            )
        if availability_times[-1] > parse_timestamp(
            occurred_at, "option_lifecycle.occurred_at"
        ):
            raise SimulationEvidenceError("option_settlement_input_future_knowledge")
        if availability_times[0] > parse_timestamp(
            occurred_at, "option_lifecycle.occurred_at"
        ):
            raise SimulationEvidenceError("option_settlement_input_after_event")

        spot = exact_decimal(
            settlement_input.get("spot_price"), "option_settlement_input.spot_price"
        )
        strike = exact_decimal(
            lifecycle_contract.get("strike"), "option_contract.strike"
        )
        intrinsic = (
            max(Decimal("0"), spot - strike)
            if lifecycle_contract.get("option_type") == "CALL"
            else max(Decimal("0"), strike - spot)
        )
        if event.get("intrinsic_value_per_unit") != decimal_text(intrinsic):
            raise SimulationEvidenceError("option_lifecycle_intrinsic_mismatch")
        fraction = exact_decimal(
            event.get("exercise_fraction"), "option_lifecycle.exercise_fraction"
        )
        if fraction < 0 or fraction > 1:
            raise SimulationEvidenceError("option_lifecycle_fraction_invalid")
        position_quantity = exact_decimal(
            lifecycle_position.get("quantity"), "option_position.quantity"
        )
        exercised = position_quantity * fraction if intrinsic > 0 else Decimal("0")
        if event.get("exercised_quantity") != decimal_text(exercised):
            raise SimulationEvidenceError("option_lifecycle_quantity_mismatch")

        instant = parse_timestamp(occurred_at, "option_lifecycle.occurred_at")
        expiry = parse_timestamp(
            _text(
                lifecycle_contract.get("expiration_at"),
                "option_contract.expiration_at",
            ),
            "option_contract.expiration_at",
        )
        position_side = _text(lifecycle_position.get("side"), "option_position.side")
        is_early = instant < expiry
        if is_early:
            if settlement_instant != instant:
                raise SimulationEvidenceError("option_early_settlement_time_mismatch")
        else:
            scheduled_settlement = parse_timestamp(
                _text(
                    lifecycle_contract.get("settlement_at"),
                    "option_contract.settlement_at",
                ),
                "option_contract.settlement_at",
            )
            if not expiry <= settlement_instant <= scheduled_settlement:
                raise SimulationEvidenceError("option_expiry_settlement_time_invalid")
        expected_event_type = (
            "EXERCISE"
            if is_early and position_side == "LONG"
            else "ASSIGNMENT"
            if is_early
            else "EXPIRY"
        )
        if event.get("event_type") != expected_event_type:
            raise SimulationEvidenceError("option_lifecycle_event_type_mismatch")

        decision_raw = event.get("early_exercise_decision")
        if is_early:
            decision = _object(decision_raw, "option_lifecycle.early_exercise_decision")
            _exact_fields(
                decision,
                {
                    "contract_id",
                    "evaluated_at",
                    "permitted",
                    "exercise",
                    "intrinsic_value",
                    "continuation_value",
                    "transaction_cost",
                    "reason",
                    "content_hash",
                },
                "option_early_exercise_decision",
            )
            _verify_content_hash(
                decision,
                "option_early_exercise_decision",
                "option_early_exercise_decision",
            )
            if (
                decision.get("contract_id") != contract_id
                or decision.get("evaluated_at") != occurred_at
                or decision.get("intrinsic_value") != decimal_text(intrinsic)
            ):
                raise SimulationEvidenceError(
                    "option_early_exercise_decision_binding_mismatch"
                )
            continuation = exact_decimal(
                decision.get("continuation_value"),
                "option_exercise.continuation_value",
            )
            transaction_cost = exact_decimal(
                decision.get("transaction_cost"), "option_exercise.transaction_cost"
            )
            if continuation < 0 or transaction_cost < 0:
                raise SimulationEvidenceError("option_exercise_input_negative")
            exercise_style = lifecycle_contract.get("exercise_style")
            if exercise_style == "AMERICAN":
                permitted = True
                reason = "american_exercise_window"
            elif exercise_style == "BERMUDAN":
                permitted = occurred_at in _texts(
                    lifecycle_contract.get("bermudan_exercise_at"),
                    "option_contract.bermudan_exercise_at",
                )
                reason = (
                    "bermudan_exercise_date" if permitted else "outside_bermudan_window"
                )
            else:
                permitted = False
                reason = "european_before_expiry"
            exercise = permitted and intrinsic > continuation + transaction_cost
            if permitted and not exercise:
                reason = "continuation_value_preferred"
            elif exercise:
                reason = "intrinsic_exceeds_continuation"
            if (
                decision.get("permitted") is not permitted
                or decision.get("exercise") is not exercise
                or decision.get("reason") != reason
                or not exercise
            ):
                raise SimulationEvidenceError("option_early_exercise_decision_mismatch")
        elif decision_raw is not None:
            raise SimulationEvidenceError("option_expiry_decision_forbidden")

        multiplier = exact_decimal(
            lifecycle_contract.get("multiplier"), "option_contract.multiplier"
        )
        sign = Decimal("1") if position_side == "LONG" else Decimal("-1")
        cash_delta = Decimal("0")
        deliverable_delta = Decimal("0")
        deliverable_id: object = None
        if exercised > 0:
            scale = exercised * multiplier
            if lifecycle_contract.get("settlement_type") == "CASH":
                cash_delta = sign * intrinsic * scale
            else:
                deliverable_id = lifecycle_contract.get("deliverable_asset_id")
                if lifecycle_contract.get("option_type") == "CALL":
                    deliverable_delta = sign * scale
                    cash_delta = -sign * strike * scale
                else:
                    deliverable_delta = -sign * scale
                    cash_delta = sign * strike * scale
        if (
            event.get("cash_delta") != decimal_text(cash_delta)
            or event.get("deliverable_quantity_delta")
            != decimal_text(deliverable_delta)
            or event.get("deliverable_asset_id") != deliverable_id
        ):
            raise SimulationEvidenceError("option_lifecycle_cash_delivery_mismatch")
        event_items.append(
            {
                "kind": "LIFECYCLE",
                "content_hash": _text(event["content_hash"], "option_lifecycle.hash"),
            }
        )

    if multileg:
        _validate_multileg(
            payload,
            policy,
            fills,
            contract_by_id,
            event_items,
        )

    return {
        "dataset_snapshot_hash": _text(dataset["content_hash"], "dataset.content_hash"),
        "product_chain_hash": _text(chain["content_hash"], "chain.content_hash"),
        "experiment_spec_hash": _text(spec["content_hash"], "spec.content_hash"),
        "execution_model_hash": sha256_prefixed(
            {
                "execution_policy_hash": policy_hash,
                "valuation_model_hash": valuation_model_hash,
            },
            label="option_simulation_model",
        ),
        "event_stream_hash": sha256_prefixed(
            event_items, label="derivative_simulation_event_stream"
        ),
    }


def _validate_black_scholes_greeks(
    *,
    valuation: Mapping[str, object],
    greek: Mapping[str, object],
) -> None:
    """Recompute the persisted European Black-Scholes result from its inputs."""

    contract = _object(valuation.get("contract"), "option_valuation.contract")
    if contract.get("exercise_style") != "EUROPEAN":
        raise SimulationEvidenceError("option_greeks_model_contract_unsupported")
    spot = float(exact_decimal(valuation.get("spot_price"), "option_valuation.spot"))
    strike = float(exact_decimal(contract.get("strike"), "option_contract.strike"))
    rate = float(
        exact_decimal(valuation.get("risk_free_rate"), "option_valuation.rate")
    )
    dividend = float(
        exact_decimal(valuation.get("dividend_yield"), "option_valuation.dividend")
    )
    sigma = float(exact_decimal(greek.get("volatility"), "option_greeks.volatility"))
    valuation_at = parse_timestamp(
        _text(valuation.get("valuation_at"), "option_valuation.valuation_at"),
        "option_valuation.valuation_at",
    )
    expiration_at = parse_timestamp(
        _text(contract.get("expiration_at"), "option_contract.expiration_at"),
        "option_contract.expiration_at",
    )
    time = (expiration_at - valuation_at).total_seconds() / 31_557_600.0
    if spot <= 0 or strike <= 0 or sigma <= 0 or time <= 0:
        raise SimulationEvidenceError("option_greeks_recomputation_input_invalid")
    root_time = math.sqrt(time)
    d1 = (math.log(spot / strike) + (rate - dividend + sigma * sigma / 2.0) * time) / (
        sigma * root_time
    )
    d2 = d1 - sigma * root_time
    discounted_spot = math.exp(-dividend * time)
    discounted_strike = math.exp(-rate * time)
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)

    def cdf(value: float) -> float:
        return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))

    gamma = discounted_spot * pdf / (spot * sigma * root_time)
    vega = spot * discounted_spot * pdf * root_time
    if contract.get("option_type") == "CALL":
        price = spot * discounted_spot * cdf(d1) - strike * discounted_strike * cdf(d2)
        delta = discounted_spot * cdf(d1)
        theta = (
            -spot * discounted_spot * pdf * sigma / (2.0 * root_time)
            - rate * strike * discounted_strike * cdf(d2)
            + dividend * spot * discounted_spot * cdf(d1)
        )
        rho = strike * time * discounted_strike * cdf(d2)
    elif contract.get("option_type") == "PUT":
        price = strike * discounted_strike * cdf(-d2) - spot * discounted_spot * cdf(
            -d1
        )
        delta = discounted_spot * (cdf(d1) - 1.0)
        theta = (
            -spot * discounted_spot * pdf * sigma / (2.0 * root_time)
            + rate * strike * discounted_strike * cdf(-d2)
            - dividend * spot * discounted_spot * cdf(-d1)
        )
        rho = -strike * time * discounted_strike * cdf(-d2)
    else:
        raise SimulationEvidenceError("option_greeks_option_type_invalid")

    expected = {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "theta_per_year": theta,
        "rho": rho,
    }
    for field_name, value in expected.items():
        if not math.isfinite(value):
            raise SimulationEvidenceError("option_greeks_recomputation_non_finite")
        expected_text = decimal_text(Decimal(format(value, ".15g")))
        if greek.get(field_name) != expected_text:
            raise SimulationEvidenceError(
                f"option_greeks_numerical_mismatch:{field_name}"
            )


def _validate_black_scholes_implied_volatility(
    *,
    valuation: Mapping[str, object],
    iv_result: Mapping[str, object],
    valuation_model: Mapping[str, object],
    execution_policy: Mapping[str, object],
) -> None:
    """Re-run the exact canonical IV solver from persisted primitive inputs."""

    contract = _object(valuation.get("contract"), "option_valuation.contract")
    quote = _object(valuation.get("quote"), "option_valuation.quote")
    if contract.get("exercise_style") != "EUROPEAN":
        raise SimulationEvidenceError("option_iv_model_contract_unsupported")
    quote_state = _text(quote.get("state"), "option_quote.state")
    allowed_state = quote_state == "NORMAL" or (
        quote_state == "ILLIQUID" and execution_policy.get("allow_illiquid") is True
    )
    if not allowed_state:
        raise SimulationEvidenceError("option_iv_quote_state_not_admitted")
    bid = exact_decimal(quote.get("bid"), "option_quote.bid", positive=True)
    ask = exact_decimal(quote.get("ask"), "option_quote.ask", positive=True)
    if bid > ask:
        raise SimulationEvidenceError("option_iv_quote_crossed")
    market_price = (bid + ask) / Decimal("2")
    if iv_result.get("market_price") != decimal_text(market_price):
        raise SimulationEvidenceError("option_iv_market_price_quote_mismatch")

    option_type = _enum_value(
        OptionType,
        contract.get("option_type"),
        "option_contract.option_type",
    )
    valuation_at = parse_timestamp(
        _text(valuation.get("valuation_at"), "option_valuation.valuation_at"),
        "option_valuation.valuation_at",
    )
    expiration_at = parse_timestamp(
        _text(contract.get("expiration_at"), "option_contract.expiration_at"),
        "option_contract.expiration_at",
    )
    time_years = Decimal(
        str(max(0.0, (expiration_at - valuation_at).total_seconds()))
    ) / Decimal("31557600")
    tolerance = exact_decimal(
        valuation_model.get("price_tolerance"),
        "option_valuation_model.price_tolerance",
        positive=True,
    )
    minimum_volatility = exact_decimal(
        valuation_model.get("minimum_volatility"),
        "option_valuation_model.minimum_volatility",
        positive=True,
    )
    maximum_volatility = exact_decimal(
        valuation_model.get("maximum_volatility"),
        "option_valuation_model.maximum_volatility",
        positive=True,
    )
    maximum_iterations = _integer(
        valuation_model.get("maximum_iterations"),
        "option_valuation_model.maximum_iterations",
    )
    if minimum_volatility >= maximum_volatility or maximum_iterations <= 0:
        raise SimulationEvidenceError("option_iv_model_configuration_invalid")
    semantic = solve_black_scholes_implied_volatility(
        option_type=option_type,
        spot=exact_decimal(
            valuation.get("spot_price"), "option_valuation.spot_price", positive=True
        ),
        strike=exact_decimal(
            contract.get("strike"), "option_contract.strike", positive=True
        ),
        risk_free_rate=exact_decimal(
            valuation.get("risk_free_rate"), "option_valuation.risk_free_rate"
        ),
        dividend_yield=exact_decimal(
            valuation.get("dividend_yield"), "option_valuation.dividend_yield"
        ),
        time_years=time_years,
        market_price=market_price,
        minimum_volatility=minimum_volatility,
        maximum_volatility=maximum_volatility,
        price_tolerance=tolerance,
        maximum_iterations=maximum_iterations,
    )
    if semantic.failure is not IVFailure.NONE or semantic.volatility is None:
        raise SimulationEvidenceError("option_iv_solver_did_not_converge")
    expected = {
        "volatility": decimal_text(semantic.volatility),
        "iterations": semantic.iterations,
        "lower_price_bound": decimal_text(semantic.lower_price_bound),
        "upper_price_bound": decimal_text(semantic.upper_price_bound),
    }
    for field_name, expected_value in expected.items():
        if iv_result.get(field_name) != expected_value:
            raise SimulationEvidenceError(f"option_iv_solver_mismatch:{field_name}")
    if semantic.residual is None or semantic.residual > tolerance:
        raise SimulationEvidenceError("option_iv_solver_residual_exceeds_tolerance")


def _validate_option_policy(policy: Mapping[str, object]) -> None:
    _verify_content_hash(policy, "option_execution_policy", "option_execution_policy")
    cost_payload = {
        "policy_id": policy.get("policy_id"),
        "policy_version": policy.get("policy_version"),
        "fee_model": "FLAT_PER_FILLED_CONTRACT",
        "fee_per_contract": policy.get("fee_per_contract"),
    }
    fill_payload = {
        "policy_id": policy.get("policy_id"),
        "policy_version": policy.get("policy_version"),
        "fill_model_version": policy.get("fill_model_version"),
        "method": "CROSS_RECORDED_TWO_SIDED_QUOTE",
        "mode": policy.get("mode"),
        "slippage_ticks": policy.get("slippage_ticks"),
        "allow_partial": policy.get("allow_partial"),
        "allow_illiquid": policy.get("allow_illiquid"),
        "maximum_leg_time_skew_seconds": policy.get("maximum_leg_time_skew_seconds"),
    }
    if policy.get("cost_model_hash") != sha256_prefixed(
        cost_payload, label="option_cost_model"
    ):
        raise SimulationEvidenceError("option_cost_model_hash_mismatch")
    if policy.get("fill_model_hash") != sha256_prefixed(
        fill_payload, label="option_fill_model"
    ):
        raise SimulationEvidenceError("option_fill_model_hash_mismatch")


def _validate_option_fill(
    fill: Mapping[str, object],
    policy: Mapping[str, object],
    contracts: Mapping[str, Mapping[str, object]],
    quotes: Mapping[str, Mapping[str, object]],
) -> None:
    contract_hash = _text(fill.get("contract_hash"), "option_fill.contract_hash")
    contract_id = next(
        (
            key
            for key, value in contracts.items()
            if value.get("content_hash") == contract_hash
        ),
        None,
    )
    if contract_id is None:
        raise SimulationEvidenceError("option_fill_contract_not_in_chain")
    quote = quotes.get(contract_id)
    if quote is None or fill.get("quote_hash") != quote.get("content_hash"):
        raise SimulationEvidenceError("option_fill_quote_not_in_chain")
    status = _text(fill.get("status"), "option_fill.status")
    executed_statuses = {
        FillStatus.FILLED.value,
        FillStatus.PARTIAL.value,
        FillStatus.UNWOUND.value,
    }
    expected_slippage = (
        policy.get("slippage_ticks") if status in executed_statuses else 0
    )
    if fill.get("slippage_ticks") != expected_slippage:
        raise SimulationEvidenceError("option_fill_slippage_policy_mismatch")
    requested_quantity = exact_decimal(
        fill.get("requested_quantity"), "option_fill.requested_quantity", positive=True
    )
    filled_quantity = exact_decimal(
        fill.get("filled_quantity"), "option_fill.filled_quantity"
    )
    fee = exact_decimal(fill.get("fee"), "option_fill.fee")
    fee_rate = exact_decimal(
        policy.get("fee_per_contract"), "option_execution_policy.fee"
    )
    if fee != filled_quantity * fee_rate:
        raise SimulationEvidenceError("option_fill_fee_policy_mismatch")
    if filled_quantity < 0 or filled_quantity > requested_quantity:
        raise SimulationEvidenceError("option_fill_quantity_invalid")
    if status == FillStatus.PARTIAL.value and policy.get("allow_partial") is not True:
        raise SimulationEvidenceError("option_partial_fill_not_allowed")
    price_raw = fill.get("price")
    if status in executed_statuses:
        price = exact_decimal(price_raw, "option_fill.price", positive=True)
        if status == FillStatus.FILLED.value and filled_quantity != requested_quantity:
            raise SimulationEvidenceError("option_fill_full_quantity_mismatch")
        if status == FillStatus.PARTIAL.value and not (
            Decimal("0") < filled_quantity < requested_quantity
        ):
            raise SimulationEvidenceError("option_fill_partial_quantity_mismatch")
        if fill.get("failure_code") is not None:
            raise SimulationEvidenceError("option_fill_success_failure_code_forbidden")
        side = _text(fill.get("side"), "option_fill.side")
        quote_price_raw = quote.get(
            "ask" if side == TransactionSide.BUY.value else "bid"
        )
        base_price = exact_decimal(
            quote_price_raw, "option_fill.executable_quote", positive=True
        )
        tick = exact_decimal(
            contracts[contract_id].get("price_tick"), "option_contract.price_tick"
        )
        slippage = tick * _integer(
            policy.get("slippage_ticks"), "option_policy.slippage_ticks"
        )
        expected = (
            base_price + slippage
            if side == TransactionSide.BUY.value
            else base_price - slippage
        )
        if price != expected:
            raise SimulationEvidenceError("option_fill_price_policy_mismatch")
        multiplier = exact_decimal(
            contracts[contract_id].get("multiplier"),
            "option_contract.multiplier",
            positive=True,
        )
        gross = price * filled_quantity * multiplier
        expected_cash = (
            -gross - fee if side == TransactionSide.BUY.value else gross - fee
        )
    elif status in {FillStatus.FAILED.value, FillStatus.UNFILLED.value}:
        if (
            price_raw is not None
            or filled_quantity != 0
            or fee != 0
            or not isinstance(fill.get("failure_code"), str)
        ):
            raise SimulationEvidenceError("option_fill_unexecuted_fields_invalid")
        expected_cash = Decimal("0")
    else:
        raise SimulationEvidenceError("option_fill_status_invalid")
    if fill.get("cash_flow") != decimal_text(expected_cash):
        raise SimulationEvidenceError("option_fill_cash_flow_mismatch")


def _validate_multileg_attempt_semantics(
    *,
    fill: Mapping[str, object],
    contract: Mapping[str, object],
    quote: Mapping[str, object],
    participation_rate: Decimal,
    allow_partial: bool,
    allow_illiquid: bool,
    slippage_ticks: int,
) -> None:
    """Recompute the attempted fill state and failure from persisted inputs."""

    fill_time = parse_timestamp(
        _text(fill.get("filled_at"), "option_fill.filled_at"),
        "option_fill.filled_at",
    )
    availability = _object(quote.get("availability"), "option_quote.availability")
    if (
        parse_timestamp(
            _text(availability.get("processed_at"), "option_quote.processed_at"),
            "option_quote.processed_at",
        )
        > fill_time
    ):
        raise SimulationEvidenceError("multileg_attempt_quote_future_knowledge")
    requested = exact_decimal(
        fill.get("requested_quantity"), "option_fill.requested_quantity", positive=True
    )
    expected_status: str
    expected_failure: str | None
    expected_filled = Decimal("0")
    if not (
        parse_timestamp(
            _text(contract.get("listing_at"), "option_contract.listing_at"),
            "option_contract.listing_at",
        )
        <= fill_time
        <= parse_timestamp(
            _text(contract.get("last_trade_at"), "option_contract.last_trade_at"),
            "option_contract.last_trade_at",
        )
    ):
        expected_status = FillStatus.FAILED.value
        expected_failure = "contract_not_tradeable"
    else:
        quote_as_of = parse_timestamp(
            _text(quote.get("as_of"), "option_quote.as_of"),
            "option_quote.as_of",
        )
        if fill_time < quote_as_of:
            raise SimulationEvidenceError("multileg_attempt_before_quote_as_of")
        quote_age = (fill_time - quote_as_of).total_seconds()
        stale_after = _integer(
            quote.get("stale_after_seconds"), "option_quote.stale_after_seconds"
        )
        quote_state = _text(quote.get("state"), "option_quote.state")
        if quote_age > stale_after:
            expected_status = FillStatus.FAILED.value
            expected_failure = "quote_stale_at_fill"
        elif quote_state != "NORMAL" and not (
            allow_illiquid and quote_state == "ILLIQUID"
        ):
            expected_status = FillStatus.FAILED.value
            expected_failure = f"quote_{quote_state.lower()}"
        else:
            side = _text(fill.get("side"), "option_fill.side")
            size_field = "ask_size" if side == TransactionSide.BUY.value else "bid_size"
            available_size = exact_decimal(
                quote.get(size_field), f"option_quote.{size_field}"
            )
            quantity_step = exact_decimal(
                contract.get("quantity_step"),
                "option_contract.quantity_step",
                positive=True,
            )
            capacity = available_size * participation_rate
            capacity_steps = (capacity // quantity_step) * quantity_step
            expected_filled = min(requested, capacity_steps)
            if expected_filled <= 0 or (
                expected_filled < requested and not allow_partial
            ):
                expected_status = FillStatus.UNFILLED.value
                expected_failure = "insufficient_displayed_liquidity"
                expected_filled = Decimal("0")
            else:
                price_field = "ask" if side == TransactionSide.BUY.value else "bid"
                base_price = exact_decimal(
                    quote.get(price_field),
                    f"option_quote.{price_field}",
                    positive=True,
                )
                price_tick = exact_decimal(
                    contract.get("price_tick"),
                    "option_contract.price_tick",
                    positive=True,
                )
                adjustment = price_tick * slippage_ticks
                execution_price = (
                    base_price + adjustment
                    if side == TransactionSide.BUY.value
                    else base_price - adjustment
                )
                if execution_price <= 0:
                    expected_status = FillStatus.FAILED.value
                    expected_failure = "slippage_price_non_positive"
                    expected_filled = Decimal("0")
                else:
                    expected_status = (
                        FillStatus.FILLED.value
                        if expected_filled == requested
                        else FillStatus.PARTIAL.value
                    )
                    expected_failure = None
    if (
        fill.get("status") != expected_status
        or fill.get("failure_code") != expected_failure
        or fill.get("filled_quantity") != decimal_text(expected_filled)
    ):
        raise SimulationEvidenceError("multileg_attempt_semantics_mismatch")


def _validate_multileg(
    payload: Mapping[str, object],
    policy: Mapping[str, object],
    fills: Sequence[Mapping[str, object]],
    contracts: Mapping[str, Mapping[str, object]],
    event_items: list[dict[str, str]],
) -> None:
    order = _object(payload["multi_leg_order"], "multi_leg_order")
    result = _object(payload["multi_leg_execution"], "multi_leg_execution")
    _verify_content_hash(order, "option_multileg_order", "multi_leg_order")
    _verify_content_hash(result, "option_multileg_result", "multi_leg_execution")
    if order.get("content_hash") != result.get("order_hash"):
        raise SimulationEvidenceError("multileg_execution_order_mismatch")
    if order.get("execution_policy_hash") != policy.get("content_hash"):
        raise SimulationEvidenceError("multileg_order_policy_hash_mismatch")
    expected_policy = {
        OptionExecutionMode.SIMULTANEOUS.value: MultiLegExecutionPolicy.SIMULTANEOUS.value,
        OptionExecutionMode.SEQUENTIAL.value: MultiLegExecutionPolicy.SEQUENTIAL.value,
    }.get(_text(policy.get("mode"), "option_execution_policy.mode"))
    if (
        order.get("policy") != expected_policy
        or result.get("policy") != expected_policy
    ):
        raise SimulationEvidenceError("multileg_execution_mode_mismatch")
    if order.get("maximum_leg_time_skew_seconds") != policy.get(
        "maximum_leg_time_skew_seconds"
    ):
        raise SimulationEvidenceError("multileg_time_skew_policy_mismatch")
    if order.get("allow_partial") != policy.get("allow_partial"):
        raise SimulationEvidenceError("multileg_partial_policy_mismatch")
    if result.get("group_id") != order.get("group_id"):
        raise SimulationEvidenceError("multileg_execution_group_mismatch")
    legs = _objects(order.get("legs"), "multi_leg_order.legs")
    if len(legs) < 2 or len(legs) != len(fills):
        raise SimulationEvidenceError("multileg_attempt_coverage_mismatch")
    raw_participation = _object(
        payload.get("multi_leg_participation_rates"),
        "multi_leg_participation_rates",
    )
    leg_ids = {_text(item.get("leg_id"), "multi_leg_order.leg_id") for item in legs}
    if not set(raw_participation).issubset(leg_ids):
        raise SimulationEvidenceError("multileg_participation_leg_unknown")
    participation_rates: dict[str, Decimal] = {}
    for leg_id, raw_rate in raw_participation.items():
        rate = exact_decimal(raw_rate, "multi_leg_participation_rate", positive=True)
        if rate > 1:
            raise SimulationEvidenceError("multileg_participation_rate_invalid")
        participation_rates[leg_id] = rate
    attempted_hashes = _texts(
        result.get("attempted_fill_hashes"), "multi_leg_execution.attempted_fills"
    )
    actual_attempted_hashes = tuple(
        _text(item.get("content_hash"), "option_fill.content_hash") for item in fills
    )
    if attempted_hashes != actual_attempted_hashes:
        raise SimulationEvidenceError("multileg_attempted_fill_binding_mismatch")
    if len(set(attempted_hashes)) != len(attempted_hashes):
        raise SimulationEvidenceError("multileg_attempted_fill_duplicate")

    requested_at = parse_timestamp(
        _text(order.get("requested_at"), "multi_leg_order.requested_at"),
        "multi_leg_order.requested_at",
    )
    fill_times = []
    contract_id_by_hash = {
        _text(value.get("content_hash"), "option_contract.content_hash"): key
        for key, value in contracts.items()
    }
    chain = _object(payload.get("product_chain"), "option_chain")
    quote_by_contract = {
        _text(item.get("contract_id"), "option_quote.contract_id"): item
        for item in _objects(chain.get("quotes"), "option_chain.quotes")
    }
    for leg, fill in zip(legs, fills, strict=True):
        leg_id = _text(leg.get("leg_id"), "multi_leg_order.leg_id")
        expected_side = (
            TransactionSide.BUY.value
            if leg.get("side") == "LONG"
            else TransactionSide.SELL.value
            if leg.get("side") == "SHORT"
            else None
        )
        if (
            fill.get("fill_id") != f"{order.get('group_id')}.{leg_id}"
            or fill.get("contract_hash") != leg.get("contract_hash")
            or fill.get("side") != expected_side
            or fill.get("requested_quantity") != leg.get("quantity")
        ):
            raise SimulationEvidenceError("multileg_attempted_fill_leg_mismatch")
        fill_time = parse_timestamp(
            _text(fill.get("filled_at"), "option_fill.filled_at"),
            "option_fill.filled_at",
        )
        if fill_time < requested_at:
            raise SimulationEvidenceError("multileg_fill_before_order")
        contract_hash = _text(fill.get("contract_hash"), "option_fill.contract_hash")
        contract_id = contract_id_by_hash[contract_hash]
        _validate_multileg_attempt_semantics(
            fill=fill,
            contract=contracts[contract_id],
            quote=quote_by_contract[contract_id],
            participation_rate=participation_rates.get(leg_id, Decimal("1")),
            allow_partial=order.get("allow_partial") is True,
            allow_illiquid=policy.get("allow_illiquid") is True,
            slippage_ticks=_integer(
                policy.get("slippage_ticks"),
                "option_execution_policy.slippage_ticks",
            ),
        )
        fill_times.append(fill_time)

    fully_filled = all(fill.get("status") == FillStatus.FILLED.value for fill in fills)
    executed = tuple(
        fill
        for fill in fills
        if fill.get("status") in {FillStatus.FILLED.value, FillStatus.PARTIAL.value}
    )
    maximum_skew = _integer(
        order.get("maximum_leg_time_skew_seconds"),
        "multi_leg_order.maximum_leg_time_skew_seconds",
    )
    time_skew = (max(fill_times) - min(fill_times)).total_seconds()
    if expected_policy == MultiLegExecutionPolicy.SIMULTANEOUS.value:
        expected_committed = (
            tuple(fills) if fully_filled and time_skew <= maximum_skew else ()
        )
        expected_state = "FILLED" if expected_committed else "FAILED"
        expected_failure = (
            None
            if expected_committed
            else "simultaneous_time_skew"
            if time_skew > maximum_skew
            else "simultaneous_atomic_fill_failed"
        )
        expected_legging: tuple[str, ...] = ()
    else:
        expected_committed = executed
        expected_state = (
            "FILLED" if fully_filled else "PARTIAL" if expected_committed else "FAILED"
        )
        expected_failure = (
            "sequential_no_leg_filled"
            if not expected_committed
            else "sequential_partial_fill_forbidden"
            if not fully_filled and order.get("allow_partial") is not True
            else None
        )
        expected_legging = (
            ()
            if fully_filled
            else tuple(
                contract_id_by_hash[
                    _text(fill.get("contract_hash"), "option_fill.contract_hash")
                ]
                for fill in expected_committed
            )
        )
    expected_committed_hashes = tuple(
        _text(item.get("content_hash"), "option_fill.content_hash")
        for item in expected_committed
    )
    committed_hashes = _texts(
        result.get("committed_fill_hashes"), "multi_leg_execution.committed_fills"
    )
    if committed_hashes != expected_committed_hashes:
        raise SimulationEvidenceError("multileg_committed_fill_binding_mismatch")
    if (
        result.get("state") != expected_state
        or result.get("failure_code") != expected_failure
    ):
        raise SimulationEvidenceError("multileg_execution_state_failure_mismatch")
    if (
        _texts(
            result.get("legging_exposure_contract_ids"),
            "multi_leg_execution.legging_exposure_contract_ids",
        )
        != expected_legging
    ):
        raise SimulationEvidenceError("multileg_legging_exposure_mismatch")
    expected_cash_flow = sum(
        (
            exact_decimal(item.get("cash_flow"), "option_fill.cash_flow")
            for item in expected_committed
        ),
        Decimal("0"),
    )
    if result.get("net_cash_flow") != decimal_text(expected_cash_flow):
        raise SimulationEvidenceError("multileg_net_cash_flow_mismatch")
    if parse_timestamp(
        _text(result.get("opened_at"), "multi_leg_execution.opened_at"),
        "multi_leg_execution.opened_at",
    ) != min(fill_times) or parse_timestamp(
        _text(result.get("finished_at"), "multi_leg_execution.finished_at"),
        "multi_leg_execution.finished_at",
    ) != max(fill_times):
        raise SimulationEvidenceError("multileg_execution_time_mismatch")

    leg_contract_hashes = {
        _text(item.get("contract_hash"), "multi_leg_order.contract_hash")
        for item in legs
    }
    known_contract_hashes = {
        _text(item.get("content_hash"), "option_contract.content_hash")
        for item in contracts.values()
    }
    if len(leg_contract_hashes) < 2 or not leg_contract_hashes.issubset(
        known_contract_hashes
    ):
        raise SimulationEvidenceError("multileg_contract_not_in_chain")
    event_items.insert(
        0,
        {
            "kind": "MULTI_LEG_ORDER",
            "content_hash": _text(order["content_hash"], "multi_leg_order.hash"),
        },
    )
    event_items.append(
        {
            "kind": "MULTI_LEG_EXECUTION",
            "content_hash": _text(result["content_hash"], "multi_leg_execution.hash"),
        }
    )


def _verify_content_hash(
    payload: Mapping[str, object], label: str, field_name: str
) -> None:
    raw_hash = _text(payload.get("content_hash"), f"{field_name}.content_hash")
    require_hash(raw_hash, f"{field_name}.content_hash")
    identity = {key: value for key, value in payload.items() if key != "content_hash"}
    expected = sha256_prefixed(identity, label=label)
    if raw_hash != expected:
        raise SimulationEvidenceError(f"{field_name}_content_hash_mismatch")


def _canonical_json(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SimulationEvidenceError("simulation_payload_not_canonical_json") from exc


def _decode_canonical_object(value: str, label: str) -> dict[str, object]:
    if not isinstance(value, str):
        raise SimulationEvidenceError(f"{label}_must_be_canonical_json_text")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise SimulationEvidenceError(f"{label}_invalid_json") from exc
    if not isinstance(decoded, dict):
        raise SimulationEvidenceError(f"{label}_must_be_object")
    if _canonical_json(decoded) != value:
        raise SimulationEvidenceError(f"{label}_not_canonical")
    return decoded


def _require_schema(value: int) -> None:
    if value != SIMULATION_EVIDENCE_SCHEMA_VERSION:
        raise SimulationEvidenceError("simulation_evidence_schema_unsupported")


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise SimulationEvidenceError(f"{label}_must_be_object")
    return value


def _objects(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    return tuple(_object(item, f"{label}[]") for item in _sequence(value, label))


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise SimulationEvidenceError(f"{label}_must_be_array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise SimulationEvidenceError(f"{label}_must_be_text")
    return value


def _texts(value: object, label: str) -> tuple[str, ...]:
    return tuple(_text(item, f"{label}[]") for item in _sequence(value, label))


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SimulationEvidenceError(f"{label}_must_be_integer")
    return value


def _enum_value(enum_type: type[_EnumT], value: object, label: str) -> _EnumT:
    text = _text(value, label)
    try:
        return enum_type(text)
    except ValueError as exc:
        raise SimulationEvidenceError(f"{label}_invalid") from exc


def _exact_fields(value: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise SimulationEvidenceError(
            f"{label}_fields_invalid:missing={missing}:unknown={unknown}"
        )
