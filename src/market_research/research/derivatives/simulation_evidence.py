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
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Sequence

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
    FillStatus,
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
    TransactionSide,
    ValuationInputSnapshot,
)


SIMULATION_EVIDENCE_SCHEMA_VERSION = DERIVATIVE_RESEARCH_SCHEMA_VERSION


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
        require_stable_id(
            self.policy_version, "option_execution_policy.policy_version"
        )
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
            raise SimulationEvidenceError(
                "option_execution_policy_slippage_invalid"
            )
        skew = self.maximum_leg_time_skew_seconds
        if self.mode is OptionExecutionMode.SINGLE:
            if skew is not None:
                raise SimulationEvidenceError(
                    "single_option_policy_leg_skew_forbidden"
                )
        elif isinstance(skew, bool) or not isinstance(skew, int) or skew < 0:
            raise SimulationEvidenceError(
                "multileg_option_policy_leg_skew_required"
            )
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
            sha256_prefixed(
                self.identity_payload(), label="option_execution_policy"
            ),
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
        orders: Sequence[OptionOrderIntentEvidence],
        fills: Sequence[OptionFill],
        positions: Sequence[OptionPosition],
        valuation_inputs: Sequence[ValuationInputSnapshot],
        implied_volatilities: Sequence[ImpliedVolatilityResult],
        greeks: Sequence[OptionGreeks],
        marks: Sequence[OptionMark],
        lifecycle_events: Sequence[OptionLifecycleEvent] = (),
    ) -> "DerivativeSimulationEvidence":
        if execution_policy.mode is not OptionExecutionMode.SINGLE:
            raise SimulationEvidenceError("single_option_execution_mode_required")
        payload = _option_payload(
            dataset=dataset,
            experiment_spec=experiment_spec,
            chain=chain,
            execution_policy=execution_policy,
            orders=orders,
            fills=fills,
            positions=positions,
            valuation_inputs=valuation_inputs,
            implied_volatilities=implied_volatilities,
            greeks=greeks,
            marks=marks,
            lifecycle_events=lifecycle_events,
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
        order: MultiLegOrder,
        execution_result: MultiLegExecutionResult,
        positions: Sequence[OptionPosition],
        valuation_inputs: Sequence[ValuationInputSnapshot],
        implied_volatilities: Sequence[ImpliedVolatilityResult],
        greeks: Sequence[OptionGreeks],
        marks: Sequence[OptionMark],
        lifecycle_events: Sequence[OptionLifecycleEvent] = (),
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
            orders=(),
            fills=execution_result.committed_fills,
            positions=positions,
            valuation_inputs=valuation_inputs,
            implied_volatilities=implied_volatilities,
            greeks=greeks,
            marks=marks,
            lifecycle_events=lifecycle_events,
        )
        payload["multi_leg_order"] = {
            **order.identity_payload(),
            "content_hash": order.content_hash,
        }
        payload["multi_leg_execution"] = {
            **execution_result.identity_payload(),
            "content_hash": execution_result.content_hash,
        }
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
    orders: Sequence[OptionOrderIntentEvidence],
    fills: Sequence[OptionFill],
    positions: Sequence[OptionPosition],
    valuation_inputs: Sequence[ValuationInputSnapshot],
    implied_volatilities: Sequence[ImpliedVolatilityResult],
    greeks: Sequence[OptionGreeks],
    marks: Sequence[OptionMark],
    lifecycle_events: Sequence[OptionLifecycleEvent],
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
    _require_members(
        lifecycle_events, OptionLifecycleEvent, "option_lifecycle_events"
    )
    return {
        "dataset_snapshot": dataset.as_dict(),
        "experiment_spec": experiment_spec.as_dict(),
        "product_chain": chain.as_dict(),
        "execution_policy": execution_policy.as_dict(),
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
    if not isinstance(
        chain, (ContractChainSnapshot, OptionChainSnapshot)
    ):
        raise SimulationEvidenceError("derivative_product_chain_required")
    if not isinstance(
        execution_model, (FuturesSimulator, OptionExecutionPolicy)
    ):
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
    return _validate_option_payload(payload, multileg=kind is SimulationProductKind.MULTI_LEG)


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
    dataset_universe = set(
        _texts(dataset.get("universe_ids"), "dataset.universe_ids")
    )
    if not dataset_universe:
        raise SimulationEvidenceError("simulation_dataset_universe_empty")
    filter_contract = _object(
        dataset.get("filter_contract"), "dataset.filter_contract"
    )
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
        for optional_key in ("margin_call", "roll_execution"):
            raw = step.get(optional_key)
            if raw is not None:
                optional = _object(raw, f"futures_step.{optional_key}")
                known_event_hashes.add(
                    _text(optional["content_hash"], f"futures_step.{optional_key}.hash")
                )
        ledger = _object(step.get("ledger"), "futures_step.ledger")
        ledger_events = _texts(ledger.get("event_hashes"), "futures_ledger.event_hashes")
        if prior_ledger_events and tuple(ledger_events[: len(prior_ledger_events)]) != prior_ledger_events:
            raise SimulationEvidenceError("futures_ledger_event_history_rewritten")
        if not set(ledger_events).issubset(known_event_hashes):
            raise SimulationEvidenceError("futures_ledger_event_not_in_steps")
        prior_ledger_events = ledger_events
        event_items.append({"kind": "STEP", "content_hash": step_hash})
    chain_lifecycle_hashes = {
        _text(item.get("content_hash"), "futures_chain.lifecycle_hash")
        for item in _objects(chain.get("lifecycle_events"), "futures_chain.lifecycle_events")
    }
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


def _validate_option_payload(
    payload: Mapping[str, object], *, multileg: bool
) -> dict[str, str]:
    expected = {
        "dataset_snapshot",
        "experiment_spec",
        "product_chain",
        "execution_policy",
        "orders",
        "fills",
        "positions",
        "valuation_inputs",
        "implied_volatilities",
        "greeks",
        "marks",
        "lifecycle_events",
    }
    if multileg:
        expected.update({"multi_leg_order", "multi_leg_execution"})
    _exact_fields(payload, expected, "option_simulation")
    dataset, spec, chain = _validate_common(payload, instrument=InstrumentKind.OPTION)
    _verify_content_hash(chain, "option_chain_snapshot", "option_chain")
    policy = _object(payload["execution_policy"], "option_execution_policy")
    _validate_option_policy(policy)
    policy_hash = _text(policy["content_hash"], "option_execution_policy.content_hash")
    if spec.get("simulation_policy_hash") != policy_hash:
        raise SimulationEvidenceError("option_spec_execution_policy_mismatch")
    if spec.get("cost_model_hash") != policy.get("cost_model_hash"):
        raise SimulationEvidenceError("option_spec_cost_model_mismatch")
    if spec.get("fill_model_hash") != policy.get("fill_model_hash"):
        raise SimulationEvidenceError("option_spec_fill_model_mismatch")
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
        quote_by_contract[_text(quote.get("contract_id"), "option_quote.contract_id")] = quote
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
            contract_hash = _text(fill.get("contract_hash"), "option_fill.contract_hash")
            fill_contract_id = next(
                (key for key, value in contract_hash_by_id.items() if value == contract_hash),
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
    for valuation in valuation_inputs:
        _verify_content_hash(
            valuation, "option_valuation_input", "option_valuation_input"
        )
        valuation_hash = _text(
            valuation["content_hash"], "option_valuation_input.content_hash"
        )
        input_by_hash[valuation_hash] = valuation
        embedded_contract = _object(
            valuation.get("contract"), "option_valuation_input.contract"
        )
        embedded_quote = _object(valuation.get("quote"), "option_valuation_input.quote")
        contract_id = _text(
            embedded_contract.get("contract_id"), "option_valuation_input.contract_id"
        )
        if embedded_contract.get("content_hash") != contract_hash_by_id.get(contract_id):
            raise SimulationEvidenceError("option_valuation_contract_not_in_chain")
        if embedded_quote.get("content_hash") != quote_by_contract.get(contract_id, {}).get("content_hash"):
            raise SimulationEvidenceError("option_valuation_quote_not_in_chain")

    greek_by_input: dict[str, Mapping[str, object]] = {}
    for iv_result in iv_results:
        _verify_content_hash(
            iv_result, "option_implied_volatility", "option_implied_volatility"
        )
        if iv_result.get("valuation_input_hash") not in input_by_hash:
            raise SimulationEvidenceError("option_iv_input_binding_missing")
    for greek in greeks:
        _verify_content_hash(greek, "option_greeks", "option_greeks")
        input_hash = _text(greek.get("valuation_input_hash"), "option_greeks.input_hash")
        if input_hash not in input_by_hash or input_hash in greek_by_input:
            raise SimulationEvidenceError("option_greeks_input_binding_invalid")
        greek_by_input[input_hash] = greek
        event_items.append(
            {
                "kind": "VALUATION",
                "content_hash": _text(greek["content_hash"], "option_greeks.hash"),
            }
        )

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
        if marked_position.get("contract_hash") != valuation_contract.get("content_hash"):
            raise SimulationEvidenceError("option_mark_position_contract_mismatch")
        if mark.get("quote_hash") != _object(valuation["quote"], "option_valuation.quote").get("content_hash"):
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
        _verify_content_hash(
            event, "option_lifecycle_event", "option_lifecycle_event"
        )
        lifecycle_position = position_by_id.get(
            _text(event.get("position_id"), "option_lifecycle.position_id")
        )
        if lifecycle_position is None or event.get("source_position_hash") != lifecycle_position.get("content_hash"):
            raise SimulationEvidenceError("option_lifecycle_position_mismatch")
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
        "execution_model_hash": policy_hash,
        "event_stream_hash": sha256_prefixed(
            event_items, label="derivative_simulation_event_stream"
        ),
    }


def _validate_option_policy(policy: Mapping[str, object]) -> None:
    _verify_content_hash(
        policy, "option_execution_policy", "option_execution_policy"
    )
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
        "maximum_leg_time_skew_seconds": policy.get(
            "maximum_leg_time_skew_seconds"
        ),
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
    if fill.get("slippage_ticks") != policy.get("slippage_ticks"):
        raise SimulationEvidenceError("option_fill_slippage_policy_mismatch")
    filled_quantity = exact_decimal(
        fill.get("filled_quantity"), "option_fill.filled_quantity"
    )
    fee = exact_decimal(fill.get("fee"), "option_fill.fee")
    fee_rate = exact_decimal(
        policy.get("fee_per_contract"), "option_execution_policy.fee"
    )
    if fee != filled_quantity * fee_rate:
        raise SimulationEvidenceError("option_fill_fee_policy_mismatch")
    status = _text(fill.get("status"), "option_fill.status")
    if status == FillStatus.PARTIAL.value and policy.get("allow_partial") is not True:
        raise SimulationEvidenceError("option_partial_fill_not_allowed")
    price_raw = fill.get("price")
    if status in {
        FillStatus.FILLED.value,
        FillStatus.PARTIAL.value,
        FillStatus.UNWOUND.value,
    }:
        price = exact_decimal(price_raw, "option_fill.price", positive=True)
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
    if order.get("policy") != expected_policy or result.get("policy") != expected_policy:
        raise SimulationEvidenceError("multileg_execution_mode_mismatch")
    if order.get("maximum_leg_time_skew_seconds") != policy.get(
        "maximum_leg_time_skew_seconds"
    ):
        raise SimulationEvidenceError("multileg_time_skew_policy_mismatch")
    if order.get("allow_partial") != policy.get("allow_partial"):
        raise SimulationEvidenceError("multileg_partial_policy_mismatch")
    committed_hashes = _texts(
        result.get("committed_fill_hashes"), "multi_leg_execution.committed_fills"
    )
    if committed_hashes != tuple(
        _text(item["content_hash"], "option_fill.content_hash") for item in fills
    ):
        raise SimulationEvidenceError("multileg_committed_fill_binding_mismatch")
    leg_contract_hashes = {
        _text(_object(item, "multi_leg_order.leg").get("contract_hash"), "multi_leg_order.contract_hash")
        for item in _sequence(order.get("legs"), "multi_leg_order.legs")
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
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
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


def _enum_value(
    enum_type: type[SimulationProductKind], value: object, label: str
) -> SimulationProductKind:
    text = _text(value, label)
    try:
        return enum_type(text)
    except ValueError as exc:
        raise SimulationEvidenceError(f"{label}_invalid") from exc


def _exact_fields(
    value: Mapping[str, object], expected: set[str], label: str
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise SimulationEvidenceError(
            f"{label}_fields_invalid:missing={missing}:unknown={unknown}"
        )
